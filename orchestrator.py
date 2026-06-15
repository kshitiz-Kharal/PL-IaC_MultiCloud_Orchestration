
"""
Hybrid PL-IaC Orchestrator
---------------------------
Three-phase control plane that connects a decoupled Azure VNet and AWS VPC
over two simultaneous IPsec VPN tunnels (active-active architecture).

Phase 1  Deploy Azure VNet + active-active VPN Gateway
         → extract two public IPs (pip1, pip2)

Phase 2  Inject both Azure IPs → deploy AWS VPC + two Customer Gateways
         + two VPN Connections → extract all tunnel IPs and PSKs

Phase 3  Inject AWS tunnel details → complete Azure Local Network Gateways
         + two IPsec Connections (primary and backup)

Phase 4  Active tunnel-convergence polling → verify data plane via ICMP
         (50 pkt) and iperf3 throughput tests (TCP bidirectional + UDP)

Tunnels provisioned:
  Tunnel 1 (primary)  — Azure pip1 ↔ AWS VPN Connection 1 Tunnel 1
  Tunnel 2 (HA)       — AWS VPN Connection 1 Tunnel 2 (AWS-managed standby)
  Tunnel 3 (backup)   — Azure pip2 ↔ AWS VPN Connection 2 Tunnel 1
  Tunnel 4 (HA)       — AWS VPN Connection 2 Tunnel 2 (AWS-managed standby)
"""

import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

BASE  = Path(__file__).parent
AWS   = BASE / "aws"
AZURE = BASE / "azure"

RESOURCE_GROUP = "pliac-rg"
AZURE_VM_NAME  = "pliac-az-vm"


# ── helpers ───────────────────────────────────────────────────────────────────

def tf(cmd: list, cwd: Path) -> float:
    """
    Run a Terraform command. Returns elapsed seconds (apply time only counts
    as provisioning latency in benchmark.py). Raises RuntimeError on failure
    so callers can catch it rather than sys.exit() killing the benchmark loop.
    """
    print(f"\n>>> terraform {' '.join(cmd)}  [{cwd.name}]")
    t0     = time.perf_counter()
    result = subprocess.run(["terraform"] + cmd, cwd=cwd)
    elapsed = time.perf_counter() - t0
    if result.returncode != 0:
        raise RuntimeError(
            f"terraform {' '.join(cmd)} in {cwd.name} (rc={result.returncode})"
        )
    return elapsed


def tf_output(cwd: Path) -> dict:
    """Return terraform outputs as a plain key→value dict (sensitive values included)."""
    print(f"[tf_output] reading state from {cwd.name}/")
    result = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"terraform output failed in {cwd.name}: {result.stderr}")
    return {k: v["value"] for k, v in json.loads(result.stdout).items()}


def tf_state_count(cwd: Path) -> int:
    """Return number of managed resources in the state file."""
    result = subprocess.run(
        ["terraform", "state", "list"],
        cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        return 0
    return len([ln for ln in result.stdout.splitlines() if ln.strip()])


def write_tfvars(path: Path, variables: dict) -> None:
    """Write a terraform.tfvars file — all cross-cloud secrets stay in Python memory."""
    lines = [
        f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}"
        for k, v in variables.items()
    ]
    path.write_text("\n".join(lines) + "\n")
    print(f"[tfvars] wrote {path.name}: {list(variables.keys())}")


def az_run(script: str, description: str = "") -> str:
    """
    Execute a PowerShell script inside the Azure test VM via az vm run-command.
    Returns the raw message string from Azure's JSON response.
    'az' is 'az.cmd' on Windows; plain 'az' on Linux/macOS (GitHub Actions runner).
    shell=True is required on Windows so cmd.exe resolves az.cmd; harmless on Linux.
    """
    if description:
        print(f"\n[az-run] {description}")
    az_bin = "az.cmd" if platform.system() == "Windows" else "az"
    cmd = (
        f'{az_bin} vm run-command invoke '
        f'--resource-group {RESOURCE_GROUP} '
        f'--name {AZURE_VM_NAME} '
        f'--command-id RunShellScript '
        f'--scripts "{script}"'
    )
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    if result.returncode != 0 and not result.stdout.strip():
        print(f"  [az-run] WARNING (Azure CLI failed): {result.stderr.strip()[:300]}")
        return ""
    if result.returncode != 0:
        print(f"  [az-run] script exited non-zero — extracting output anyway")
    try:
        return json.loads(result.stdout)["value"][0]["message"]
    except (json.JSONDecodeError, KeyError, IndexError):
        return result.stdout


