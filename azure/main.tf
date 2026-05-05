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

resource "azurerm_public_ip" "vpn_gw" {
  name                = "pliac-vpn-gw-pip"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["1", "2", "3"] #Zone Redundancy
}

resource "azurerm_virtual_network_gateway" "main" {
  name                = "pliac-vpn-gw"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  type                = "Vpn"
  vpn_type            = "RouteBased"
  sku                 = "VpnGw1AZ"
  active_active       = false
  enable_bgp          = false

  ip_configuration {
    name                          = "gwConfig"
    public_ip_address_id          = azurerm_public_ip.vpn_gw.id
    private_ip_address_allocation = "Dynamic"
    subnet_id                     = azurerm_subnet.gateway.id
  }
}

# Created only in Phase 2 when the orchestrator has injected the AWS tunnel IP.
resource "azurerm_local_network_gateway" "aws" {
  count               = var.aws_tunnel_ip != "" ? 1 : 0
  name                = "pliac-lng-aws"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  gateway_address     = var.aws_tunnel_ip
  address_space       = [var.aws_cidr]
}

resource "azurerm_virtual_network_gateway_connection" "to_aws" {
  count                      = var.aws_tunnel_ip != "" ? 1 : 0
  name                       = "pliac-conn-to-aws"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  type                       = "IPsec"
  virtual_network_gateway_id = azurerm_virtual_network_gateway.main.id
  local_network_gateway_id   = azurerm_local_network_gateway.aws[0].id
  shared_key                 = var.aws_preshared_key
}


# network security group and its association
resource "azurerm_network_security_group" "open_nsg" {
  name                = "pliac-open-nsg"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  # Allow all inbound traffic (for testing connectivity)
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

  # Allow all outbound traffic
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

# Associate the NSG with your network interface
resource "azurerm_network_interface_security_group_association" "test" {
  network_interface_id      = azurerm_network_interface.test_nic.id
  network_security_group_id = azurerm_network_security_group.open_nsg.id
}

# nsg association
resource "azurerm_network_interface" "test_nic" {
  name                = "pliac-azure-nic"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.main.id
    private_ip_address_allocation = "Dynamic"
  }
}

# virtual machine
resource "azurerm_windows_virtual_machine" "test_vm" {
  name                = "pliac-win-vm"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  size                = "Standard_D2als_v7" # The cheapest size from your search
  admin_username      = "azureuser"
  admin_password      = "ResearchLab!2026"

  # ── Spot Configuration ──────────────────────────────────────────────────
  priority        = "Spot"
  eviction_policy = "Deallocate" 
  # max_bid_price   = -1 # Pay up to the standard price to stay running

  network_interface_ids = [azurerm_network_interface.test_nic.id]

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "MicrosoftWindowsDesktop"
    offer     = "Windows-11"
    sku       = "win11-24h2-pro"
    version   = "latest"
  }
}

output "azure_vm_private_ip" {
  description = "Private IP of the Azure Windows VM"
  value       = azurerm_network_interface.test_nic.private_ip_address
}
