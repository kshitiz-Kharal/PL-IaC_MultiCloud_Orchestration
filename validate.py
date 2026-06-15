
"""
validate.py — Pre-flight syntax and configuration checks
=========================================================
Run this before any real deployment to catch errors without spending money.

Checks:
  1. Python syntax on all .py files
  2. terraform fmt  — HCL formatting
  3. terraform validate — provider schema and variable consistency
     (requires terraform init to have been run at least once)

Usage:
    python validate.py

Exit code 0 = all checks passed. Non-zero = at least one check failed.
"""

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"


def check(label: str, ok: bool, detail: str = "") -> bool:
    tag = PASS if ok else FAIL
    msg = f"  {tag}  {label}"
    if detail and not ok:
        msg += f"\n         {detail}"
    print(msg)
    return ok


def run(cmd: list, cwd: Path = BASE) -> tuple[int, str]:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


# ── 1. Python syntax ──────────────────────────────────────────────────────────

def check_python_syntax() -> bool:
    print("\n── Python syntax ────────────────────────────────────────────")
    py_files = [
        "orchestrator.py", "benchmark.py",
        "inject.py", "analysis.py", "validate.py",
    ]
    all_ok = True
    for fname in py_files:
        p = BASE / fname
        if not p.exists():
            print(f"  {SKIP}  {fname} (not found)")
            continue
        rc, out = run([sys.executable, "-m", "py_compile", fname])
        ok = check(fname, rc == 0, out)
        all_ok = all_ok and ok
    return all_ok


# ── 2. Terraform fmt ─────────────────────────────────────────────────────────

def check_tf_fmt() -> bool:
    print("\n── Terraform formatting (terraform fmt -check) ───────────────")
    all_ok = True
    for module in ["azure", "aws", "monolith"]:
        d = BASE / module
        if not d.exists():
            print(f"  {SKIP}  {module}/ (directory not found)")
            continue
        rc, out = run(["terraform", "fmt", "-check", "-recursive"], cwd=d)
        detail = f"Run 'terraform fmt' in {module}/ to fix" if rc != 0 else ""
        ok = check(f"{module}/", rc == 0, detail)
        all_ok = all_ok and ok
    return all_ok


# ── 3. Terraform validate ────────────────────────────────────────────────────

def check_tf_validate() -> bool:
    print("\n── Terraform validate ────────────────────────────────────────")
    all_ok = True
    for module in ["azure", "aws", "monolith"]:
        d = BASE / module
        if not d.exists():
            print(f"  {SKIP}  {module}/ (directory not found)")
            continue

        tf_dir = d / ".terraform"
        if not tf_dir.exists():
            print(
                f"  {SKIP}  {module}/ — not initialised "
                f"(run: terraform init in {module}/)"
            )
            continue

        rc, out = run(["terraform", "validate"], cwd=d)
        detail = out if rc != 0 else ""
        ok = check(f"{module}/", rc == 0, detail)
        all_ok = all_ok and ok
    return all_ok


# ── 4. CLI tools ─────────────────────────────────────────────────────────────

def check_cli_tools() -> bool:
    print("\n── CLI tools ─────────────────────────────────────────────────")
    all_ok = True

    # Terraform
    rc, out = run(["terraform", "version"])
    ok = check("terraform", rc == 0, "Install from https://developer.hashicorp.com/terraform/install")
    all_ok = all_ok and ok
    if rc == 0:
        print(f"         {out.splitlines()[0]}")

    # AWS CLI
    rc, out = run(["aws", "--version"])
    ok = check("aws cli", rc == 0,
               "Install from https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html")
    all_ok = all_ok and ok
    if rc == 0:
        print(f"         {out.splitlines()[0]}")

    # Azure CLI
    import platform
    az_bin = "az.cmd" if platform.system() == "Windows" else "az"
    rc, out = run([az_bin, "--version"])
    ok = check("azure cli (az)", rc == 0,
               "Install from https://learn.microsoft.com/en-us/cli/azure/install-azure-cli")
    all_ok = all_ok and ok
    if rc == 0:
        # az --version output is multi-line; print just the first line
        print(f"         {out.splitlines()[0]}")

    return all_ok


# ── 5. Environment variables ──────────────────────────────────────────────────

def check_env_vars() -> bool:
    import os
    from pathlib import Path

    is_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    all_ok = True

    # Always required — orchestrator.py uses AZURE_VM_PASSWORD directly.
    print("\n── Required environment variables ────────────────────────────")
    present = bool(os.environ.get("AZURE_VM_PASSWORD"))
    ok = check("AZURE_VM_PASSWORD (Azure VM admin password)", present,
               "Set with: $env:AZURE_VM_PASSWORD = 'YourPassword'")
    all_ok = all_ok and ok

    # AWS credentials — env vars OR ~/.aws/credentials both work for local runs.
    aws_via_env  = bool(os.environ.get("AWS_ACCESS_KEY_ID"))
    aws_via_file = (Path.home() / ".aws" / "credentials").exists()
    if is_ci:
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"):
            ok = check(f"{var}", bool(os.environ.get(var)),
                       f"Set with: $env:{var} = '...'")
            all_ok = all_ok and ok
    else:
        ok = aws_via_env or aws_via_file
        detail = "Set env vars or run: aws configure" if not ok else ""
        check("AWS credentials (env vars or ~/.aws/credentials)", ok, detail)
        if not ok:
            all_ok = False
        if not os.environ.get("AWS_DEFAULT_REGION"):
            check("AWS_DEFAULT_REGION", False,
                  "Set with: $env:AWS_DEFAULT_REGION = 'ap-southeast-2'")
            all_ok = False

    # Azure service principal — only required in CI. Locally, az login is enough.
    if is_ci:
        print("\n── Azure service principal (CI only) ─────────────────────────")
        for var in ("ARM_SUBSCRIPTION_ID", "ARM_CLIENT_ID",
                    "ARM_CLIENT_SECRET", "ARM_TENANT_ID"):
            ok = check(f"{var}", bool(os.environ.get(var)),
                       f"Add as a GitHub Actions secret")
            all_ok = all_ok and ok
    else:
        import platform
        az_bin = "az.cmd" if platform.system() == "Windows" else "az"
        print("\n── Azure login (local) ───────────────────────────────────────")
        rc, out = run([az_bin, "account", "show", "--query", "id", "-o", "tsv"])
        ok = rc == 0 and out.strip() != ""
        check("az login active (az account show)", ok,
              "Run: az login")
        if not ok:
            all_ok = False

    return all_ok


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 60)
    print("  PL-IaC Orchestrator — Pre-flight Validation")
    print("=" * 60)

    results = [
        check_python_syntax(),
        check_tf_fmt(),
        check_tf_validate(),
        check_cli_tools(),
        check_env_vars(),
    ]

    passed = sum(results)
    total  = len(results)
    print(f"\n{'='*60}")
    print(f"  {passed}/{total} checks passed")
    print(f"{'='*60}")

    if passed < total:
        print("\nFix the FAIL items above before running benchmark.py.\n")
        sys.exit(1)
    else:
        print("\nAll checks passed. Safe to run benchmark.py.\n")


if __name__ == "__main__":
    main()
