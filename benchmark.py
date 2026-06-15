
"""
benchmark.py — Experiment 1: Steady-State Performance (30 cycles)
==================================================================
Times every phase of the PL-IaC framework and records a full row to
data/exp1_steady_state_YYYY-MM-DD.csv after each cycle.

Append-only writes: if the run crashes at cycle 23, rows 1-22 are safe.

Usage:
    python benchmark.py --cycles 30
    python benchmark.py --cycles 5   # smoke test

Latency columns — three numbers per phase:
  t_phaseN_s      total wall-clock time for the phase function
  t_phaseN_prov_s Terraform apply time only (cloud API provisioning)
  t_phaseN_proc_s Python overhead = t_phaseN_s - t_phaseN_prov_s
                  (write_tfvars, tf_output JSON parse, Python logic)

Tunnel convergence:
  tunnel_convergence_s  seconds from Phase 3 completion until both primary
                        tunnels report UP — active polling, not a static sleep
"""

import argparse
import csv
import json
import os
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from orchestrator import (
    phase1_azure,
    phase2_aws,
    phase3_azure_connect,
    run_icmp_test,
    run_iperf3_test,
    check_tunnel_status,
    wait_for_tunnels,
    wait_for_vm_ready,
    destroy,
    tf_state_count,
    AWS,
    AZURE,
)

BASE     = Path(__file__).parent
DATA_DIR = BASE / "data"
DATA_DIR.mkdir(exist_ok=True)

CSV_COLS = [
    "cycle",
    "timestamp_utc",
    # ── Phase 1 timings ────────────────────────────────────────────────────────
    "t_phase1_s",               # total wall-clock (Azure deploy)
    "t_phase1_prov_s",          # Terraform apply only
    "t_phase1_proc_s",          # Python overhead
    # ── Phase 2 timings ────────────────────────────────────────────────────────
    "t_phase2_s",               # total wall-clock (AWS deploy)
    "t_phase2_prov_s",
    "t_phase2_proc_s",
    # ── Phase 3 timings ────────────────────────────────────────────────────────
    "t_phase3_s",               # total wall-clock (Azure connect)
    "t_phase3_prov_s",
    "t_phase3_proc_s",
    # ── Totals ─────────────────────────────────────────────────────────────────
    "t_total_provisioning_s",   # Phase 1+2+3 combined wall-clock
    "t_total_prov_s",           # Sum of pure Terraform apply times (phases 1-3)
    "t_total_proc_s",           # Sum of pure Python overhead (phases 1-3)
    # ── Tunnel convergence ─────────────────────────────────────────────────────
    "tunnel_convergence_s",     # Seconds from Phase 3 end → both primary tunnels UP
    "vm_startup_s",             # Seconds from tunnel UP → AWS EC2 first ICMP reply
    # ── Tunnel status snapshot ─────────────────────────────────────────────────
    "tunnel1_up",               # AWS Conn1 T1 — primary active
    "tunnel2_up",               # AWS Conn1 T2 — AWS-managed HA standby
    "tunnel3_up",               # AWS Conn2 T1 — backup active
    "tunnel4_up",               # AWS Conn2 T2 — AWS-managed HA standby
    "active_tunnel_count",
    # ── ICMP (Azure VM → AWS VM, 50 packets) ──────────────────────────────────
    "icmp_rtt_min_ms",
    "icmp_rtt_avg_ms",
    "icmp_rtt_max_ms",
    "icmp_packet_loss_pct",
    "icmp_success",
    # ── iperf3 throughput ──────────────────────────────────────────────────────
    "tcp_az_to_aws_mbps",
    "tcp_aws_to_az_mbps",
    "tcp_retransmissions",
    "udp_mbps",
    "jitter_ms",
    "udp_loss_pct",
    # ── Teardown ───────────────────────────────────────────────────────────────
    "t_destroy_s",
    "destroy_success",
    # ── State isolation evidence ───────────────────────────────────────────────
    "aws_state_resource_count",
    "azure_state_resource_count",
    # ── Metadata ───────────────────────────────────────────────────────────────
    "success",
    "failure_phase",
    "failure_type",
    "failure_reason",
    "cycle_duration_min",
    "run_source",       # "github_actions" or "local"
    "github_run_id",    # GitHub Actions run ID (empty string for local runs)
]


def timed(fn, *args, **kwargs):
    """Call fn(*args), return (result, elapsed_seconds)."""
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, time.perf_counter() - t0


def categorise_failure(exc: Exception) -> str:
    """Map an exception to a short tag for the failure_type column."""
    msg = str(exc).lower()
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(exc, RuntimeError) and "terraform" in msg:
        return "terraform_error"
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError)):
        return "api_parse_error"
    if isinstance(exc, OSError):
        return "os_error"
    return type(exc).__name__


