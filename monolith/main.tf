# =============================================================================
# MONOLITHIC BASELINE — Both Azure and AWS in one Terraform configuration.
# Used as the comparison condition for Experiment 2 (fault injection).
#
# Key structural difference from the framework:
#   - One shared state file covers resources in both clouds.
#   - Cross-cloud references resolved directly by Terraform's dependency graph.
#   - No Python orchestration layer; no runtime variable injection.
#   - A failure in any resource affects the entire joint plan.
#   - Neither module can be applied, updated, or destroyed independently.
#
# This file intentionally mirrors the framework topology (active-active VPN,
# two VPN connections, iperf3 on AWS VM) so the comparison is fair.
# =============================================================================

terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "azurerm" {
  features {}
}

provider "aws" {
  region = "ap-southeast-2"
}

# ── Azure ─────────────────────────────────────────────────────────────────────

resource "azurerm_resource_group" "main" {
  name     = "pliac-mono-rg"
  location = "australiaeast"
}

resource "azurerm_virtual_network" "main" {
  name                = "pliac-mono-vnet"
  address_space       = ["10.2.0.0/16"]
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_subnet" "main" {
  name                 = "pliac-mono-subnet"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.2.1.0/24"]
}

resource "azurerm_subnet" "gateway" {
  name                 = "GatewaySubnet"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.2.255.0/27"]
}

# Active-active: two public IPs, two ip_configuration blocks
resource "azurerm_public_ip" "vpn_gw_1" {
  name                = "pliac-mono-vpn-gw-pip1"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1", "2", "3"]
}

resource "azurerm_public_ip" "vpn_gw_2" {
  name                = "pliac-mono-vpn-gw-pip2"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1", "2", "3"]
}

resource "azurerm_virtual_network_gateway" "main" {
  name                = "pliac-mono-vpn-gw"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  type                = "Vpn"
  vpn_type            = "RouteBased"
  sku                 = "VpnGw1AZ"
  active_active       = true
  enable_bgp          = false

  ip_configuration {
    name                          = "gwConfig1"
    public_ip_address_id          = azurerm_public_ip.vpn_gw_1.id
    private_ip_address_allocation = "Dynamic"
    subnet_id                     = azurerm_subnet.gateway.id
  }

  ip_configuration {
    name                          = "gwConfig2"
    public_ip_address_id          = azurerm_public_ip.vpn_gw_2.id
    private_ip_address_allocation = "Dynamic"
    subnet_id                     = azurerm_subnet.gateway.id
  }
}

# Cross-cloud reference: LNGs point directly to AWS tunnel IPs via Terraform graph.
# In the monolith no Python injection is needed — but no isolation exists either.
resource "azurerm_local_network_gateway" "aws_primary" {
  name                = "pliac-mono-lng-primary"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  gateway_address     = aws_vpn_connection.primary.tunnel1_address
  address_space       = ["10.1.0.0/16"]
}

resource "azurerm_local_network_gateway" "aws_backup" {
  name                = "pliac-mono-lng-backup"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  gateway_address     = aws_vpn_connection.backup.tunnel1_address
  address_space       = ["10.1.0.0/16"]
}

resource "azurerm_virtual_network_gateway_connection" "to_aws_primary" {
  name                       = "pliac-mono-conn-primary"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  type                       = "IPsec"
  virtual_network_gateway_id = azurerm_virtual_network_gateway.main.id
  local_network_gateway_id   = azurerm_local_network_gateway.aws_primary.id
  shared_key                 = aws_vpn_connection.primary.tunnel1_preshared_key
}

resource "azurerm_virtual_network_gateway_connection" "to_aws_backup" {
  name                       = "pliac-mono-conn-backup"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  type                       = "IPsec"
  virtual_network_gateway_id = azurerm_virtual_network_gateway.main.id
  local_network_gateway_id   = azurerm_local_network_gateway.aws_backup.id
  shared_key                 = aws_vpn_connection.backup.tunnel1_preshared_key
}

resource "azurerm_network_security_group" "open_nsg" {
  name                = "pliac-mono-open-nsg"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  security_rule {
    name                       = "AllowAllInbound"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowAllOutbound"
    priority                   = 100
    direction                  = "Outbound"
    access                     = "Allow"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_public_ip" "test_vm" {
  name                = "pliac-mono-vm-pip"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_network_interface" "test_nic" {
  name                = "pliac-mono-azure-nic"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.main.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.test_vm.id
  }
}

