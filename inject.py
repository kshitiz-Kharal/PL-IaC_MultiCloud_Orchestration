
"""
inject.py — Experiment 2: Failure-Injection Comparison
=======================================================
Measures blast-radius isolation by injecting deliberate faults and comparing:

  (A) PL-IaC Framework   — decoupled azure/ and aws/ modules
  (B) Monolithic Baseline — single shared monolith/main.tf

Per cycle:
  1. Select fault from catalogue via seeded RNG (reproducible with --seed).
  2. Inject fault by modifying the relevant .tf file.
  3. Run both conditions, recording which resources were provisioned.
  4. Restore the original file.
  5. Measure re-apply (recovery) time after correction.

Usage:
    python inject.py --cycles 15 --seed 42

Output: data/exp2_fault_injection_YYYY-MM-DD.csv
"""

import argparse
import csv
import json
import os
import random
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from orchestrator import tf_state_count, AWS, AZURE

BASE     = Path(__file__).parent
MONO_DIR = BASE / "monolith"
DATA_DIR = BASE / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── Fault catalogue ───────────────────────────────────────────────────────────
# Each entry:
#   id, category, description
#   file        — path relative to BASE for the framework module
#   find/replace — exact string substitution to inject the fault
#   mono_file   — path in monolith (may differ due to resource naming)
#   mono_find / mono_replace — substitution for monolith

FAULT_CATALOGUE = [
    # ── Syntax ────────────────────────────────────────────────────────────────
    {
        "id": "SYN-01",
        "category": "syntax",
        "description": "Invalid AWS VPC CIDR prefix length (/99)",
        "file": "aws/variables.tf",
        "find":    '  default = "10.1.0.0/16"',
        "replace": '  default = "10.1.0.0/99"',
        "mono_file":    "monolith/main.tf",
        "mono_find":    '  cidr_block           = "10.1.0.0/16"',
        "mono_replace": '  cidr_block           = "10.1.0.0/99"',
    },
    {
        "id": "SYN-02",
        "category": "syntax",
        "description": "Invalid Azure VNet CIDR (malformed address)",
        "file": "azure/main.tf",
        "find":    '  address_space       = [var.vnet_cidr]',
        "replace": '  address_space       = ["10.2.0.0/999"]',
        "mono_file":    "monolith/main.tf",
        "mono_find":    '  address_space       = ["10.2.0.0/16"]',
        "mono_replace": '  address_space       = ["10.2.0.0/999"]',
    },
    {
        "id": "SYN-03",
        "category": "syntax",
        "description": "Invalid AWS EC2 instance type",
        "file": "aws/main.tf",
        "find":    '  instance_type = "t3.micro"',
        "replace": '  instance_type = "t99.invalid"',
        "mono_file":    "monolith/main.tf",
        "mono_find":    '  instance_type          = "t3.micro"',
        "mono_replace": '  instance_type          = "t99.invalid"',
    },
    # ── Semantic ──────────────────────────────────────────────────────────────
    {
        "id": "SEM-01",
        "category": "semantic",
        "description": "AWS subnet CIDR outside VPC range (10.9.x not in 10.1.x)",
        "file": "aws/variables.tf",
        "find":    '  default = "10.1.1.0/24"',
        "replace": '  default = "10.9.0.0/24"',
        "mono_file":    "monolith/main.tf",
        "mono_find":    '  cidr_block        = "10.1.1.0/24"',
        "mono_replace": '  cidr_block        = "10.9.0.0/24"',
    },
    {
        "id": "SEM-02",
        "category": "semantic",
        "description": "Azure GatewaySubnet too small — /32 is below /27 minimum",
        "file": "azure/variables.tf",
        "find":    '  default = "10.2.255.0/27"',
        "replace": '  default = "10.2.255.0/32"',
        "mono_file":    "monolith/main.tf",
        "mono_find":    '  address_prefixes     = ["10.2.255.0/27"]',
        "mono_replace": '  address_prefixes     = ["10.2.255.0/32"]',
    },
    # ── Runtime ───────────────────────────────────────────────────────────────
    {
        "id": "RUN-01",
        "category": "runtime",
        "description": "Invalid AWS AMI filter — no AMI matches, data source returns error",
        "file": "aws/main.tf",
        "find":    '    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]',
        "replace": '    values = ["ubuntu/images/hvm-ssd/ubuntu-totally-wrong-*"]',
        "mono_file":    "monolith/main.tf",
        "mono_find":    '  ami                    = "ami-0111f46977d33b84b"  # Ubuntu 22.04 LTS ap-southeast-2',
        "mono_replace": '  ami                    = "ami-00000000000invalid"  # Ubuntu 22.04 LTS ap-southeast-2',
    },
    {
        "id": "RUN-02",
        "category": "runtime",
        "description": "Invalid AWS availability zone — subnet creation fails at runtime",
        "file": "aws/main.tf",
        "find":    '  availability_zone       = "${var.region}a"',
        "replace": '  availability_zone       = "${var.region}z"',
        "mono_file":    "monolith/main.tf",
        "mono_find":    '  availability_zone       = "ap-southeast-2a"',
        "mono_replace": '  availability_zone       = "ap-southeast-2z"',
    },
    # ── Cross-cloud ───────────────────────────────────────────────────────────
    {
        "id": "CC-01",
        "category": "cross-cloud",
        "description": "AWS VPN static route CIDR mismatches Azure VNet — routing black hole",
        "file": "aws/main.tf",
        "find":    "  destination_cidr_block = var.azure_cidr",
        "replace": '  destination_cidr_block = "192.168.99.0/24"',
        "mono_file":    "monolith/main.tf",
        "mono_find":    '  destination_cidr_block = "10.2.0.0/16"',
        "mono_replace": '  destination_cidr_block = "192.168.99.0/24"',
    },
    {
        "id": "CC-02",
        "category": "cross-cloud",
        "description": "Azure VNet CIDR mismatches AWS VPN route — traffic never reaches VPN",
        "file": "azure/variables.tf",
        "find":    '  default = "10.2.0.0/16"',
        "replace": '  default = "192.168.88.0/16"',
        "mono_file":    "monolith/main.tf",
        "mono_find":    '  address_space       = ["10.2.0.0/16"]',
        "mono_replace": '  address_space       = ["192.168.88.0/16"]',
    },
]

