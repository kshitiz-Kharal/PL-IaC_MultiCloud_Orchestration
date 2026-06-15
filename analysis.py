
"""
analysis.py — Statistical Analysis and Plot Generation
=======================================================
Reads CSVs from benchmark.py (Exp 1) and inject.py (Exp 2), computes
mean / std / median / IQR / p95 / 95% CI, and saves figures to results/figures/.

Usage:
    python analysis.py                        # auto-discovers latest CSVs
    python analysis.py --exp1 data/exp1_...csv
    python analysis.py --exp2 data/exp2_...csv
    python analysis.py --exp1 ... --exp2 ...
"""

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

BASE        = Path(__file__).parent
RESULTS_DIR = BASE / "results" / "figures"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

matplotlib.rcParams["font.family"] = "DejaVu Sans"
matplotlib.rcParams["figure.dpi"]  = 150
ITU_T_THRESHOLD_MS = 150   # ITU-T G.114 max RTT for real-time communications


# ── Statistics helpers ────────────────────────────────────────────────────────

def ci95(series: pd.Series) -> tuple[float, float]:
    """95% CI using t-distribution (correct for finite n)."""
    s = series.dropna()
    if len(s) < 2:
        return float("nan"), float("nan")
    return stats.t.interval(0.95, df=len(s)-1, loc=s.mean(), scale=stats.sem(s))


def describe(series: pd.Series, label: str) -> dict:
    s = series.dropna()
    lo, hi = ci95(s)
    return {
        "metric":  label,
        "n":       len(s),
        "mean":    s.mean(),
        "std":     s.std(),
        "median":  s.median(),
        "IQR":     s.quantile(0.75) - s.quantile(0.25),
        "p95":     s.quantile(0.95),
        "CI95_lo": lo,
        "CI95_hi": hi,
        "min":     s.min(),
        "max":     s.max(),
    }


def print_table(rows: list) -> None:
    cols = ["metric", "n", "mean", "std", "median", "IQR", "p95", "CI95_lo", "CI95_hi"]
    w    = {"metric": 38, "n": 5, "mean": 10, "std": 9,
            "median": 10, "IQR": 9, "p95": 9, "CI95_lo": 10, "CI95_hi": 10}
    hdr = "".join(c.ljust(w[c]) for c in cols)
    sep = "─" * len(hdr)
    print(f"\n{sep}\n{hdr}\n{sep}")
    for r in rows:
        line = ""
        for c in cols:
            v = r.get(c)
            if v is None:
                line += " " * w[c]
            elif isinstance(v, float):
                line += f"{v:>8.2f}  "
            else:
                line += str(v).ljust(w[c])
        print(line)
    print(sep)


# ── Experiment 1 ─────────────────────────────────────────────────────────────

