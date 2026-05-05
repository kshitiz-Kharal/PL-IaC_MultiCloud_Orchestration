output "vpn_gateway_public_ip" {
  description = "Azure VPN Gateway public IP — fed into AWS Customer Gateway by orchestrator"
  value       = azurerm_public_ip.vpn_gw.ip_address
}

output "vnet_cidr" {
  value = var.vnet_cidr
}

output "connection_created" {
  value = length(azurerm_virtual_network_gateway_connection.to_aws) > 0
}
