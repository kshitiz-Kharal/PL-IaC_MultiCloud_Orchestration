terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
}

resource "azurerm_resource_group" "main" {
  name     = "pliac-rg"
  location = var.region
}

resource "azurerm_virtual_network" "main" {
  name                = "pliac-vnet"
  address_space       = [var.vnet_cidr]
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_subnet" "main" {
  name                 = "pliac-subnet"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.subnet_cidr]
}

# Azure requires exactly "GatewaySubnet" as the name for VPN Gateway subnets.
resource "azurerm_subnet" "gateway" {
  name                 = "GatewaySubnet"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.gateway_subnet_cidr]
}

# ── Active-Active VPN Gateway — two public IPs ────────────────────────────────
# Active-active mode provides two simultaneous IPsec endpoints (one per IP).
# Each AWS Customer Gateway points to one of these IPs — no BGP ASN workaround
# is needed because the two IPs are genuinely distinct at the network layer.

resource "azurerm_public_ip" "vpn_gw_1" {
  name                = "pliac-vpn-gw-pip1"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1", "2", "3"]
}

resource "azurerm_public_ip" "vpn_gw_2" {
  name                = "pliac-vpn-gw-pip2"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1", "2", "3"]
}

resource "azurerm_virtual_network_gateway" "main" {
  name                = "pliac-vpn-gw"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  type                = "Vpn"
  vpn_type            = "RouteBased"
  sku                 = "VpnGw1AZ"
  active_active       = true # enables two simultaneous active connections
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

# ── Conditional Phase-3 resources ────────────────────────────────────────────
# count = 0 during Phase 1 (no AWS tunnel details yet).
# count = 1 during Phase 3 (orchestrator injects AWS tunnel IPs + PSKs).

# Primary connection — Azure pip1 ↔ AWS VPN Connection 1 Tunnel 1
resource "azurerm_local_network_gateway" "aws_primary" {
  count               = var.aws_tunnel_ip != "" ? 1 : 0
  name                = "pliac-lng-aws-primary"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  gateway_address     = var.aws_tunnel_ip
  address_space       = [var.aws_cidr]
}

resource "azurerm_virtual_network_gateway_connection" "to_aws_primary" {
  count                      = var.aws_tunnel_ip != "" ? 1 : 0
  name                       = "pliac-conn-to-aws-primary"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  type                       = "IPsec"
  virtual_network_gateway_id = azurerm_virtual_network_gateway.main.id
  local_network_gateway_id   = azurerm_local_network_gateway.aws_primary[0].id
  shared_key                 = var.aws_preshared_key
}

# Backup connection — Azure pip2 ↔ AWS VPN Connection 2 Tunnel 1
resource "azurerm_local_network_gateway" "aws_backup" {
  count               = var.aws_tunnel_ip_2 != "" ? 1 : 0
  name                = "pliac-lng-aws-backup"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  gateway_address     = var.aws_tunnel_ip_2
  address_space       = [var.aws_cidr]
}

resource "azurerm_virtual_network_gateway_connection" "to_aws_backup" {
  count                      = var.aws_tunnel_ip_2 != "" ? 1 : 0
  name                       = "pliac-conn-to-aws-backup"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  type                       = "IPsec"
  virtual_network_gateway_id = azurerm_virtual_network_gateway.main.id
  local_network_gateway_id   = azurerm_local_network_gateway.aws_backup[0].id
  shared_key                 = var.aws_preshared_key_2
}

# ── NSG ───────────────────────────────────────────────────────────────────────

resource "azurerm_network_security_group" "open_nsg" {
  name                = "pliac-open-nsg"
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

# ── Test VM public IP ─────────────────────────────────────────────────────────
# Required for outbound internet access (Azure default outbound SNAT retired
# Sep 2025). The VM uses this so cloud-init can run apt-get install iperf3.

resource "azurerm_public_ip" "test_vm" {
  name                = "pliac-vm-pip"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_network_interface" "test_nic" {
  name                = "pliac-azure-nic"
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

# ── Test VM ───────────────────────────────────────────────────────────────────
# Ubuntu 22.04 LTS — acts as the iperf3 CLIENT only.
# cloud-init installs iperf3 at first boot (~60-90 s total).
# The orchestrator triggers tests via az vm run-command invoke (RunShellScript).

resource "azurerm_linux_virtual_machine" "test_vm" {
  name                            = "pliac-az-vm"
  resource_group_name             = azurerm_resource_group.main.name
  location                        = azurerm_resource_group.main.location
  size                            = "Standard_D2s_v3"
  admin_username                  = "azureuser"
  admin_password                  = var.vm_admin_password
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
