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

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = { Name = "pliac-vpc" }
}

resource "aws_subnet" "main" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.subnet_cidr
  availability_zone = "${var.region}a"
  tags              = { Name = "pliac-subnet" }
}

resource "aws_vpn_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "pliac-vgw" }
}

# Points to the Azure VPN Gateway public IP injected by the orchestrator.
resource "aws_customer_gateway" "azure" {
  bgp_asn    = 65000
  ip_address = var.azure_gateway_ip
  type       = "ipsec.1"
  tags       = { Name = "pliac-cgw-azure" }
}

resource "aws_vpn_connection" "to_azure" {
  vpn_gateway_id      = aws_vpn_gateway.main.id
  customer_gateway_id = aws_customer_gateway.azure.id
  type                = "ipsec.1"
  static_routes_only  = true
  tags                = { Name = "pliac-vpn-to-azure" }
}

resource "aws_vpn_connection_route" "azure" {
  destination_cidr_block = var.azure_cidr
  vpn_connection_id      = aws_vpn_connection.to_azure.id
}

# Without this, EC2 instances have no route to 10.2.0.0/16 even though the
# tunnel is up. Route propagation pushes VPN routes into the VPC's main
# route table so the Windows VM can actually reach Azure.
resource "aws_vpn_gateway_route_propagation" "main" {
  vpn_gateway_id = aws_vpn_gateway.main.id
  route_table_id = aws_vpc.main.main_route_table_id
}

resource "aws_security_group" "open_sg" {
  name        = "pliac-open-sg"
  description = "Allow all inbound and outbound for research testing"
  vpc_id      = aws_vpc.main.id

  # Allow all inbound
  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Allow all outbound
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "pliac-open-sg" }
}

resource "aws_instance" "test_vm" {
  # This finds the latest Windows Server 2022 Base AMI
  ami           = "ami-094281959696a6b6c" # Standard Windows 2022 in ap-southeast-2
  instance_type = "t3.micro" 
  subnet_id     = aws_subnet.main.id

  # Associate the Open Security Group
  vpc_security_group_ids = [aws_security_group.open_sg.id]

  # ── Spot Configuration ──────────────────────────────────────────────────
  instance_market_options {
    market_type = "spot"
    spot_options {
      max_price = "0.02" # Matches your low cost target
    }
  }

  # Disables the internal Windows Firewall so pings work immediately
  user_data = <<EOF
<powershell>
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled False
</powershell>
EOF

  tags = { Name = "pliac-aws-win-vm" }
}

output "aws_vm_private_ip" {
  value = aws_instance.test_vm.private_ip
}