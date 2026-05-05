# Hybrid PL-IaC Orchestrator — Proof of Concept

A first step towards fully automated multi-cloud orchestration. This proof-of-concept demonstrates that a lightweight Python control plane can automatically exchange live network variables between two isolated Terraform modules — replacing the manual, error-prone process of connecting decoupled cloud environments.

The orchestrator provisions an isolated AWS VPC and an isolated Azure VNet, then dynamically extracts and injects the routing variables required to establish a secure site-to-site **IPsec VPN tunnel** between them — without any hardcoded cross-cloud secrets or manual intervention.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│           Control Plane (Python)                │
│                                                 │
│  orchestrator.py — API-Driven State Manager     │
│  1. Triggers Azure  5. Injects AWS IP & PSK     │
│  2. Extracts Azure IP    4. Extracts AWS IP & PSK│
│  3. Injects Azure IP & Triggers AWS             │
└──────────┬──────────────────────┬───────────────┘
           │                      │
    ┌──────▼──────┐        ┌──────▼──────┐
    │  Terraform  │        │  Terraform  │
    │Azure Module │        │ AWS Module  │
    │Isolated     │        │ Isolated    │
    │   State     │        │   State     │
    └──────┬──────┘        └──────┬──────┘
           │                      │
    ┌──────▼──────────────────────▼──────┐
    │            Data Plane              │
    │                                    │
    │  Azure VNet          AWS VPC       │
    │  VPN Gateway ◄──────► VPN Gateway │
    │  Windows VM   IPsec   Windows VM  │
    │           Tunnel (Layer 3)         │
    └────────────────────────────────────┘
```

The Python script acts as the **SDN-style control plane**, while Terraform handles the **declarative execution plane**. The two Terraform modules maintain completely isolated state files — neither cloud environment has any direct knowledge of the other.

---

## How It Works

The orchestrator runs in four sequential phases:

**Phase 1 — Deploy Azure**
Provisions the Azure VNet, workload subnet, GatewaySubnet, and VPN Gateway. Once Terraform finishes, the orchestrator reads the Gateway's public IP directly from the live Terraform state.

**Phase 2 — Deploy AWS**
Writes the Azure Gateway IP into an AWS `terraform.tfvars` file and provisions the AWS VPC, VPN Gateway, Customer Gateway (pointing at Azure), and VPN Connection. The orchestrator then reads the AWS tunnel IP and the auto-generated pre-shared key (PSK) from the live AWS state — the PSK never touches any source file.

**Phase 3 — Complete Azure Connection**
Writes the AWS tunnel IP and PSK into an Azure `terraform.tfvars` file and re-applies the Azure module. This triggers the creation of the Local Network Gateway and IPsec Connection, completing the tunnel.

**Phase 4 — Data Plane Verification**
Waits 300 seconds for the VPN handshake and Windows VMs to finish booting, then uses the Azure CLI `run-command` to instruct the Azure VM to ping the AWS VM's private IP over the tunnel.

```
python orchestrator.py           # deploy + verify
python orchestrator.py --destroy # tear everything down
```

---

## Project Structure

```
├── aws/
│   ├── main.tf        # VPC, subnet, VPN Gateway, Customer Gateway, VPN Connection
│   ├── variables.tf   # azure_gateway_ip (injected at runtime), CIDRs
│   └── outputs.tf     # tunnel1_address, tunnel1_preshared_key, vpc_cidr
│
├── azure/
│   ├── main.tf        # VNet, GatewaySubnet, VPN Gateway, LNG + Connection (conditional)
│   ├── variables.tf   # aws_tunnel_ip, aws_preshared_key (injected at runtime)
│   └── outputs.tf     # vpn_gateway_public_ip, connection_created
│
├── orchestrator.py    # Python control plane — all four phases
├── requirements.txt   # No pip packages; lists system tool prerequisites
└── README.md
```

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | 3.9+ | Run orchestrator.py |
| Terraform CLI | 1.5+ | Provision cloud infrastructure |
| Azure CLI (`az`) | Latest | Phase 4 connectivity test via `run-command` |
| AWS account | — | ap-southeast-2 (Sydney) |
| Azure account | — | Australia East |

**Python** — https://www.python.org/downloads/  
**Terraform** — https://developer.hashicorp.com/terraform/install  
**Azure CLI** — https://learn.microsoft.com/en-us/cli/azure/install-azure-cli

No pip packages are required. `orchestrator.py` uses only Python standard-library modules.

---

## Setup

### 1. AWS credentials

```bash
export AWS_ACCESS_KEY_ID="your-access-key"
export AWS_SECRET_ACCESS_KEY="your-secret-key"
export AWS_DEFAULT_REGION="ap-southeast-2"
```

### 2. Azure credentials

```bash
az login
```

Or with a service principal (for CI/CD):

```bash
az login --service-principal \
  -u $ARM_CLIENT_ID \
  -p $ARM_CLIENT_SECRET \
  --tenant $ARM_TENANT_ID

export ARM_SUBSCRIPTION_ID="your-subscription-id"
export ARM_CLIENT_ID="your-client-id"
export ARM_CLIENT_SECRET="your-client-secret"
export ARM_TENANT_ID="your-tenant-id"
```

### 3. Accept the Windows 11 Marketplace terms (Azure — one-time)

Azure requires you to accept the legal terms for the Windows 11 marketplace image before the first deployment. Run this once in your subscription:

```bash
az vm image terms accept \
  --publisher MicrosoftWindowsDesktop \
  --offer Windows-11 \
  --plan win11-24h2-pro