# ── phase 1: azure active-active vpn gateway ─────────────────────────────────

def _azure_vm_password() -> str:
    """Read VM admin password from environment. Raises if not set."""
    pwd = os.environ.get("AZURE_VM_PASSWORD", "")
    if not pwd:
        raise RuntimeError(
            "AZURE_VM_PASSWORD environment variable is not set. "
            "Set it before running: $env:AZURE_VM_PASSWORD = 'YourPassword'"
        )
    return pwd


def phase1_azure() -> tuple[str, str, float]:
    """
    Deploy Azure VNet, GatewaySubnet, active-active VPN Gateway, and Ubuntu VM.
    cloud-init installs iperf3 on the VM in the background after first boot.
    Returns (gateway_ip_1, gateway_ip_2, terraform_apply_seconds).
    terraform_apply_seconds is provisioning latency only (excludes init + tf_output).
    """
    print("\n=== PHASE 1: Azure VNet + Active-Active VPN Gateway ===")

    tfvars = AZURE / "terraform.tfvars"
    if tfvars.exists():
        tfvars.unlink()

    # Phase 1 only needs the VM password — all other variables have defaults.
    write_tfvars(tfvars, {"vm_admin_password": _azure_vm_password()})

    tf(["init", "-input=false"], AZURE)          # init: not counted in prov_s
    prov_s  = tf(["apply", "-input=false", "-auto-approve"], AZURE)
    outputs = tf_output(AZURE)
    ip1     = outputs["vpn_gateway_public_ip_1"]
    ip2     = outputs["vpn_gateway_public_ip_2"]
    print(f"\nAzure Gateway IPs: pip1={ip1}  pip2={ip2}")
    return ip1, ip2, prov_s


# ── phase 2: aws vpc + two cgws + two vpn connections ────────────────────────

def phase2_aws(azure_ip1: str, azure_ip2: str) -> tuple[dict, float]:
    """
    Inject both Azure IPs, deploy AWS VPC with two Customer Gateways and two
    VPN Connections. Returns (tunnel_dict, terraform_apply_seconds).
    """
    print("\n=== PHASE 2: AWS VPC + Dual Customer Gateways + Dual VPN Connections ===")

    write_tfvars(AWS / "terraform.tfvars", {
        "azure_gateway_ip":   azure_ip1,
        "azure_gateway_ip_2": azure_ip2,
    })

    tf(["init", "-input=false"], AWS)            # init: not counted in prov_s
    prov_s = tf(["apply", "-input=false", "-auto-approve"], AWS)
    o      = tf_output(AWS)

    result = {
        "conn1_t1_ip":   o["conn1_tunnel1_address"],
        "conn1_t1_psk":  o["conn1_tunnel1_preshared_key"],
        "conn1_t2_ip":   o["conn1_tunnel2_address"],     # AWS internal HA, log only
        "conn1_t2_psk":  o["conn1_tunnel2_preshared_key"],
        "conn2_t1_ip":   o["conn2_tunnel1_address"],
        "conn2_t1_psk":  o["conn2_tunnel1_preshared_key"],
        "conn2_t2_ip":   o["conn2_tunnel2_address"],     # AWS internal HA, log only
        "conn2_t2_psk":  o["conn2_tunnel2_preshared_key"],
        "vpc_cidr":      o["vpc_cidr"],
        "vpn_conn1_id":  o["vpn_connection_1_id"],
        "vpn_conn2_id":  o["vpn_connection_2_id"],
        "aws_vm_ip":     o["aws_vm_private_ip"],
    }

    print(
        f"\nAWS tunnels:"
        f"\n  Conn1 T1 (primary): {result['conn1_t1_ip']}"
        f"\n  Conn1 T2 (HA):      {result['conn1_t2_ip']}"
        f"\n  Conn2 T1 (backup):  {result['conn2_t1_ip']}"
        f"\n  Conn2 T2 (HA):      {result['conn2_t2_ip']}"
        f"\n  AWS VM private IP:  {result['aws_vm_ip']}"
    )
    return result, prov_s


# ── phase 3: azure local network gateways + ipsec connections ────────────────

