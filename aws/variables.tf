variable "region" {
  default = "ap-southeast-2"
}

variable "vpc_cidr" {
  default = "10.1.0.0/16"
}

variable "subnet_cidr" {
  default = "10.1.1.0/24"
}

# Primary Azure VPN Gateway public IP — maps to Customer Gateway primary.
variable "azure_gateway_ip" {
  description = "Azure VPN Gateway public IP 1 (active-active pip1) — written by orchestrator"
  type        = string
  default     = ""
}

# Backup Azure VPN Gateway public IP — maps to Customer Gateway backup.
variable "azure_gateway_ip_2" {
  description = "Azure VPN Gateway public IP 2 (active-active pip2) — written by orchestrator"
  type        = string
  default     = ""
}

variable "azure_cidr" {
  default = "10.2.0.0/16"
}
