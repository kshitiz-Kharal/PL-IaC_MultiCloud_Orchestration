# !/usr/bin/env python3
"""
Hybrid PL-IaC Orchestrator
---------------------------
Three-phase control plane that connects a decoupled Azure VNet and AWS VPC
over an IPsec VPN tunnel without any hardcoded cross-cloud secrets.

Phase 1  Deploy Azure VNet + VPN Gateway → extract Gateway public IP
Phase 2  Inject Azure IP → deploy AWS VPC + Customer Gateway + VPN Connection
         → extract tunnel IP and PSK from live AWS state
Phase 3  Inject AWS tunnel details → complete Azure Local Network Gateway
         + IPsec Connection
"""

import json
import os
import subprocess
import sys
from pathlib import Path
import time

BASE   = Path(__file__).parent
AWS    = BASE / "aws"
AZURE  = BASE / "azure"


# ── helpers ───────────────────────────────────────────────────────────────────

def tf(cmd: list, cwd: Path) -> None:
    """Run a Terraform command, streaming output to the terminal."""
    print(f"\n>>> terraform {' '.join(cmd)}  [{cwd.name}]")
    result = subprocess.run(["terraform"] + cmd, cwd=cwd)
    if result.returncode != 0:
        sys.exit(f"Terraform failed: terraform {' '.join(cmd)} in {cwd.name}")


def tf_output(cwd: Path) -> dict:
    print("-------TF_OUTPUT-----------")
    """Return terraform outputs as a plain key→value dict."""
    result = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit(f"terraform output failed in {cwd.name}: {result.stderr}")
    return {k: v["value"] for k, v in json.loads(result.stdout).items()}


def write_tfvars(path: Path, variables: dict) -> None:
    """Write a terraform.tfvars file from a dict — no hardcoding required."""
    lines = [
        f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}"
        for k, v in variables.items()
    ]
    path.write_text("\n".join(lines) + "\n")
    print(f"Wrote {path.name}: {list(variables.keys())}")


# ── phase 1: azure network + vpn gateway ─────────────────────────────────────

def phase1_azure() -> str:
    """
    Deploy Azure VNet, GatewaySubnet, and VPN Gateway.
    Returns the Gateway public IP for use by AWS in Phase 2.
    """
    print("\n=== PHASE 1: Azure VNet + VPN Gateway ===")

    # Remove any leftover Phase-2 vars so the LNG/connection aren't created yet.
    tfvars = AZURE / "terraform.tfvars"
    if tfvars.exists():
        tfvars.unlink()

    tf(["init", "-input=false"], AZURE)
    tf(["apply", "-input=false", "-auto-approve"], AZURE)

    outputs = tf_output(AZURE)
    gateway_ip = outputs["vpn_gateway_public_ip"]
    print(f"\nAzure Gateway IP: {gateway_ip}")
    return gateway_ip


# ── phase 2: aws vpc + customer gateway + vpn connection ─────────────────────

def phase2_aws(azure_gateway_ip: str) -> tuple[str, str, str]:
    """
    Inject Azure IP, deploy AWS VPC + Customer Gateway + VPN Connection.
    Returns (tunnel_ip, preshared_key, vpc_cidr) extracted from live state.
    """
    print("\n=== PHASE 2: AWS VPC + Customer Gateway + VPN Connection ===")

    write_tfvars(AWS / "terraform.tfvars", {"azure_gateway_ip": azure_gateway_ip})

    tf(["init", "-input=false"], AWS)
    tf(["apply", "-input=false", "-auto-approve"], AWS)

    outputs   = tf_output(AWS)
    tunnel_ip = outputs["tunnel1_address"]
    psk       = outputs["tunnel1_preshared_key"]
    vpc_cidr  = outputs["vpc_cidr"]
    print(f"\nAWS tunnel IP: {tunnel_ip}  |  VPC CIDR: {vpc_cidr}")
    return tunnel_ip, psk, vpc_cidr


# ── phase 3: azure local network gateway + ipsec connection ──────────────────

def phase3_azure_connect(tunnel_ip: str, psk: str, aws_cidr: str) -> None:
    """
    Inject AWS tunnel details into Azure.
    Creates the Local Network Gateway and IPsec Connection (count switches 0→1).
    The PSK stays in memory only — never written to source files.
    """
    print("\n=== PHASE 3: Azure Local Network Gateway + IPsec Connection ===")

    write_tfvars(
        AZURE / "terraform.tfvars",
        {"aws_tunnel_ip": tunnel_ip, "aws_preshared_key": psk, "aws_cidr": aws_cidr},
    )

    tf(["apply", "-input=false", "-auto-approve"], AZURE)

    outputs = tf_output(AZURE)
    print(f"\nConnection created: {outputs.get('connection_created', False)}")


# ── teardown ──────────────────────────────────────────────────────────────────

def destroy() -> None:
    """Tear down all infrastructure. Azure connection first, then AWS."""
    print("\n=== TEARDOWN ===")

    if (AZURE / "terraform.tfvars").exists():
        (AZURE / "terraform.tfvars").unlink()
    tf(["destroy", "-input=false", "-auto-approve"], AZURE)

    if (AWS / "terraform.tfvars").exists():
        (AWS / "terraform.tfvars").unlink()
    tf(["destroy", "-input=false", "-auto-approve"], AWS)


#---- Pinging from azure to aws for connectivity check--------------------------------------
def test_connectivity(aws_vm_ip: str) -> None:
    """
    Triggers the Azure VM to ping the AWS VM private IP using Azure Run-Command.
    """
    print(f"\n=== PHASE 4: Data Plane Verification (Ping) ===")
    print(f"Instructing Azure VM to ping AWS Endpoint: {aws_vm_ip}...")

    # Azure CLI command to execute PowerShell inside the Windows VM
    cmd = (
        f'az vm run-command invoke '
        f'--resource-group pliac-rg '
        f'--name pliac-win-vm '
        f'--command-id RunPowerShellScript '
        f'--scripts "ping -n 4 {aws_vm_ip}"'
    )

    try:
        # Run command and capture JSON output
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, shell=True)
        raw_output = json.loads(result.stdout)
        
        # Extract the message from the Azure JSON response
        ping_results = raw_output['value'][0]['message']
        
        print("\n--- Remote Ping Output ---")
        print(ping_results)
        print("--------------------------")

        if "Reply from" in ping_results:
            print("\n✓ SUCCESS: Ping successful! Data Plane is operational.")
        else:
            print("\n FAILED: No replies received. Check Windows Firewalls.")
            
    except Exception as e:
        print(f"\n ERROR: Connectivity test failed: {e}")

# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if "--destroy" in sys.argv:
        destroy()
        return

    azure_ip                  = phase1_azure()
    tunnel_ip, psk, vpc_cidr  = phase2_aws(azure_ip)
    phase3_azure_connect(tunnel_ip, psk, vpc_cidr)

    print("\n✓ IPsec VPN tunnel established between Azure and AWS.")


    # New: Data Plane Test
    print("\nWaiting 300 seconds for VPN handshake and Windows boot...")
    time.sleep(300)

    # Fetch AWS Private IP from AWS outputs
    aws_outputs = tf_output(AWS)
    aws_private_ip = aws_outputs.get("aws_vm_private_ip")

    if aws_private_ip:
        test_connectivity(aws_private_ip)
    else:
        print(" ERROR: Could not find 'aws_vm_private_ip' in AWS outputs.")

    print("\n✓ Orchestration Complete. Run with --destroy to clean up.")

    print("  Run with --destroy to tear everything down.")


if __name__ == "__main__":
    main()
