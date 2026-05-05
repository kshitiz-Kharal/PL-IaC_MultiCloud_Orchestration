variable "region" {
  default = "ap-southeast-2"
}

variable "vpc_cidr" {
  default = "10.1.0.0/16"
}

variable "subnet_cidr" {
  default = "10.1.1.0/24"
}

variable "azure_gateway_ip" {
  description = "Azure VPN Gateway public IP — written by the orchestrator"
  type        = string
}

variable "azure_cidr" {
  default = "10.2.0.0/16"
}