CSV_COLS = [
    "cycle", "timestamp_utc",
    "fault_id", "fault_category", "fault_description", "fault_file",
    # Framework
    "fw_azure_provisioned", "fw_aws_provisioned",
    "fw_azure_resource_count", "fw_aws_resource_count",
    "fw_blast_radius_score",   # 0-2: clouds still provisioned despite fault
    "fw_recovery_time_s",
    # Monolith
    "mono_azure_provisioned", "mono_aws_provisioned",
    "mono_resource_count",
    "mono_blast_radius_score",
    "mono_recovery_time_s",
    "notes",
]


# ── File backup / restore ─────────────────────────────────────────────────────
# Backups are written to DISK (*.tf.bak) rather than kept in Python memory.
# If the process is killed hard (OOM, reboot) before restore_files() runs,
# the .bak files survive on disk and cleanup_stale_backups() recovers them
# automatically at the start of the next run.

def cleanup_stale_backups() -> None:
    """
    Restore any *.tf.bak files left on disk by a previous crashed run.
    Called once at the start of run_experiment() before any fault is injected.
    """
    stale = list(BASE.glob("**/*.tf.bak"))
    if not stale:
        return
    print(f"[inject] WARNING: {len(stale)} stale .bak file(s) found from a previous crash — restoring")
    for bak in stale:
        orig = bak.with_suffix("")   # removes .bak → original .tf path
        shutil.copy2(bak, orig)
        bak.unlink()
        print(f"  Restored: {orig.relative_to(BASE)}")


def inject_fault(fault: dict) -> dict:
    """
    Inject fault into framework and monolith files.
    Writes *.tf.bak files to disk BEFORE modifying anything.
    Returns {key: (original_path, bak_path)} for restore_files().
    """
    bak_paths = {}

    fw_path = BASE / fault["file"]
    fw_bak  = fw_path.with_suffix(".tf.bak")
    shutil.copy2(fw_path, fw_bak)
    bak_paths["fw"] = (fw_path, fw_bak)

    content = fw_path.read_text(encoding="utf-8")
    if fault["find"] not in content:
        fw_bak.unlink(missing_ok=True)
        raise ValueError(
            f"{fault['id']}: pattern not found in {fw_path.name}\n"
            f"  Expected: {fault['find']!r}"
        )
    fw_path.write_text(content.replace(fault["find"], fault["replace"], 1), encoding="utf-8")

    mono_path    = BASE / fault.get("mono_file", "monolith/main.tf")
    mono_find    = fault.get("mono_find",    fault["find"])
    mono_replace = fault.get("mono_replace", fault["replace"])
    mono_bak     = mono_path.with_suffix(".tf.bak")
    shutil.copy2(mono_path, mono_bak)
    bak_paths["mono"] = (mono_path, mono_bak)

    content = mono_path.read_text(encoding="utf-8")
    if mono_find not in content:
        fw_bak.unlink(missing_ok=True)
        mono_bak.unlink(missing_ok=True)
        raise ValueError(
            f"{fault['id']}: monolith pattern not found in {mono_path.name}\n"
            f"  Expected: {mono_find!r}"
        )
    mono_path.write_text(content.replace(mono_find, mono_replace, 1), encoding="utf-8")

    print(f"[inject] {fault['id']} ({fault['category']}): {fault['description']}")
    return bak_paths


