output "vpc_cidr" {
  value = aws_vpc.main.cidr_block
}

output "tunnel1_address" {
  description = "AWS VPN tunnel outside IP — fed into Azure Local Network Gateway"
  value       = aws_vpn_connection.to_azure.tunnel1_address
}

output "tunnel1_preshared_key" {
  description = "AWS-generated PSK — fed into Azure VPN Connection"
  value       = aws_vpn_connection.to_azure.tunnel1_preshared_key
  sensitive   = true
}
