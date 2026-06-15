variable "region" {
  default = "australiaeast"
}

variable "vnet_cidr" {
  default = "10.2.0.0/16"
}

variable "subnet_cidr" {
  default = "10.2.1.0/24"
}

variable "gateway_subnet_cidr" {
  default = "10.2.255.0/27"
}

# ── Phase 3 variables — injected by the orchestrator ─────────────────────────

# Primary tunnel: Azure pip1 ↔ AWS VPN Connection 1 Tunnel 1
variable "aws_tunnel_ip" {
  description = "AWS VPN Connection 1 Tunnel 1 outside IP (primary)"
  type        = string
  default     = ""
}

variable "aws_preshared_key" {
  description = "AWS VPN Connection 1 Tunnel 1 PSK (primary)"
  type        = string
  default     = ""
  sensitive   = true
}

# Backup tunnel: Azure pip2 ↔ AWS VPN Connection 2 Tunnel 1
variable "aws_tunnel_ip_2" {
  description = "AWS VPN Connection 2 Tunnel 1 outside IP (backup)"
  type        = string
  default     = ""
}

variable "aws_preshared_key_2" {
  description = "AWS VPN Connection 2 Tunnel 1 PSK (backup)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "aws_cidr" {
  default = "10.1.0.0/16"
}

variable "vm_admin_password" {
  description = "Admin password for the Azure test VM. Set via AZURE_VM_PASSWORD env var."
  type        = string
  sensitive   = true
}