resource "azurerm_network_interface_security_group_association" "test" {
  network_interface_id      = azurerm_network_interface.test_nic.id
  network_security_group_id = azurerm_network_security_group.open_nsg.id
}

resource "azurerm_linux_virtual_machine" "test_vm" {
  name                            = "pliac-mono-az-vm"
  resource_group_name             = azurerm_resource_group.main.name
  location                        = azurerm_resource_group.main.location
  size                            = "Standard_D2s_v3"
  admin_username                  = "azureuser"
  admin_password                  = "ResearchLab!2026"
  disable_password_authentication = false
  network_interface_ids           = [azurerm_network_interface.test_nic.id]

  custom_data = base64encode(<<-INIT
    #!/bin/bash
    apt-get update -y
    apt-get install -y iperf3
    INIT
  )

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }
}

# ── AWS ───────────────────────────────────────────────────────────────────────

resource "aws_vpc" "main" {
  cidr_block           = "10.1.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "pliac-mono-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "pliac-mono-igw" }
}

resource "aws_route_table" "main" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "pliac-mono-rt" }
}

resource "aws_subnet" "main" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.1.1.0/24"
  availability_zone       = "ap-southeast-2a"
  map_public_ip_on_launch = true
  tags                    = { Name = "pliac-mono-subnet" }
}

resource "aws_route_table_association" "main" {
  subnet_id      = aws_subnet.main.id
  route_table_id = aws_route_table.main.id
}

resource "aws_vpn_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "pliac-mono-vgw" }
}

resource "aws_vpn_gateway_route_propagation" "main" {
  vpn_gateway_id = aws_vpn_gateway.main.id
  route_table_id = aws_route_table.main.id
}

# Cross-cloud reference: CGW IPs come directly from Azure public IP resources.
resource "aws_customer_gateway" "primary" {
  bgp_asn    = 65000
  ip_address = azurerm_public_ip.vpn_gw_1.ip_address
  type       = "ipsec.1"
  tags       = { Name = "pliac-mono-cgw-primary" }
}

resource "aws_customer_gateway" "backup" {
  bgp_asn    = 65000
  ip_address = azurerm_public_ip.vpn_gw_2.ip_address
  type       = "ipsec.1"
  tags       = { Name = "pliac-mono-cgw-backup" }
}

resource "aws_vpn_connection" "primary" {
  vpn_gateway_id      = aws_vpn_gateway.main.id
  customer_gateway_id = aws_customer_gateway.primary.id
  type                = "ipsec.1"
  static_routes_only  = true
  tags                = { Name = "pliac-mono-vpn-primary" }
}

resource "aws_vpn_connection" "backup" {
  vpn_gateway_id      = aws_vpn_gateway.main.id
  customer_gateway_id = aws_customer_gateway.backup.id
  type                = "ipsec.1"
  static_routes_only  = true
  tags                = { Name = "pliac-mono-vpn-backup" }
}

resource "aws_vpn_connection_route" "primary" {
  destination_cidr_block = "10.2.0.0/16"
  vpn_connection_id      = aws_vpn_connection.primary.id
}

resource "aws_vpn_connection_route" "backup" {
  destination_cidr_block = "10.2.0.0/16"
  vpn_connection_id      = aws_vpn_connection.backup.id
}

resource "aws_security_group" "open_sg" {
  name        = "pliac-mono-open-sg"
  description = "Allow all traffic for research"
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
  tags = { Name = "pliac-mono-open-sg" }
}

resource "aws_instance" "test_vm" {
  ami                    = "ami-0111f46977d33b84b" # Ubuntu 22.04 LTS ap-southeast-2
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.main.id
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

  tags = { Name = "pliac-mono-aws-lin-vm" }
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "azure_vm_private_ip" { value = azurerm_network_interface.test_nic.private_ip_address }

output "aws_vm_private_ip" { value = aws_instance.test_vm.private_ip }
output "azure_gateway_ip_1" { value = azurerm_public_ip.vpn_gw_1.ip_address }
output "azure_gateway_ip_2" { value = azurerm_public_ip.vpn_gw_2.ip_address }
output "aws_tunnel_1_address" { value = aws_vpn_connection.primary.tunnel1_address }
output "aws_tunnel_3_address" { value = aws_vpn_connection.backup.tunnel1_address }