def analyse_exp1(csv_path: Path) -> None:
    print(f"\n{'='*60}")
    print(f"  EXPERIMENT 1 — Steady-State Performance")
    print(f"  {csv_path.name}")
    print(f"{'='*60}")

    df = pd.read_csv(csv_path)
    n  = len(df)

    # ── Summary statistics ────────────────────────────────────────────────────
    metrics = [
        # Totals
        ("Total provisioning time (s)",        "t_total_provisioning_s"),
        ("Total Terraform apply time (s)",      "t_total_prov_s"),
        ("Total Python overhead (s)",           "t_total_proc_s"),
        # Phase 1 breakdown
        ("Phase 1 — Azure base, total (s)",     "t_phase1_s"),
        ("Phase 1 — Terraform apply (s)",       "t_phase1_prov_s"),
        ("Phase 1 — Python overhead (s)",       "t_phase1_proc_s"),
        # Phase 2 breakdown
        ("Phase 2 — AWS deploy, total (s)",     "t_phase2_s"),
        ("Phase 2 — Terraform apply (s)",       "t_phase2_prov_s"),
        ("Phase 2 — Python overhead (s)",       "t_phase2_proc_s"),
        # Phase 3 breakdown
        ("Phase 3 — Azure connect, total (s)",  "t_phase3_s"),
        ("Phase 3 — Terraform apply (s)",       "t_phase3_prov_s"),
        ("Phase 3 — Python overhead (s)",       "t_phase3_proc_s"),
        # Tunnel convergence
        ("Tunnel convergence time (s)",         "tunnel_convergence_s"),
        # Teardown
        ("Destroy time (s)",                    "t_destroy_s"),
        # Network
        ("ICMP RTT average (ms)",               "icmp_rtt_avg_ms"),
        ("ICMP RTT minimum (ms)",               "icmp_rtt_min_ms"),
        ("ICMP RTT maximum (ms)",               "icmp_rtt_max_ms"),
        ("ICMP packet loss (%)",                "icmp_packet_loss_pct"),
        ("TCP Azure→AWS (Mbps)",                "tcp_az_to_aws_mbps"),
        ("TCP AWS→Azure (Mbps)",                "tcp_aws_to_az_mbps"),
        ("UDP throughput (Mbps)",               "udp_mbps"),
        ("UDP jitter (ms)",                     "jitter_ms"),
        ("Active tunnel count",                 "active_tunnel_count"),
    ]
    rows = [describe(df[col], lbl) for lbl, col in metrics if col in df.columns]
    print_table(rows)

    # ── Summary counts ────────────────────────────────────────────────────────
    if "icmp_success" in df.columns:
        rate = df["icmp_success"].astype(bool).mean() * 100
        print(f"\nICMP success rate : {rate:.1f}%  ({int(df['icmp_success'].sum())}/{n} cycles)")
    if "success" in df.columns:
        rate = df["success"].astype(bool).mean() * 100
        print(f"Deploy success rate: {rate:.1f}%  ({int(df['success'].sum())}/{n} cycles)")
    if "icmp_rtt_avg_ms" in df.columns:
        pct_ok = (df["icmp_rtt_avg_ms"].dropna() < ITU_T_THRESHOLD_MS).mean() * 100
        print(f"ITU-T G.114 compliance (RTT < 150 ms): {pct_ok:.1f}% of cycles")
    if "aws_state_resource_count" in df.columns and "azure_state_resource_count" in df.columns:
        print(f"\nState isolation (mean resource counts):")
        print(f"  AWS  state: {df['aws_state_resource_count'].mean():.1f} resources")
        print(f"  Azure state:{df['azure_state_resource_count'].mean():.1f} resources")

    # ── Failure breakdown ─────────────────────────────────────────────────────
    if "failure_type" in df.columns and "success" in df.columns:
        failed = df[~df["success"].astype(bool)]
        if len(failed) > 0:
            print(f"\nFailure breakdown ({len(failed)} failed cycles):")
            for ft, cnt in failed["failure_type"].value_counts().items():
                print(f"  {ft}: {cnt}")
            if "failure_phase" in df.columns:
                print("  By phase:")
                for ph, cnt in failed["failure_phase"].value_counts().sort_index().items():
                    print(f"    Phase {ph}: {cnt}")

    # ── Figure 3: Per-phase wall-clock duration ───────────────────────────────
    phase_cols = [(c, l) for c, l in [
        ("t_phase1_s", "Phase 1\nAzure Base"),
        ("t_phase2_s", "Phase 2\nAWS Deploy"),
        ("t_phase3_s", "Phase 3\nAzure Connect"),
        ("t_destroy_s","Destroy"),
    ] if c in df.columns]

    if phase_cols:
        fig, ax = plt.subplots(figsize=(10, 5))
        cols, lbls = zip(*phase_cols)
        means = [df[c].mean() / 60 for c in cols]
        cis   = [ci95(df[c] / 60) for c in cols]
        errs  = [[m - lo for m, (lo, _) in zip(means, cis)],
                 [hi - m for m, (_, hi) in zip(means, cis)]]
        clrs  = ["#2E75B6", "#E07000", "#538135", "#C00000"]
        ax.bar(range(len(cols)), means, color=clrs[:len(cols)], width=0.5,
               yerr=errs, capsize=6, error_kw={"elinewidth": 1.5})
        ax.set_xticks(range(len(cols)))
        ax.set_xticklabels(lbls)
        ax.set_ylabel("Duration (minutes)")
        ax.set_title(f"Figure 3: Per-Phase Duration — Mean ± 95% CI  (n={n})")
        ax.yaxis.grid(True, linestyle="--", alpha=0.5)
        fig.tight_layout()
        out = RESULTS_DIR / "exp1_phase_breakdown.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"\nSaved: {out}")

    # ── Figure 3b: Total provisioning distribution ────────────────────────────
    if "t_total_provisioning_s" in df.columns:
        s = df["t_total_provisioning_s"].dropna() / 60
        lo_m, hi_m = ci95(s)
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.hist(s, bins=10, color="#2E75B6", edgecolor="white", alpha=0.85)
        ax.axvline(s.mean(),           color="#C00000", lw=2,
                   label=f"Mean {s.mean():.1f} min")
        ax.axvline(s.median(),         color="#538135", lw=2, linestyle="--",
                   label=f"Median {s.median():.1f} min")
        ax.axvline(s.quantile(0.95),   color="#E07000", lw=1.5, linestyle=":",
                   label=f"p95 {s.quantile(0.95):.1f} min")
        ax.axvspan(lo_m, hi_m, alpha=0.15, color="#C00000", label="95% CI")
        ax.set_xlabel("Total Provisioning Time (minutes)")
        ax.set_ylabel("Frequency")
        ax.set_title(f"Figure 3b: Total Provisioning Time Distribution  (n={n})")
        ax.legend()
        fig.tight_layout()
        out = RESULTS_DIR / "exp1_total_distribution.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"Saved: {out}")

    # ── Figure 3c: Provisioning vs processing overhead (stacked bar) ──────────
    prov_triples = [
        ("t_phase1_prov_s", "t_phase1_proc_s", "Phase 1\nAzure Base"),
        ("t_phase2_prov_s", "t_phase2_proc_s", "Phase 2\nAWS Deploy"),
        ("t_phase3_prov_s", "t_phase3_proc_s", "Phase 3\nAzure Connect"),
    ]
    prov_triples = [
        (pv, pr, lb) for pv, pr, lb in prov_triples
        if pv in df.columns and pr in df.columns
    ]

    if prov_triples:
        fig, ax = plt.subplots(figsize=(9, 5))
        x        = np.arange(len(prov_triples))
        pv_means = [df[pv].mean() / 60 for pv, _, _ in prov_triples]
        pr_means = [df[pr].mean() / 60 for _, pr, _ in prov_triples]
        lbls     = [lb for _, _, lb in prov_triples]
        ax.bar(x, pv_means, 0.5, label="Terraform apply (provisioning)", color="#2E75B6")
        ax.bar(x, pr_means, 0.5, bottom=pv_means,
               label="Python orchestration (processing)", color="#FFC000")
        ax.set_xticks(x)
        ax.set_xticklabels(lbls)
        ax.set_ylabel("Duration (minutes)")
        ax.set_title(f"Figure 3c: Provisioning vs Processing Overhead — Mean  (n={n})")
        ax.legend()
        ax.yaxis.grid(True, linestyle="--", alpha=0.4)
        fig.tight_layout()
        out = RESULTS_DIR / "exp1_prov_vs_proc.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"Saved: {out}")

    # ── Figure 3d: Tunnel convergence time ────────────────────────────────────
    if "tunnel_convergence_s" in df.columns:
        s = df["tunnel_convergence_s"].dropna()
        if len(s) > 0:
            fig, axes = plt.subplots(1, 2, figsize=(12, 4))

            # Left: scatter over cycles with mean and p95 reference lines
            axes[0].plot(range(1, len(s)+1), s, "o-", color="#2E75B6", ms=5, lw=1.5)
            axes[0].axhline(s.mean(), color="#C00000", lw=2, linestyle="--",
                            label=f"Mean {s.mean():.0f}s")
            axes[0].axhline(s.quantile(0.95), color="#E07000", lw=1.5, linestyle=":",
                            label=f"p95 {s.quantile(0.95):.0f}s")
            axes[0].set_xlabel("Cycle")
            axes[0].set_ylabel("Convergence Time (s)")
            axes[0].set_title("Tunnel Convergence per Cycle")
            axes[0].legend()
            axes[0].yaxis.grid(True, linestyle="--", alpha=0.4)

            # Right: box plot
            bp = axes[1].boxplot(s, patch_artist=True,
                                 medianprops={"color": "black", "linewidth": 2})
            bp["boxes"][0].set_facecolor("#BDD7EE")
            axes[1].set_xticklabels(["Convergence\nTime"])
            axes[1].set_ylabel("Seconds")
            axes[1].set_title(
                f"Figure 3d: Tunnel Convergence Distribution\n"
                f"median={s.median():.0f}s  p95={s.quantile(0.95):.0f}s  n={len(s)}"
            )
            axes[1].yaxis.grid(True, linestyle="--", alpha=0.4)

            fig.tight_layout()
            out = RESULTS_DIR / "exp1_convergence.png"
            fig.savefig(str(out), bbox_inches="tight", facecolor="white")
            plt.close(fig)
            print(f"Saved: {out}")

    # ── Figure 4: ICMP RTT vs ITU-T G.114 threshold ───────────────────────────
    if "icmp_rtt_avg_ms" in df.columns:
        s = df["icmp_rtt_avg_ms"].dropna()
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(range(1, len(s)+1), s, "o-", color="#2E75B6", ms=5, lw=1.5,
                label="ICMP RTT avg (ms)")
        ax.axhline(ITU_T_THRESHOLD_MS, color="#C00000", lw=2, linestyle="--",
                   label=f"ITU-T G.114 threshold ({ITU_T_THRESHOLD_MS} ms)")
        ax.axhline(s.quantile(0.95), color="#E07000", lw=1.5, linestyle=":",
                   label=f"p95 ({s.quantile(0.95):.0f} ms)")
        ax.fill_between(range(1, len(s)+1), s, ITU_T_THRESHOLD_MS,
                        where=(s > ITU_T_THRESHOLD_MS),
                        color="#C00000", alpha=0.2, label="Above threshold")
        ax.set_xlabel("Cycle")
        ax.set_ylabel("RTT (ms)")
        ax.set_title("Figure 4: ICMP Round-Trip Time vs ITU-T G.114 Threshold")
        ax.legend()
        ax.yaxis.grid(True, linestyle="--", alpha=0.4)
        fig.tight_layout()
        out = RESULTS_DIR / "exp1_rtt_vs_threshold.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"Saved: {out}")

    # ── Figure 5: iperf3 bidirectional throughput ─────────────────────────────
    if "tcp_az_to_aws_mbps" in df.columns and "tcp_aws_to_az_mbps" in df.columns:
        fwd = df["tcp_az_to_aws_mbps"].dropna()
        rev = df["tcp_aws_to_az_mbps"].dropna()
        n_  = min(len(fwd), len(rev))
        fig, ax = plt.subplots(figsize=(10, 4))
        x = np.arange(n_)
        w = 0.35
        ax.bar(x - w/2, fwd[:n_], w, label="TCP Azure→AWS", color="#2E75B6")
        ax.bar(x + w/2, rev[:n_], w, label="TCP AWS→Azure", color="#E07000")
        ax.set_xlabel("Cycle")
        ax.set_ylabel("Throughput (Mbps)")
        ax.set_title("Figure 5: iperf3 TCP Bidirectional Throughput")
        ax.legend()
        ax.yaxis.grid(True, linestyle="--", alpha=0.4)
        fig.tight_layout()
        out = RESULTS_DIR / "exp1_throughput.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"Saved: {out}")