def phase3_azure_connect(aws: dict) -> float:
    """
    Inject AWS tunnel details into Azure. Creates two Local Network Gateways
    and two VPN Connections. Returns terraform_apply_seconds.
    PSKs stay in Python memory — never written to source files.
    """
    print("\n=== PHASE 3: Azure Dual Local Network Gateways + IPsec Connections ===")

    write_tfvars(
        AZURE / "terraform.tfvars",
        {
            "vm_admin_password":   _azure_vm_password(),
            "aws_tunnel_ip":       aws["conn1_t1_ip"],
            "aws_preshared_key":   aws["conn1_t1_psk"],
            "aws_tunnel_ip_2":     aws["conn2_t1_ip"],
            "aws_preshared_key_2": aws["conn2_t1_psk"],
            "aws_cidr":            aws["vpc_cidr"],
        },
    )

    prov_s  = tf(["apply", "-input=false", "-auto-approve"], AZURE)
    outputs = tf_output(AZURE)
    print(
        f"\nConnection 1 (primary) created: {outputs.get('connection_1_created', False)}"
        f"\nConnection 2 (backup)  created: {outputs.get('connection_2_created', False)}"
    )
    return prov_s


# ── tunnel convergence polling ────────────────────────────────────────────────

def wait_for_tunnels(conn1_id: str, conn2_id: str,
                     timeout: int = 600, poll_interval: int = 30) -> float:
    """
    Poll AWS until both primary tunnels (Conn1-T1 and Conn2-T1) report UP,
    or until timeout seconds elapse. Returns actual elapsed seconds.
    This replaces the static 300 s sleep so benchmark.py gets a real
    tunnel-convergence measurement instead of dead wait time.
    """
    print(
        f"\n[tunnel-wait] Polling for UP status "
        f"(timeout={timeout}s, every {poll_interval}s)"
    )
    t0       = time.perf_counter()
    deadline = t0 + timeout

    while time.perf_counter() < deadline:
        c1      = check_tunnel_status(conn1_id)
        c2      = check_tunnel_status(conn2_id)
        elapsed = time.perf_counter() - t0
        c1_s    = "UP" if c1["tunnel1_up"] else "DOWN"
        c2_s    = "UP" if c2["tunnel1_up"] else "DOWN"
        print(f"  [{elapsed:5.0f}s] Conn1-T1: {c1_s}  Conn2-T1: {c2_s}")
        if c1["tunnel1_up"] and c2["tunnel1_up"]:
            print(f"[tunnel-wait] Both primary tunnels UP in {elapsed:.0f}s")
            return elapsed
        time.sleep(poll_interval)

    elapsed = time.perf_counter() - t0
    print(f"[tunnel-wait] Timeout ({elapsed:.0f}s) — proceeding with available tunnels")
    return elapsed


# ── vm readiness polling ──────────────────────────────────────────────────────

def wait_for_vm_ready(aws_vm_ip: str,
                      timeout: int = 1000, poll_interval: int = 30) -> float:
    """
    Wait until two conditions are both true, then return elapsed seconds:
      1. cloud-init on the Azure VM has finished (iperf3 client installed).
      2. TCP port 5201 is reachable on the AWS VM (iperf3 server listening).

    Why cloud-init --wait instead of ping: Linux responds to ICMP the instant
    the NIC comes up (~30 s), but apt-get install iperf3 takes another 60-90 s.
    Pinging too early gives a false "ready" signal before iperf3 exists.
    TCP port 5201 only opens once iperf3 --server is actually running.
    """
    print(
        f"\n[vm-ready] Waiting for cloud-init + iperf3 server on {aws_vm_ip} "
        f"(timeout={timeout}s, every {poll_interval}s)"
    )
    t0       = time.perf_counter()
    deadline = t0 + timeout

    while time.perf_counter() < deadline:
        script = (
            f"cloud-init status --wait > /dev/null 2>&1 && "
            f"timeout 5 bash -c '</dev/tcp/{aws_vm_ip}/5201' 2>/dev/null && "
            f"echo READY"
        )
        output  = az_run(script, "")
        elapsed = time.perf_counter() - t0
        if output and "READY" in output:
            print(f"[vm-ready] iperf3 server ready after {elapsed:.0f}s")
            return elapsed
        print(f"  [{elapsed:5.0f}s] not ready yet — retrying in {poll_interval}s")
        time.sleep(poll_interval)

    elapsed = time.perf_counter() - t0
    print(f"[vm-ready] Timeout after {elapsed:.0f}s — proceeding anyway. Tests may fail.")
    return elapsed


# ── phase 4: data plane verification ─────────────────────────────────────────

