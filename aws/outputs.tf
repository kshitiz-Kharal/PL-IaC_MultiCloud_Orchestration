output "vpc_cidr" {
  value = aws_vpc.main.cidr_block
}

output "aws_vm_private_ip" {
  value = aws_instance.test_vm.private_ip
}

# ── VPN Connection IDs ────────────────────────────────────────────────────────
# Used by benchmark.py to poll tunnel Up/Down status via AWS CLI.

output "vpn_connection_1_id" {
  description = "Primary VPN Connection ID — used to poll tunnel telemetry"
  value       = aws_vpn_connection.primary.id
}

output "vpn_connection_2_id" {
  description = "Backup VPN Connection ID — used to poll tunnel telemetry"
  value       = aws_vpn_connection.backup.id
}

# ── VPN Connection 1 tunnels ──────────────────────────────────────────────────
# Tunnel 1 = primary active (wired to Azure Connection 1 via LNG)
# Tunnel 2 = AWS-managed internal HA standby (monitored but not wired to Azure)

output "conn1_tunnel1_address" {
  description = "VPN Conn1 Tunnel1 outside IP — fed to Azure LNG primary"
  value       = aws_vpn_connection.primary.tunnel1_address
}

output "conn1_tunnel1_preshared_key" {
  description = "VPN Conn1 Tunnel1 PSK — fed to Azure Connection primary"
  value       = aws_vpn_connection.primary.tunnel1_preshared_key
  sensitive   = true
}

output "conn1_tunnel2_address" {
  description = "VPN Conn1 Tunnel2 outside IP — AWS internal HA, logged only"
  value       = aws_vpn_connection.primary.tunnel2_address
}

output "conn1_tunnel2_preshared_key" {
  description = "VPN Conn1 Tunnel2 PSK — AWS internal HA, logged only"
  value       = aws_vpn_connection.primary.tunnel2_preshared_key
  sensitive   = true
}

# ── VPN Connection 2 tunnels ──────────────────────────────────────────────────
# Tunnel 1 = backup active (wired to Azure Connection 2 via LNG)
# Tunnel 2 = AWS-managed internal HA standby for backup path

output "conn2_tunnel1_address" {
  description = "VPN Conn2 Tunnel1 outside IP — fed to Azure LNG backup"
  value       = aws_vpn_connection.backup.tunnel1_address
}

output "conn2_tunnel1_preshared_key" {
  description = "VPN Conn2 Tunnel1 PSK — fed to Azure Connection backup"
  value       = aws_vpn_connection.backup.tunnel1_preshared_key
  sensitive   = true
}

output "conn2_tunnel2_address" {
  description = "VPN Conn2 Tunnel2 outside IP — AWS internal HA, logged only"
  value       = aws_vpn_connection.backup.tunnel2_address
}

output "conn2_tunnel2_preshared_key" {
  description = "VPN Conn2 Tunnel2 PSK — AWS internal HA, logged only"
  value       = aws_vpn_connection.backup.tunnel2_preshared_key
  sensitive   = true
}
