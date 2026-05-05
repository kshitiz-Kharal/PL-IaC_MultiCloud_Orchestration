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

# Phase 2 — injected by the orchestrator after AWS is deployed.
variable "aws_tunnel_ip" {
  description = "AWS VPN tunnel 1 outside IP"
  type        = string
  default     = ""
}

variable "aws_preshared_key" {
  description = "AWS-generated PSK"
  type        = string
  default     = ""
  sensitive   = true
}

variable "aws_cidr" {
  default = "10.1.0.0/16"
}