def check_tunnel_status(vpn_conn_id: str) -> dict:
    """
    Poll AWS CLI for the tunnel Up/Down status of a VPN Connection.
    Returns {"tunnel1_up": bool, "tunnel2_up": bool}.
    """
    cmd = (
        f'aws ec2 describe-vpn-connections '
        f'--vpn-connection-ids {vpn_conn_id} '
        f'--query "VpnConnections[0].VgwTelemetry" '
        f'--output json'
    )
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    if result.returncode != 0 or not result.stdout.strip():
        return {"tunnel1_up": False, "tunnel2_up": False}
    try:
        tunnels = json.loads(result.stdout)
        return {
            "tunnel1_up": len(tunnels) > 0 and tunnels[0].get("Status") == "UP",
            "tunnel2_up": len(tunnels) > 1 and tunnels[1].get("Status") == "UP",
        }
    except (json.JSONDecodeError, TypeError):
        return {"tunnel1_up": False, "tunnel2_up": False}


def run_icmp_test(aws_vm_ip: str) -> dict:
    """
    Instruct the Azure VM to ping the AWS VM (50 packets) via run-command.
    Returns parsed metrics: rtt_min, rtt_avg, rtt_max, packet_loss_pct.
    """
    print(f"\n[phase4] ICMP test — Azure VM → AWS VM ({aws_vm_ip}), 50 packets")
    output = az_run(f"ping -c 50 {aws_vm_ip}", "ICMP 50-packet ping")

    metrics = {
        "icmp_rtt_min_ms":      None,
        "icmp_rtt_avg_ms":      None,
        "icmp_rtt_max_ms":      None,
        "icmp_packet_loss_pct": 100.0,
        "icmp_success":         False,
    }
    if not output:
        return metrics

    print(output)

    # Linux ping stats: "50 packets transmitted, 48 received, 4% packet loss"
    # Linux RTT stats:  "rtt min/avg/max/mdev = 44.2/45.1/46.3/0.4 ms"
    loss_match = re.search(r"(\d+)% packet loss", output)
    rtt_match  = re.search(
        r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/",
        output
    )

    if loss_match:
        metrics["icmp_packet_loss_pct"] = float(loss_match.group(1))
    if rtt_match:
        metrics["icmp_rtt_min_ms"] = float(rtt_match.group(1))
        metrics["icmp_rtt_avg_ms"] = float(rtt_match.group(2))
        metrics["icmp_rtt_max_ms"] = float(rtt_match.group(3))
        metrics["icmp_success"]    = True

    return metrics


def run_iperf3_test(aws_vm_ip: str) -> dict:
    """
    Run iperf3 tests from the Azure VM toward the AWS VM iperf3 server:
      - TCP Azure→AWS  (30 s)
      - TCP AWS→Azure  (30 s, reverse flag -R)
      - UDP Azure→AWS  (30 s, target 100 Mbps)

    Uses human-readable output (no -J flag) to avoid Azure run-command's ~4 KB
    output cap, which truncates iperf3's verbose JSON for 30-second tests.
    Human-readable summary lines are ~200 bytes total.
    """
    metrics = {
        "tcp_az_to_aws_mbps":  None,
        "tcp_aws_to_az_mbps":  None,
        "tcp_retransmissions": None,
        "udp_mbps":            None,
        "jitter_ms":           None,
        "udp_loss_pct":        None,
    }

    def _mbps(val: str, unit: str) -> float:
        return round(float(val) * (1000 if unit == 'G' else 1), 2)

    # ── TCP Azure → AWS ───────────────────────────────────────────────────────
    # Summary "sender" line: "  N Mbits/sec  R  sender"  (R = retransmits)
    print(f"\n[phase4] iperf3 TCP  Azure→AWS  ({aws_vm_ip}:5201)")
    raw = az_run(f"iperf3 -c {aws_vm_ip} -t 30", "iperf3 TCP forward")
    print(raw[:600] if raw else "  (no output)")
    m = re.search(r"([\d.]+)\s+(M|G)bits/sec\s+(\d+)\s+sender", raw or "")
    if m:
        metrics["tcp_az_to_aws_mbps"]  = _mbps(m.group(1), m.group(2))
        metrics["tcp_retransmissions"] = int(m.group(3))
    else:
        print(f"  [iperf3] Could not parse TCP forward. Raw: {(raw or '')[:300]!r}")

    # ── TCP AWS → Azure (reverse) ─────────────────────────────────────────────
    # In -R mode the "sender" stats are still from the server (AWS), shown on client.
    print(f"\n[phase4] iperf3 TCP  AWS→Azure  (reverse mode)")
    raw = az_run(f"iperf3 -c {aws_vm_ip} -t 30 -R", "iperf3 TCP reverse")
    print(raw[:600] if raw else "  (no output)")
    m = re.search(r"([\d.]+)\s+(M|G)bits/sec\s+(\d+)\s+sender", raw or "")
    if m:
        metrics["tcp_aws_to_az_mbps"] = _mbps(m.group(1), m.group(2))
    else:
        print(f"  [iperf3] Could not parse TCP reverse. Raw: {(raw or '')[:300]!r}")

    # ── UDP Azure → AWS ───────────────────────────────────────────────────────
    # UDP receiver line: "... X Mbits/sec  J ms  lost/total (P%)  receiver"
    print(f"\n[phase4] iperf3 UDP  Azure→AWS  (target 100 Mbps)")
    raw = az_run(f"iperf3 -c {aws_vm_ip} -u -b 100M -t 30", "iperf3 UDP")
    print(raw[:600] if raw else "  (no output)")
    m = re.search(
        r"([\d.]+)\s+(M|G)bits/sec\s+([\d.]+)\s+ms\s+\d+/\d+\s+\(([\d.]+)%\)\s+receiver",
        raw or ""
    )
    if m:
        metrics["udp_mbps"]     = _mbps(m.group(1), m.group(2))
        metrics["jitter_ms"]    = float(m.group(3))
        metrics["udp_loss_pct"] = float(m.group(4))
    else:
        print(f"  [iperf3] Could not parse UDP. Raw: {(raw or '')[:300]!r}")

    return metrics