def run_cycle(cycle_num: int) -> dict:
    row = {c: None for c in CSV_COLS}
    row["cycle"]         = cycle_num
    row["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    row["success"]       = False
    row["failure_phase"] = None
    row["failure_type"]  = None
    row["failure_reason"] = ""

    wall_start = time.perf_counter()
    aws_data   = {}

    try:
        # ── Phase 1 ───────────────────────────────────────────────────────────
        (azure_ip1, azure_ip2, prov1_s), row["t_phase1_s"] = timed(phase1_azure)
        row["t_phase1_prov_s"] = prov1_s
        row["t_phase1_proc_s"] = max(0.0, row["t_phase1_s"] - prov1_s)

        # ── Phase 2 ───────────────────────────────────────────────────────────
        (aws_data, prov2_s), row["t_phase2_s"] = timed(phase2_aws, azure_ip1, azure_ip2)
        row["t_phase2_prov_s"] = prov2_s
        row["t_phase2_proc_s"] = max(0.0, row["t_phase2_s"] - prov2_s)

        # ── Phase 3 ───────────────────────────────────────────────────────────
        prov3_s, row["t_phase3_s"] = timed(phase3_azure_connect, aws_data)
        row["t_phase3_prov_s"] = prov3_s
        row["t_phase3_proc_s"] = max(0.0, row["t_phase3_s"] - prov3_s)

        row["t_total_provisioning_s"] = (
            row["t_phase1_s"] + row["t_phase2_s"] + row["t_phase3_s"]
        )
        row["t_total_prov_s"] = prov1_s + prov2_s + prov3_s
        row["t_total_proc_s"] = (
            row["t_phase1_proc_s"] + row["t_phase2_proc_s"] + row["t_phase3_proc_s"]
        )

        # ── State isolation evidence ───────────────────────────────────────────
        row["aws_state_resource_count"]   = tf_state_count(AWS)
        row["azure_state_resource_count"] = tf_state_count(AZURE)

        # ── Tunnel convergence (active polling, replaces static 300 s sleep) ──
        print(f"\n[Cycle {cycle_num}] Waiting for tunnel convergence...")
        row["tunnel_convergence_s"] = wait_for_tunnels(
            aws_data["vpn_conn1_id"], aws_data["vpn_conn2_id"]
        )

        # ── VM readiness (poll ICMP until AWS EC2 replies) ─────────────────────
        row["vm_startup_s"] = wait_for_vm_ready(aws_data["aws_vm_ip"])

        # ── Tunnel status snapshot ─────────────────────────────────────────────
        c1 = check_tunnel_status(aws_data["vpn_conn1_id"])
        c2 = check_tunnel_status(aws_data["vpn_conn2_id"])
        row["tunnel1_up"] = c1["tunnel1_up"]
        row["tunnel2_up"] = c1["tunnel2_up"]
        row["tunnel3_up"] = c2["tunnel1_up"]
        row["tunnel4_up"] = c2["tunnel2_up"]
        row["active_tunnel_count"] = sum([
            bool(row["tunnel1_up"]), bool(row["tunnel2_up"]),
            bool(row["tunnel3_up"]), bool(row["tunnel4_up"]),
        ])

        # ── ICMP ──────────────────────────────────────────────────────────────
        icmp = run_icmp_test(aws_data["aws_vm_ip"])
        row.update(icmp)

        # ── iperf3 ────────────────────────────────────────────────────────────
        iperf = run_iperf3_test(aws_data["aws_vm_ip"])
        row.update(iperf)

        row["success"] = True

    except Exception as exc:
        row["failure_type"]   = categorise_failure(exc)
        row["failure_reason"] = repr(exc)
        if row["t_phase1_s"] is None:
            row["failure_phase"] = 1
        elif row["t_phase2_s"] is None:
            row["failure_phase"] = 2
        elif row["t_phase3_s"] is None:
            row["failure_phase"] = 3
        else:
            row["failure_phase"] = 4
        print(
            f"\n[Cycle {cycle_num}] ERROR phase={row['failure_phase']} "
            f"type={row['failure_type']}: {exc}"
        )
        traceback.print_exc()

    finally:
        try:
            _, row["t_destroy_s"] = timed(destroy)
            row["destroy_success"] = True
        except Exception as exc:
            row["failure_reason"] = (row["failure_reason"] or "") + f" | destroy: {repr(exc)}"
            row["destroy_success"] = False

    row["cycle_duration_min"] = (time.perf_counter() - wall_start) / 60
    row["run_source"]    = "github_actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local"
    row["github_run_id"] = os.environ.get("GITHUB_RUN_ID", "")
    return row


def run_benchmark(cycles: int, out_path: Path) -> None:
    file_exists = out_path.exists()

    with out_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()

        for i in range(1, cycles + 1):
            print(f"\n{'='*60}")
            print(f"  BENCHMARK CYCLE {i} / {cycles}")
            print(f"{'='*60}")

            row = run_cycle(i)
            writer.writerow(row)
            f.flush()  # flush immediately — don't lose data on crash

            prov_str = f"{row['t_total_prov_s']:.0f}s" if row["t_total_prov_s"] else "ERR"
            proc_str = f"{row['t_total_proc_s']:.1f}s" if row["t_total_proc_s"] else "ERR"
            conv_str = (
                f"{row['tunnel_convergence_s']:.0f}s"
                if row["tunnel_convergence_s"] is not None else "N/A"
            )
            print(
                f"\n[Cycle {i}] "
                f"total={row['cycle_duration_min']:.1f}min  "
                f"prov={prov_str}  proc={proc_str}  "
                f"conv={conv_str}  "
                f"tunnels={row['active_tunnel_count']}/4  "
                f"ping={'OK' if row['icmp_success'] else 'FAIL'}  "
                f"tcp={row['tcp_az_to_aws_mbps']}Mbps"
            )

    print(f"\nBenchmark complete — {cycles} cycles written to: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Experiment 1: Steady-State Benchmark")
    parser.add_argument("--cycles", type=int, default=30)
    args = parser.parse_args()

    today    = datetime.now().strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"exp1_steady_state_{today}.csv"

    print(f"Experiment 1 — {args.cycles} cycles")
    print(f"Output: {out_path}\n")
    run_benchmark(args.cycles, out_path)


if __name__ == "__main__":
    main()