```

### 4. Run

```bash
python orchestrator.py
```

To tear down all resources afterwards:

```bash
python orchestrator.py --destroy
```

---

## Network Topology

| | AWS | Azure |
|---|---|---|
| **Region** | ap-southeast-2 (Sydney) | Australia East |
| **Network CIDR** | 10.1.0.0/16 | 10.2.0.0/16 |
| **Subnet** | 10.1.1.0/24 | 10.2.1.0/24 |
| **Gateway Subnet** | — | 10.2.255.0/27 |
| **VPN Gateway SKU** | Standard | VpnGw1AZ (zone-redundant) |
| **Test VM** | Windows Server 2022 (Spot, t3.micro) | Windows 11 Pro (Spot, Standard_D2als_v7) |
| **Tunnel Protocol** | IPsec / IKE | IPsec / IKE |
| **Routing** | Static | Route-based |

---

## ⚠️ Security Warnings (Research Use Only)

This is a proof-of-concept for academic research. **Do not deploy this in a production environment.**

### Hardcoded credentials

The Azure VM password is hardcoded in `azure/main.tf`:

```hcl
admin_password = "ResearchLab!2026"
```

In production, this must be replaced with a secret manager reference (Azure Key Vault, AWS Secrets Manager, or a Terraform variable marked `sensitive = true` loaded from a secure store — never committed to source control).

### Open security groups and NSGs

Both the AWS Security Group and the Azure NSG are configured to allow **all inbound and outbound traffic** (`0.0.0.0/0`):

```hcl
# AWS — open_sg
protocol    = "-1"
cidr_blocks = ["0.0.0.0/0"]

# Azure — open_nsg
source_address_prefix      = "*"
destination_address_prefix = "*"
```

This is intentional for research testing so ICMP ping works without firewall interference. In production, restrict rules to the minimum required CIDRs and ports.

### Windows Firewall disabled via user_data

The AWS Windows VM's user_data script disables the Windows Firewall entirely:

```xml
<powershell>
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled False
</powershell>
```

This is done so ICMP ping replies work immediately without waiting for GPO or manual configuration. In production, keep the Windows Firewall enabled and create specific inbound ICMP rules.

### Hardcoded AMI ID

The AWS Windows Server 2022 AMI is hardcoded:

```hcl
ami = "ami-094281959696a6b6c"  # ap-southeast-2 only
```

This AMI ID is region-specific and will become outdated as AWS releases new AMI versions. If this AMI ID is no longer valid, replace it with the latest Windows Server 2022 Base AMI for ap-southeast-2 from the AWS Console under EC2 → AMI Catalog.

---

## Known Limitations

- **Azure VPN Gateway takes 30–45 minutes to provision.** This is a fixed Azure platform constraint. The orchestrator waits synchronously — there is no timeout or retry logic.
- **The 300-second wait in Phase 4 is an estimate.** If the Windows VMs are slow to boot (common with Spot instances), the ping may run before the OS is fully ready. If Phase 4 reports failure, wait a few minutes and manually run `az vm run-command invoke` to re-test.
- **`Standard_D2als_v7` may not be available in Australia East.** If the Azure apply fails on the VM resource, change the size to `Standard_D2s_v3` in `azure/main.tf`.
- **Single tunnel only.** The orchestrator uses AWS tunnel 1. AWS VPN Connections provide two tunnels for redundancy — tunnel 2 is provisioned but unused.
- **No state backend.** Terraform state is stored locally. If the orchestrator is interrupted mid-run, re-running it should be safe but may require manual `terraform destroy` on one or both modules.

---

## What You Should Also Add

### 1. `.gitignore` — critical before pushing to GitHub

The orchestrator writes `terraform.tfvars` files at runtime. The Phase 3 Azure tfvars file **contains the VPN pre-shared key**. You must gitignore these files:

```gitignore
# Terraform state (contains sensitive outputs including the PSK)
*.tfstate
*.tfstate.backup

# Runtime-injected variable files (contain PSK in Phase 3)
**/terraform.tfvars

# Provider plugins (large binary downloads)
**/.terraform/
**/.terraform.lock.hcl
```

### 2. Terraform remote backend (for team use)

Currently both modules store state locally. If multiple people run this, state files will conflict. Add an S3 backend (AWS) or Azure Storage backend to share state safely:

```hcl
# aws/main.tf — add inside the terraform {} block
backend "s3" {
  bucket = "your-tfstate-bucket"
  key    = "pliac/aws/terraform.tfstate"
  region = "ap-southeast-2"
}
```

### 3. A GitHub Actions workflow (for CI/CD)

To run the orchestrator automatically on every push, add a `.github/workflows/deploy.yml` that sets the AWS and Azure credentials as GitHub Secrets and calls `python orchestrator.py`.

---

## Cost Estimate

Running one full deploy costs approximately:

| Resource | Cost |
|---|---|
| Azure VpnGw1AZ | ~$0.35 / hour |
| AWS VPN Connection | ~$0.05 / hour |
| AWS t3.micro Spot | ~$0.005 / hour |
| Azure Standard_D2als_v7 Spot | ~$0.04 / hour |
| **Total** | **~$0.45 / hour** |

Always run `python orchestrator.py --destroy` when finished to avoid ongoing charges.

---

## References

This implementation is a proof-of-concept for the research paper:  
*"Hybrid PL-IaC Approach for Automated Modular Orchestration of Decoupled Multi-Cloud Networks"*

Key design decisions follow:
- Hauser et al. (2020) — SDN-inspired decoupled control/data plane
- Rahman et al. (2019) — avoiding hardcoded secrets ("security smells")
- Pahl et al. (2020) — eliminating configuration drift via programmatic state injection
- Sokolowski et al. (2023) — decentralised IaC modularisation
