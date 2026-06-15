terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# ── VPC ───────────────────────────────────────────────────────────────────────

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "pliac-vpc" }
}

# ── Internet Gateway + Route Table ───────────────────────────────────────────
# Required so the EC2 instance can reach the internet at boot time to download
# NSSM and iperf3. Traffic to 10.2.0.0/16 (Azure) still routes via VGW thanks
# to route propagation on this same table.

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "pliac-igw" }
}

resource "aws_route_table" "main" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "pliac-rt" }
}

resource "aws_route_table_association" "main" {
  subnet_id      = aws_subnet.main.id
  route_table_id = aws_route_table.main.id
}

# ── Subnet ────────────────────────────────────────────────────────────────────

resource "aws_subnet" "main" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.subnet_cidr
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = true # assigns public IP so user_data can download iperf3
  tags                    = { Name = "pliac-subnet" }
}

# ── Virtual Private Gateway ───────────────────────────────────────────────────

resource "aws_vpn_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "pliac-vgw" }
}

# Route propagation: pushes VPN routes (10.2.0.0/16 via VGW) into the custom
# route table. Both VPN connections share the same VGW so one propagation
# resource handles both tunnels.
resource "aws_vpn_gateway_route_propagation" "main" {
  vpn_gateway_id = aws_vpn_gateway.main.id
  route_table_id = aws_route_table.main.id
}

# ── Customer Gateways ─────────────────────────────────────────────────────────
# Each CGW points to one of the two Azure VPN Gateway public IPs (active-active).
# Because the IPs are different, AWS treats them as genuinely distinct CGWs —
# no BGP ASN workaround required.

resource "aws_customer_gateway" "primary" {
  bgp_asn    = 65000
  ip_address = var.azure_gateway_ip
  type       = "ipsec.1"
  tags       = { Name = "pliac-cgw-azure-primary" }
}

resource "aws_customer_gateway" "backup" {
  bgp_asn    = 65000
  ip_address = var.azure_gateway_ip_2
  type       = "ipsec.1"
  tags       = { Name = "pliac-cgw-azure-backup" }
}

# ── VPN Connections ───────────────────────────────────────────────────────────
# Each VPN Connection automatically provisions two tunnels (AWS managed HA).
# Tunnel 1 of each connection is what we wire to Azure.
# Tunnel 2 of each connection is AWS's internal standby (monitored, not wired).

resource "aws_vpn_connection" "primary" {
  vpn_gateway_id      = aws_vpn_gateway.main.id
  customer_gateway_id = aws_customer_gateway.primary.id
  type                = "ipsec.1"
  static_routes_only  = true
  tags                = { Name = "pliac-vpn-primary" }
}

resource "aws_vpn_connection" "backup" {
  vpn_gateway_id      = aws_vpn_gateway.main.id
  customer_gateway_id = aws_customer_gateway.backup.id
  type                = "ipsec.1"
  static_routes_only  = true
  tags                = { Name = "pliac-vpn-backup" }
}

# Static routes — both connections need to know the Azure CIDR.
resource "aws_vpn_connection_route" "primary" {
  destination_cidr_block = var.azure_cidr
  vpn_connection_id      = aws_vpn_connection.primary.id
}

resource "aws_vpn_connection_route" "backup" {
  destination_cidr_block = var.azure_cidr
  vpn_connection_id      = aws_vpn_connection.backup.id
}

# ── Security Group ────────────────────────────────────────────────────────────

resource "aws_security_group" "open_sg" {
  name        = "pliac-open-sg"
  description = "Allow all inbound and outbound for research testing"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "pliac-open-sg" }
}

# ── Ubuntu AMI (latest 22.04 LTS in ap-southeast-2) ──────────────────────────

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ── Test VM ───────────────────────────────────────────────────────────────────
# Ubuntu 22.04 LTS — acts as the iperf3 SERVER.
# user_data (cloud-init) installs iperf3 and registers it as a systemd service.
# No firewall concerns: Linux allows ICMP and port 5201 by default.
# Boot + apt-get takes ~60-90 s; wait_for_vm_ready() polls until ICMP replies.

resource "aws_instance" "test_vm" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = "t3.micro"
  subnet_id     = aws_subnet.main.id

  vpc_security_group_ids = [aws_security_group.open_sg.id]

  user_data = <<-EOF
    #!/bin/bash
    apt-get update -y
    apt-get install -y iperf3

    cat > /etc/systemd/system/iperf3.service <<'UNIT'
    [Unit]
    Description=iperf3 server
    After=network.target

    [Service]
    ExecStart=/usr/bin/iperf3 -s --forceflush
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=multi-user.target
    UNIT

    systemctl daemon-reload
    systemctl enable iperf3
    systemctl start iperf3
  EOF

  tags = { Name = "pliac-aws-lin-vm" }
}