def restore_files(bak_paths: dict) -> None:
    """Copy each *.tf.bak back to the original path, then delete the .bak."""
    for _, (path, bak_path) in bak_paths.items():
        if bak_path.exists():
            shutil.copy2(bak_path, path)
            bak_path.unlink()
    print("[inject] files restored from disk backups")


# ── Terraform helpers ─────────────────────────────────────────────────────────

def tf_run(cmd: list, cwd: Path) -> tuple[int, str]:
    result = subprocess.run(
        ["terraform"] + cmd, cwd=cwd,
        capture_output=True, text=True, timeout=3600
    )
    return result.returncode, result.stdout + result.stderr


# ── Per-condition runs ────────────────────────────────────────────────────────

def run_framework_with_fault(fault: dict, azure_ip1: str = "1.2.3.4",
                              azure_ip2: str = "1.2.3.5") -> dict:
    result = {"azure_provisioned": 0, "aws_provisioned": 0,
              "azure_count": 0, "aws_count": 0}

    tf_run(["init", "-input=false"], AZURE)
    tf_run(["init", "-input=false"], AWS)

    vm_password = os.environ.get("AZURE_VM_PASSWORD", "")
    (AZURE / "terraform.tfvars").write_text(
        f'vm_admin_password = "{vm_password}"\n', encoding="utf-8"
    )

    rc_az, _ = tf_run(["apply", "-input=false", "-auto-approve"], AZURE)
    result["azure_provisioned"] = 1 if rc_az == 0 else 0
    result["azure_count"]       = tf_state_count(AZURE)

    # inject azure IPs into aws tfvars
    (AWS / "terraform.tfvars").write_text(
        f'azure_gateway_ip   = "{azure_ip1}"\n'
        f'azure_gateway_ip_2 = "{azure_ip2}"\n'
    )
    rc_aws, _ = tf_run(["apply", "-input=false", "-auto-approve"], AWS)
    result["aws_provisioned"] = 1 if rc_aws == 0 else 0
    result["aws_count"]       = tf_state_count(AWS)

    tf_run(["destroy", "-input=false", "-auto-approve"], AZURE)
    tf_run(["destroy", "-input=false", "-auto-approve"], AWS)
    for p in [AZURE / "terraform.tfvars", AWS / "terraform.tfvars"]:
        if p.exists(): p.unlink()

    return result


def run_monolith_with_fault() -> dict:
    result = {"azure_provisioned": 0, "aws_provisioned": 0, "state_count": 0}

    tf_run(["init", "-input=false"], MONO_DIR)
    rc, _ = tf_run(["apply", "-input=false", "-auto-approve"], MONO_DIR)

    if rc == 0:
        result["azure_provisioned"] = 1
        result["aws_provisioned"]   = 1
    else:
        rc_list, out = tf_run(["state", "list"], MONO_DIR)
        if rc_list == 0:
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            result["azure_provisioned"] = int(any("azurerm" in l for l in lines))
            result["aws_provisioned"]   = int(any(l.startswith("aws_") for l in lines))

    result["state_count"] = tf_state_count(MONO_DIR)
    tf_run(["destroy", "-input=false", "-auto-approve"], MONO_DIR)
    return result


def measure_recovery_framework() -> float:
    tf_run(["init", "-input=false"], AZURE)
    t0 = time.perf_counter()
    tf_run(["apply", "-input=false", "-auto-approve"], AZURE)
    elapsed = time.perf_counter() - t0
    tf_run(["destroy", "-input=false", "-auto-approve"], AZURE)
    tf_run(["destroy", "-input=false", "-auto-approve"], AWS)
    return elapsed