# ── teardown ──────────────────────────────────────────────────────────────────

def destroy() -> None:
    """
    Tear down all infrastructure. Azure destroyed first to release VPN endpoints,
    then AWS. tfvars deleted AFTER each destroy. Both clouds are always attempted
    even if one fails, to avoid orphaned resources across cycles.
    """
    print("\n=== TEARDOWN ===")
    errors = []

    try:
        tf(["destroy", "-input=false", "-auto-approve"], AZURE)
    except RuntimeError as e:
        errors.append(str(e))
        print(f"[destroy] Azure failed: {e}")
    finally:
        if (AZURE / "terraform.tfvars").exists():
            (AZURE / "terraform.tfvars").unlink()

    try:
        tf(["destroy", "-input=false", "-auto-approve"], AWS)
    except RuntimeError as e:
        errors.append(str(e))
        print(f"[destroy] AWS failed: {e}")
    finally:
        if (AWS / "terraform.tfvars").exists():
            (AWS / "terraform.tfvars").unlink()

    if errors:
        raise RuntimeError(" | ".join(errors))


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if "--destroy" in sys.argv:
        try:
            destroy()
        except RuntimeError as e:
            sys.exit(str(e))
        return

    try:
        azure_ip1, azure_ip2, _ = phase1_azure()
        aws, _                  = phase2_aws(azure_ip1, azure_ip2)
        phase3_azure_connect(aws)

        print("\n✓ Both IPsec VPN tunnels established (active-active).")
        wait_for_tunnels(aws["vpn_conn1_id"], aws["vpn_conn2_id"])
        wait_for_vm_ready(aws["aws_vm_ip"])

        print("\n=== PHASE 4: Data Plane Verification ===")

        conn1_status = check_tunnel_status(aws["vpn_conn1_id"])
        conn2_status = check_tunnel_status(aws["vpn_conn2_id"])
        print(
            f"\nTunnel status:"
            f"\n  Conn1 Tunnel1 (primary): {'UP' if conn1_status['tunnel1_up'] else 'DOWN'}"
            f"\n  Conn1 Tunnel2 (HA):      {'UP' if conn1_status['tunnel2_up'] else 'DOWN'}"
            f"\n  Conn2 Tunnel1 (backup):  {'UP' if conn2_status['tunnel1_up'] else 'DOWN'}"
            f"\n  Conn2 Tunnel2 (HA):      {'UP' if conn2_status['tunnel2_up'] else 'DOWN'}"
        )

        icmp = run_icmp_test(aws["aws_vm_ip"])
        if icmp["icmp_success"]:
            print(f"\n✓ ICMP: avg={icmp['icmp_rtt_avg_ms']}ms  loss={icmp['icmp_packet_loss_pct']}%")
        else:
            print("\n✗ ICMP: no replies — check tunnel state and VM firewall")

        iperf = run_iperf3_test(aws["aws_vm_ip"])
        print(
            f"\niperf3 results:"
            f"\n  TCP Azure→AWS:  {iperf['tcp_az_to_aws_mbps']} Mbps"
            f"\n  TCP AWS→Azure:  {iperf['tcp_aws_to_az_mbps']} Mbps"
            f"\n  UDP throughput: {iperf['udp_mbps']} Mbps  "
            f"jitter={iperf['jitter_ms']} ms  loss={iperf['udp_loss_pct']}%"
        )
        print("\n✓ Orchestration complete.  Run with --destroy to tear down.")

    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