# ── Experiment 2 ─────────────────────────────────────────────────────────────

def analyse_exp2(csv_path: Path) -> None:
    print(f"\n{'='*60}")
    print(f"  EXPERIMENT 2 — Fault Injection Comparison")
    print(f"  {csv_path.name}")
    print(f"{'='*60}")

    df = pd.read_csv(csv_path)

    print("\n--- Blast Radius: resources provisioned despite fault ---")
    print("(Score: 2=both clouds, 1=one cloud only, 0=neither)\n")
    for cond, col in [("Framework", "fw_blast_radius_score"),
                       ("Monolith",  "mono_blast_radius_score")]:
        if col not in df.columns:
            continue
        print(f"  {cond}:")
        for s in [0, 1, 2]:
            cnt = (df[col] == s).sum()
            lbl = {0: "neither", 1: "one cloud", 2: "both clouds"}[s]
            print(f"    {s}/2 ({lbl}): {cnt} cycles ({cnt/len(df)*100:.0f}%)")

    stat_rows = []
    for lbl, col in [("Framework recovery (s)", "fw_recovery_time_s"),
                     ("Monolith recovery (s)",   "mono_recovery_time_s")]:
        if col in df.columns:
            stat_rows.append(describe(df[col], lbl))
    if stat_rows:
        print("\n--- Recovery Time ---")
        print_table(stat_rows)

    if "fault_category" in df.columns:
        print("\n--- By fault category ---")
        for cat in sorted(df["fault_category"].unique()):
            sub = df[df["fault_category"] == cat]
            fw  = sub["fw_blast_radius_score"].mean()  if "fw_blast_radius_score"   in sub else "N/A"
            mo  = sub["mono_blast_radius_score"].mean() if "mono_blast_radius_score" in sub else "N/A"
            print(f"  {cat:15s}  n={len(sub):2d}  "
                  f"FW={fw:.2f}/2  Mono={mo:.2f}/2")

    # Figure 6: blast radius grouped bar
    if "fw_blast_radius_score" in df.columns and "mono_blast_radius_score" in df.columns:
        fig, ax = plt.subplots(figsize=(8, 5))
        scores = [0, 1, 2]
        lbls   = ["0/2\n(neither)", "1/2\n(one cloud)", "2/2\n(both)"]
        fw_c   = [(df["fw_blast_radius_score"]   == s).sum() for s in scores]
        mo_c   = [(df["mono_blast_radius_score"] == s).sum() for s in scores]
        x = np.arange(3)
        ax.bar(x - 0.2, fw_c, 0.35, label="PL-IaC Framework",   color="#2E75B6")
        ax.bar(x + 0.2, mo_c, 0.35, label="Monolithic Baseline", color="#E07000")
        ax.set_xticks(x)
        ax.set_xticklabels(lbls)
        ax.set_ylabel("Number of cycles")
        ax.set_xlabel("Blast Radius Score")
        ax.set_title("Figure 6: Blast Radius — Framework vs Monolith")
        ax.legend()
        ax.yaxis.grid(True, linestyle="--", alpha=0.4)
        fig.tight_layout()
        out = RESULTS_DIR / "exp2_blast_radius.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"\nSaved: {out}")

    # Figure 7: recovery time boxplot
    if "fw_recovery_time_s" in df.columns and "mono_recovery_time_s" in df.columns:
        fw_r = df["fw_recovery_time_s"].dropna()   / 60
        mo_r = df["mono_recovery_time_s"].dropna() / 60
        fig, ax = plt.subplots(figsize=(6, 5))
        bp = ax.boxplot([fw_r, mo_r],
                        labels=["PL-IaC\nFramework", "Monolithic\nBaseline"],
                        patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2})
        bp["boxes"][0].set_facecolor("#BDD7EE")
        bp["boxes"][1].set_facecolor("#FFE0B0")
        ax.set_ylabel("Recovery Time (minutes)")
        ax.set_title("Figure 7: Recovery Time After Fault Correction")
        ax.yaxis.grid(True, linestyle="--", alpha=0.4)
        fig.tight_layout()
        out = RESULTS_DIR / "exp2_recovery_time.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"Saved: {out}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp1", type=Path)
    parser.add_argument("--exp2", type=Path)
    args = parser.parse_args()

    if not args.exp1 and not args.exp2:
        e1 = sorted((BASE / "data").glob("exp1_steady_state_*.csv"))
        e2 = sorted((BASE / "data").glob("exp2_fault_injection_*.csv"))
        if e1: args.exp1 = e1[-1]; print(f"Auto: {args.exp1.name}")
        if e2: args.exp2 = e2[-1]; print(f"Auto: {args.exp2.name}")

    if not args.exp1 and not args.exp2:
        sys.exit("No CSVs found. Run benchmark.py and/or inject.py first.")

    if args.exp1: analyse_exp1(args.exp1)
    if args.exp2: analyse_exp2(args.exp2)

    print(f"\nAll figures saved to: {RESULTS_DIR}")


if __name__ == "__main__":
    main()
