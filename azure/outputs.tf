# Both public IPs — each fed into one AWS Customer Gateway by the orchestrator.
output "vpn_gateway_public_ip_1" {
  description = "Azure VPN Gateway public IP 1 — fed into AWS CGW primary"
  value       = azurerm_public_ip.vpn_gw_1.ip_address
}

output "vpn_gateway_public_ip_2" {
  description = "Azure VPN Gateway public IP 2 — fed into AWS CGW backup"
  value       = azurerm_public_ip.vpn_gw_2.ip_address
}

output "vnet_cidr" {
  value = var.vnet_cidr
}

output "connection_1_created" {
  description = "True when the primary VPN connection exists in state"
  value       = length(azurerm_virtual_network_gateway_connection.to_aws_primary) > 0
}

output "connection_2_created" {
  description = "True when the backup VPN connection exists in state"
  value       = length(azurerm_virtual_network_gateway_connection.to_aws_backup) > 0
}

output "azure_vm_private_ip" {
  description = "Private IP of the Azure Linux VM"
  value       = azurerm_network_interface.test_nic.private_ip_address
}

output "azure_vm_public_ip" {
  description = "Public IP of the Azure Linux VM (for internet access / SSH)"
  value       = azurerm_public_ip.test_vm.ip_address
}