def measure_recovery_monolith() -> float:
    tf_run(["init", "-input=false"], MONO_DIR)
    t0 = time.perf_counter()
    tf_run(["apply", "-input=false", "-auto-approve"], MONO_DIR)
    elapsed = time.perf_counter() - t0
    tf_run(["destroy", "-input=false", "-auto-approve"], MONO_DIR)
    return elapsed


# ── Main experiment loop ──────────────────────────────────────────────────────

def run_experiment(cycles: int, start_index: int, out_path: Path) -> None:
    """Run fault injection cycles, selecting faults by index for exhaustive coverage.

    Fault selection: FAULT_CATALOGUE[(start_index + i) % len(FAULT_CATALOGUE)]
    With 9 faults and cycles=9, every fault is tested exactly once.
    start_index lets the workflow resume mid-catalogue across daily runs.
    """
    cleanup_stale_backups()   # recover from any previous hard crash

    file_exists = out_path.exists()
    with out_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()

        for i in range(1, cycles + 1):
            fault = FAULT_CATALOGUE[(start_index + i - 1) % len(FAULT_CATALOGUE)]
            print(f"\n{'='*60}")
            print(f"  FAULT CYCLE {i}/{cycles}  |  {fault['id']} — {fault['description']}")
            print(f"{'='*60}")

            row = {c: None for c in CSV_COLS}
            row.update({
                "cycle": i,
                "timestamp_utc":    datetime.now(timezone.utc).isoformat(),
                "fault_id":         fault["id"],
                "fault_category":   fault["category"],
                "fault_description":fault["description"],
                "fault_file":       fault["file"],
                "notes": "",
            })

            backups = None
            try:
                backups = inject_fault(fault)

                print("\n--- Framework ---")
                fw = run_framework_with_fault(fault)
                row["fw_azure_provisioned"]  = fw["azure_provisioned"]
                row["fw_aws_provisioned"]    = fw["aws_provisioned"]
                row["fw_azure_resource_count"]= fw["azure_count"]
                row["fw_aws_resource_count"] = fw["aws_count"]
                row["fw_blast_radius_score"] = fw["azure_provisioned"] + fw["aws_provisioned"]

                print("\n--- Monolith ---")
                mono = run_monolith_with_fault()
                row["mono_azure_provisioned"]  = mono["azure_provisioned"]
                row["mono_aws_provisioned"]    = mono["aws_provisioned"]
                row["mono_resource_count"]     = mono["state_count"]
                row["mono_blast_radius_score"] = mono["azure_provisioned"] + mono["aws_provisioned"]

                restore_files(backups)
                backups = None

                print("\n--- Recovery timing ---")
                row["fw_recovery_time_s"]   = measure_recovery_framework()
                row["mono_recovery_time_s"] = measure_recovery_monolith()

            except Exception as exc:
                row["notes"] = repr(exc)
                print(f"\n[Cycle {i}] ERROR: {exc}")
                traceback.print_exc()
            finally:
                if backups is not None:
                    try:
                        restore_files(backups)
                    except Exception as e:
                        print(f"WARNING: restore failed: {e}")

            writer.writerow(row)
            f.flush()
            fw_rec   = f"{row['fw_recovery_time_s']:.0f}s"   if row["fw_recovery_time_s"]   else "N/A"
            mono_rec = f"{row['mono_recovery_time_s']:.0f}s" if row["mono_recovery_time_s"] else "N/A"
            print(
                f"\n[Cycle {i}] fault={fault['id']}  "
                f"FW={row['fw_blast_radius_score']}/2  "
                f"Mono={row['mono_blast_radius_score']}/2  "
                f"FW_rec={fw_rec}  Mono_rec={mono_rec}"
            )

    print(f"\nExperiment 2 complete — {cycles} cycles written to: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Experiment 2: Fault Injection")
    parser.add_argument("--cycles", type=int, default=18)
    parser.add_argument("--start-index", type=int, default=0,
                        help="Catalogue index to start from (0-based). "
                             "Pass the number of cycles already done so daily "
                             "runs step through the catalogue in order.")
    args = parser.parse_args()

    today    = datetime.now().strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"exp2_fault_injection_{today}.csv"

    print(f"Experiment 2 — {args.cycles} cycles, start_index={args.start_index}")
    print(f"Fault catalogue: {len(FAULT_CATALOGUE)} entries")
    print(f"Output: {out_path}\n")

    run_experiment(args.cycles, args.start_index, out_path)


if __name__ == "__main__":
    main()
