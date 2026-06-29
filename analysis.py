
"""
analysis.py — Statistical Analysis and Plot Generation
=======================================================
Reads all matching CSVs from benchmark.py (Exp 1) and inject.py (Exp 2).

Computes:
  • Descriptive statistics: mean, std, CV, median, IQR, p95, 95% t-CI
  • Normality: Shapiro-Wilk test (α=0.05) + Q-Q plots
  • Exp 1 success rates: Wilson score 95% CI
  • Exp 2 hypothesis tests: Wilcoxon signed-rank + Mann-Whitney U
  • Effect sizes: Cliff's Delta with magnitude labels

Usage:
    python analysis.py                          # auto-discovers all CSVs
    python analysis.py --exp1 data/exp1_*.csv
    python analysis.py --exp2 data/exp2_*.csv
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

ITU_T_THRESHOLD_MS = 150   # ITU-T G.114 max RTT
ALPHA              = 0.05  # significance level


# ── Statistical helpers ────────────────────────────────────────────────────────

def ci95(series: pd.Series) -> tuple[float, float]:
    """95% t-distribution CI (correct for finite n, unknown σ)."""
    s = series.dropna()
    if len(s) < 2:
        return float("nan"), float("nan")
    return stats.t.interval(0.95, df=len(s) - 1, loc=s.mean(), scale=stats.sem(s))


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for a binomial proportion."""
    if n == 0:
        return float("nan"), float("nan")
    p      = k / n
    denom  = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = (z / denom) * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return centre - margin, centre + margin


def cliffs_delta(x: pd.Series, y: pd.Series) -> float:
    """Cliff's delta: P(xi > yj) - P(xi < yj) over all pairs."""
    xa, ya = np.array(x.dropna()), np.array(y.dropna())
    n1, n2 = len(xa), len(ya)
    if n1 == 0 or n2 == 0:
        return float("nan")
    more = sum(1 for xi in xa for yj in ya if xi > yj)
    less = sum(1 for xi in xa for yj in ya if xi < yj)
    return (more - less) / (n1 * n2)


def delta_label(d: float) -> str:
    ad = abs(d)
    if ad >= 0.474: return "large"
    if ad >= 0.330: return "medium"
    if ad >= 0.147: return "small"
    return "negligible"


def describe(series: pd.Series, label: str) -> dict:
    s    = series.dropna()
    lo, hi = ci95(s)
    mean = s.mean()
    std  = s.std()
    cv   = std / mean if mean != 0 else float("nan")
    return {
        "metric":  label,
        "n":       len(s),
        "mean":    mean,
        "std":     std,
        "CV":      cv,
        "median":  s.median(),
        "IQR":     s.quantile(0.75) - s.quantile(0.25),
        "p95":     s.quantile(0.95),
        "CI95_lo": lo,
        "CI95_hi": hi,
    }


def print_table(rows: list) -> None:
    cols = ["metric", "n", "mean", "std", "CV", "median", "IQR", "p95", "CI95_lo", "CI95_hi"]
    w    = {"metric": 38, "n": 5, "mean": 10, "std": 9, "CV": 7,
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
                fmt = f"{v:>5.3f}  " if c == "CV" else f"{v:>8.2f}  "
                line += fmt
            else:
                line += str(v).ljust(w[c])
        print(line)
    print(sep)


def shapiro_table(df: pd.DataFrame, cols: list) -> None:
    """Run Shapiro-Wilk on each column and print a results table."""
    print(f"\n── Shapiro-Wilk Normality Test  (α = {ALPHA}) ──")
    print(f"  {'Metric':<38} {'W':>8}  {'p-value':>9}  Result")
    print(f"  {'─'*38}  {'─'*8}  {'─'*9}  {'─'*10}")
    for lbl, col in cols:
        if col not in df.columns:
            continue
        s = df[col].dropna()
        if len(s) < 3:
            continue
        w_stat, p = stats.shapiro(s)
        result = "normal" if p > ALPHA else "NON-NORMAL"
        print(f"  {lbl:<38} {w_stat:>8.4f}  {p:>9.4f}  {result}")


# ── Data loading ───────────────────────────────────────────────────────────────

def load_exp1(paths) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Concatenate all exp1 CSVs.
    Returns (df_all, df_ci):
      df_all — every row (for total counts / failure reporting)
      df_ci  — github_actions rows only (used for all inferential statistics)

    Cycle 1 local run is excluded from df_ci because Phases 1 and 2 were
    pre-deployed before the script ran — it is not a full-lifecycle observation
    and violates the provision-from-scratch protocol.
    """
    df = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    df = df.sort_values("timestamp_utc").reset_index(drop=True)

    if "run_source" in df.columns:
        n_local = (df["run_source"] != "github_actions").sum()
        if n_local:
            print(f"  Note: {n_local} local run(s) excluded from inferential analysis "
                  f"(partial lifecycle — Phases 1+2 pre-deployed, not full provision-from-scratch).")
        df_ci = df[df["run_source"] == "github_actions"].copy()
    else:
        df_ci = df.copy()

    print(f"  Exp1: {len(df)} total rows, {len(df_ci)} CI rows used for statistics.")
    return df, df_ci


def load_exp2(paths) -> pd.DataFrame:
    """
    Concatenate all exp2 CSVs and drop rows where the fault injection script
    errored before writing monolith data (blank fw_blast_radius_score or
    mono_blast_radius_score — indicates a failed injection cycle).
    """
    df = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    df = df.sort_values("timestamp_utc").reset_index(drop=True)

    valid    = df["fw_blast_radius_score"].notna() & df["mono_blast_radius_score"].notna()
    n_invalid = (~valid).sum()
    if n_invalid:
        print(f"  Note: {n_invalid} incomplete cycle(s) excluded "
              f"(fault injection error — missing blast-radius data).")
    df = df[valid].copy()
    print(f"  Exp2: {len(df)} valid cycles used for analysis.")
    return df


# ── Experiment 1 ───────────────────────────────────────────────────────────────

def analyse_exp1(paths) -> None:
    print(f"\n{'='*60}")
    print(f"  EXPERIMENT 1 — Steady-State Performance")
    print(f"{'='*60}")

    df_all, df = load_exp1(paths)
    n = len(df)

    # ── Descriptive statistics ─────────────────────────────────────────────────
    metrics = [
        ("Total provisioning time (s)",        "t_total_provisioning_s"),
        ("Total Terraform apply time (s)",      "t_total_prov_s"),
        ("Total Python overhead (s)",           "t_total_proc_s"),
        ("Phase 1 — Azure base, total (s)",     "t_phase1_s"),
        ("Phase 1 — Terraform apply (s)",       "t_phase1_prov_s"),
        ("Phase 1 — Python overhead (s)",       "t_phase1_proc_s"),
        ("Phase 2 — AWS deploy, total (s)",     "t_phase2_s"),
        ("Phase 2 — Terraform apply (s)",       "t_phase2_prov_s"),
        ("Phase 2 — Python overhead (s)",       "t_phase2_proc_s"),
        ("Phase 3 — Azure connect, total (s)",  "t_phase3_s"),
        ("Phase 3 — Terraform apply (s)",       "t_phase3_prov_s"),
        ("Phase 3 — Python overhead (s)",       "t_phase3_proc_s"),
        ("Tunnel convergence time (s)",         "tunnel_convergence_s"),
        ("Destroy time (s)",                    "t_destroy_s"),
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
    print(f"\n── Descriptive Statistics  (n={n}, CI runs only) ──")
    print_table(rows)

    # ── ε_proc ────────────────────────────────────────────────────────────────
    if "t_total_proc_s" in df.columns and "t_total_provisioning_s" in df.columns:
        eps = (df["t_total_proc_s"] / df["t_total_provisioning_s"] * 100).dropna()
        print(f"\n── Python Control-Plane Overhead (ε_proc) ──")
        print(f"  mean : {eps.mean():.4f}%")
        print(f"  min  : {eps.min():.4f}%")
        print(f"  max  : {eps.max():.4f}%")
        verdict = "NEGLIGIBLE (<0.5%)" if eps.mean() < 0.5 else "HIGH — investigate"
        print(f"  Verdict: {verdict}")

    # ── Success rates (Wilson CI) ──────────────────────────────────────────────
    print(f"\n── Success Rates (Wilson 95% CI) ──")
    for label, col in [("Deploy success", "success"), ("ICMP success", "icmp_success")]:
        if col not in df.columns:
            continue
        k  = int(df[col].astype(bool).sum())
        nn = int(df[col].notna().sum())
        lo, hi = wilson_ci(k, nn)
        print(f"  {label}: {k}/{nn} = {k/nn*100:.1f}%  "
              f"Wilson 95% CI [{lo*100:.1f}%, {hi*100:.1f}%]")

    # ── ITU-T G.114 compliance ─────────────────────────────────────────────────
    if "icmp_rtt_avg_ms" in df.columns:
        rtt = df["icmp_rtt_avg_ms"].dropna()
        pct_ok = (rtt < ITU_T_THRESHOLD_MS).mean() * 100
        print(f"\n── ITU-T G.114 Compliance (RTT < {ITU_T_THRESHOLD_MS} ms) ──")
        print(f"  {pct_ok:.1f}% of cycles within threshold  "
              f"(max observed: {rtt.max():.1f} ms)")

    # ── Shapiro-Wilk normality ─────────────────────────────────────────────────
    sw_metrics = [(l, c) for l, c in metrics
                  if c in df.columns and c != "active_tunnel_count"]
    shapiro_table(df, sw_metrics)

    # ── Failure breakdown ──────────────────────────────────────────────────────
    if "failure_type" in df_all.columns and "success" in df_all.columns:
        failed = df_all[~df_all["success"].astype(bool)]
        if len(failed) > 0:
            print(f"\nFailure breakdown ({len(failed)} failed cycles):")
            for ft, cnt in failed["failure_type"].value_counts().items():
                print(f"  {ft}: {cnt}")

    # ── Figure 3: Phase duration box plots ────────────────────────────────────
    phase_cols = [(c, l) for c, l in [
        ("t_phase1_s",  "Phase 1\nAzure Base"),
        ("t_phase2_s",  "Phase 2\nAWS Deploy"),
        ("t_phase3_s",  "Phase 3\nAzure Connect"),
        ("t_destroy_s", "Destroy"),
    ] if c in df.columns]

    if phase_cols:
        data_bp = [df[c].dropna() / 60 for c, _ in phase_cols]
        lbls_bp = [l for _, l in phase_cols]
        fig, ax = plt.subplots(figsize=(10, 5))
        bp = ax.boxplot(data_bp, tick_labels=lbls_bp, patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2})
        clrs = ["#2E75B6", "#E07000", "#538135", "#C00000"]
        for patch, c in zip(bp["boxes"], clrs):
            patch.set_facecolor(c)
            patch.set_alpha(0.7)
        ax.set_ylabel("Duration (minutes)")
        ax.set_title(f"Figure 3: Per-Phase Duration Distribution  (n={n})")
        ax.yaxis.grid(True, linestyle="--", alpha=0.2)
        fig.tight_layout()
        out = RESULTS_DIR / "exp1_phase_breakdown.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"\nSaved: {out}")

    # ── Figure 3b: eCDF of total provisioning time ───────────────────────────
    if "t_total_provisioning_s" in df.columns:
        s_sorted = np.sort(df["t_total_provisioning_s"].dropna() / 60)
        n_s = len(s_sorted)
        y_cdf = np.arange(1, n_s + 1) / n_s
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.step(s_sorted, y_cdf, where="post", color="#2E75B6", lw=2)
        ax.axvline(np.percentile(s_sorted, 80), color="#E07000", lw=1.5, linestyle=":",
                   label=f"p80 = {np.percentile(s_sorted, 80):.1f} min")
        ax.axvline(np.percentile(s_sorted, 95), color="#C00000", lw=1.5, linestyle="--",
                   label=f"p95 = {np.percentile(s_sorted, 95):.1f} min")
        ax.set_xlabel("Total Provisioning Time (minutes)")
        ax.set_ylabel("Cumulative Proportion")
        ax.set_title(f"Figure 3b: eCDF — Total Provisioning Time  (n={n_s})")
        ax.legend()
        ax.yaxis.grid(True, linestyle="--", alpha=0.2)
        fig.tight_layout()
        out = RESULTS_DIR / "exp1_ecdf.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"Saved: {out}")


    # ── Figure 3e: Q-Q plots ───────────────────────────────────────────────────
    qq_cols = [
        ("t_total_provisioning_s", "Total Provisioning (s)"),
        ("t_phase1_s",             "Phase 1 — Azure Base (s)"),
        ("t_phase2_s",             "Phase 2 — AWS Deploy (s)"),
        ("t_phase3_s",             "Phase 3 — Azure Connect (s)"),
        ("icmp_rtt_avg_ms",        "ICMP RTT avg (ms)"),
        ("tcp_az_to_aws_mbps",     "TCP Az→AWS (Mbps)"),
    ]
    qq_cols = [(c, l) for c, l in qq_cols if c in df.columns]
    if qq_cols:
        ncols = 3
        nrows = (len(qq_cols) + ncols - 1) // ncols
        fig, axes = plt.subplots(nrows, ncols, figsize=(14, 4 * nrows))
        axes_flat = axes.flatten() if nrows > 1 else [axes] if ncols == 1 else axes.flatten()
        for i, (col, lbl) in enumerate(qq_cols):
            s = df[col].dropna()
            (osm, osr), (slope, intercept, _r) = stats.probplot(s, dist="norm")
            axes_flat[i].scatter(osm, osr, color="#2E75B6", s=20, alpha=0.8)
            axes_flat[i].plot(osm, slope * np.array(osm) + intercept,
                              color="#C00000", lw=1.5)
            axes_flat[i].set_title(lbl, fontsize=9)
            axes_flat[i].set_xlabel("Theoretical quantiles", fontsize=8)
            axes_flat[i].set_ylabel("Sample quantiles", fontsize=8)
            axes_flat[i].grid(True, linestyle="--", alpha=0.2)
        for j in range(i + 1, len(axes_flat)):
            axes_flat[j].set_visible(False)
        fig.suptitle(f"Figure 3e: Q-Q Plots — Normality Check  (n={n})", fontsize=11)
        fig.tight_layout()
        (RESULTS_DIR / "appendix").mkdir(exist_ok=True)
        out = RESULTS_DIR / "appendix" / "exp1_qq_plots.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"Saved: {out}  [appendix]")

    # ── Figure 4: ICMP RTT vs ITU-T G.114 ─────────────────────────────────────
    if "icmp_rtt_avg_ms" in df.columns:
        s = df["icmp_rtt_avg_ms"].dropna()
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(range(1, len(s) + 1), s, "o-", color="#2E75B6", ms=5, lw=1.5,
                label="ICMP RTT avg (ms)")
        ax.axhline(s.quantile(0.95), color="#E07000", lw=1.5, linestyle=":",
                   label=f"p95 ({s.quantile(0.95):.0f} ms)")
        ax.set_ylim(0, 20)
        ax.annotate(
            f"ITU-T G.114 limit = {ITU_T_THRESHOLD_MS} ms\n(not shown at this scale)",
            xy=(0.98, 0.97), xycoords="axes fraction",
            ha="right", va="top", fontsize=8, color="#C00000",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                      edgecolor="#C00000", alpha=0.8),
        )
        ax.set_xlabel("Cycle")
        ax.set_ylabel("RTT (ms)")
        ax.set_title("Figure 4: ICMP Round-Trip Time vs ITU-T G.114 Threshold")
        ax.legend()
        ax.yaxis.grid(True, linestyle="--", alpha=0.2)
        fig.tight_layout()
        out = RESULTS_DIR / "exp1_rtt_vs_threshold.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"Saved: {out}")

    # ── Figure 5: TCP throughput box plots ───────────────────────────────────
    if "tcp_az_to_aws_mbps" in df.columns and "tcp_aws_to_az_mbps" in df.columns:
        fwd = df["tcp_az_to_aws_mbps"].dropna()
        rev = df["tcp_aws_to_az_mbps"].dropna()
        fig, ax = plt.subplots(figsize=(6, 5))
        bp = ax.boxplot([fwd, rev],
                        tick_labels=["TCP Azure→AWS", "TCP AWS→Azure"],
                        patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2})
        bp["boxes"][0].set_facecolor("#BDD7EE")
        bp["boxes"][1].set_facecolor("#FFE0B0")
        ax.set_ylabel("Throughput (Mbps)")
        ax.set_title(f"Figure 5: TCP Bidirectional Throughput  (n={len(fwd)})")
        ax.yaxis.grid(True, linestyle="--", alpha=0.2)
        fig.tight_layout()
        out = RESULTS_DIR / "exp1_throughput.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"Saved: {out}")


# ── Experiment 2 ───────────────────────────────────────────────────────────────

def analyse_exp2(paths) -> None:
    print(f"\n{'='*60}")
    print(f"  EXPERIMENT 2 — Fault Injection Comparison")
    print(f"{'='*60}")

    df = load_exp2(paths)
    n  = len(df)

    # ── Blast radius distribution ──────────────────────────────────────────────
    print(f"\n── Blast Radius Distribution  (n={n}) ──")
    print("  Score: 2=both clouds affected, 1=one cloud, 0=neither")
    for cond, col in [("Framework", "fw_blast_radius_score"),
                      ("Monolith",  "mono_blast_radius_score")]:
        if col not in df.columns:
            continue
        print(f"\n  {cond}:")
        for s in [0, 1, 2]:
            cnt = (df[col] == s).sum()
            lbl = {0: "neither", 1: "one cloud", 2: "both clouds"}[s]
            print(f"    {s}/2 ({lbl}): {cnt}/{n}  ({cnt/n*100:.0f}%)")

    # ── Wilcoxon signed-rank + Cliff's Delta (blast radius) ───────────────────
    if "fw_blast_radius_score" in df.columns and "mono_blast_radius_score" in df.columns:
        pairs = df[["fw_blast_radius_score", "mono_blast_radius_score"]].dropna()
        fw_b  = pairs["fw_blast_radius_score"]
        mo_b  = pairs["mono_blast_radius_score"]

        print(f"\n── Wilcoxon Signed-Rank Test — Blast Radius ──")
        print(f"  H₀: distribution of (mono − fw) blast scores symmetric around 0")
        print(f"  H₁: mono blast radius > fw blast radius  (one-sided)")
        diffs = mo_b - fw_b
        if (diffs != 0).sum() < 1:
            print("  All differences are zero — test not applicable.")
        else:
            try:
                w_stat, p_val = stats.wilcoxon(fw_b, mo_b, alternative="less")
                sig = "SIGNIFICANT" if p_val < ALPHA else "not significant"
                print(f"  W = {w_stat:.4f},  p = {p_val:.4f}  → {sig} at α={ALPHA}")
            except Exception as e:
                print(f"  Wilcoxon: {e}")

        d = cliffs_delta(fw_b, mo_b)
        direction = "FW blast < Mono blast" if d < 0 else "FW blast ≥ Mono blast"
        print(f"  Cliff's Delta (FW vs Mono): δ = {d:.4f}  [{delta_label(d)}]  ({direction})")

    # ── Recovery time descriptive stats ───────────────────────────────────────
    rec_rows = []
    for lbl, col in [("Framework recovery (s)", "fw_recovery_time_s"),
                     ("Monolith recovery (s)",   "mono_recovery_time_s")]:
        if col in df.columns:
            rec_rows.append(describe(df[col], lbl))
    if rec_rows:
        print(f"\n── Recovery Time ──")
        print_table(rec_rows)

    # ── Mann-Whitney U + Cliff's Delta (recovery time) ────────────────────────
    if "fw_recovery_time_s" in df.columns and "mono_recovery_time_s" in df.columns:
        fw_r = df["fw_recovery_time_s"].dropna()
        mo_r = df["mono_recovery_time_s"].dropna()
        if len(fw_r) > 0 and len(mo_r) > 0:
            print(f"\n── Mann-Whitney U Test — Recovery Time ──")
            print(f"  H₀: FW and Mono recovery times drawn from same distribution")
            print(f"  H₁: FW recovery time < Mono recovery time  (one-sided)")
            try:
                u_stat, p_val = stats.mannwhitneyu(fw_r, mo_r, alternative="less")
                sig = "SIGNIFICANT" if p_val < ALPHA else "not significant"
                print(f"  U = {u_stat:.1f},  p = {p_val:.6f}  → {sig} at α={ALPHA}")
            except Exception as e:
                print(f"  Mann-Whitney: {e}")

            d = cliffs_delta(fw_r, mo_r)
            direction = "FW faster" if d < 0 else "Mono faster"
            print(f"  Cliff's Delta (FW vs Mono): δ = {d:.4f}  [{delta_label(d)}]  ({direction})")

            if fw_r.median() > 0:
                ratio = mo_r.median() / fw_r.median()
                print(f"  Median ratio (Mono / FW): {ratio:.0f}× slower")

    # ── Per-category breakdown ─────────────────────────────────────────────────
    if "fault_category" in df.columns:
        print(f"\n── By Fault Category ──")
        for cat in sorted(df["fault_category"].unique()):
            sub    = df[df["fault_category"] == cat]
            fw_b   = sub["fw_blast_radius_score"].mean()   if "fw_blast_radius_score"   in sub.columns else float("nan")
            mo_b   = sub["mono_blast_radius_score"].mean() if "mono_blast_radius_score" in sub.columns else float("nan")
            fw_rec = sub["fw_recovery_time_s"].mean()      if "fw_recovery_time_s"      in sub.columns else float("nan")
            mo_rec = sub["mono_recovery_time_s"].mean()    if "mono_recovery_time_s"    in sub.columns else float("nan")
            print(f"  {cat:15s}  n={len(sub):2d}  "
                  f"FW_blast={fw_b:.2f}/2  Mono_blast={mo_b:.2f}/2  "
                  f"FW_rec={fw_rec:.2f}s  Mono_rec={mo_rec:.0f}s")

    # ── Figure 6: blast radius grouped bar ────────────────────────────────────
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
        ax.set_title(f"Figure 6: Blast Radius — Framework vs Monolith  (n={n})")
        ax.legend()
        ax.yaxis.grid(True, linestyle="--", alpha=0.2)
        fig.tight_layout()
        out = RESULTS_DIR / "exp2_blast_radius.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"\nSaved: {out}")

    # ── Figure 7: recovery time boxplot (log scale) ───────────────────────────
    if "fw_recovery_time_s" in df.columns and "mono_recovery_time_s" in df.columns:
        fw_r = df["fw_recovery_time_s"].dropna()   / 60
        mo_r = df["mono_recovery_time_s"].dropna() / 60
        fig, ax = plt.subplots(figsize=(6, 5))
        bp = ax.boxplot([fw_r, mo_r],
                        tick_labels=["PL-IaC\nFramework", "Monolithic\nBaseline"],
                        patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2})
        bp["boxes"][0].set_facecolor("#BDD7EE")
        bp["boxes"][1].set_facecolor("#FFE0B0")
        ax.set_yscale("log")
        ax.set_ylabel("Recovery Time (minutes, log scale)")
        ax.set_title(f"Figure 7: Recovery Time After Fault Correction  (n={n})")
        ax.yaxis.grid(True, linestyle="--", alpha=0.2)
        fig.tight_layout()
        out = RESULTS_DIR / "exp2_recovery_time.png"
        fig.savefig(str(out), bbox_inches="tight", facecolor="white")
        plt.close(fig)
        print(f"Saved: {out}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp1", type=Path, nargs="+",
                        help="Exp1 CSV file(s). Omit to auto-discover all.")
    parser.add_argument("--exp2", type=Path, nargs="+",
                        help="Exp2 CSV file(s). Omit to auto-discover all.")
    args = parser.parse_args()

    if not args.exp1 and not args.exp2:
        e1 = sorted((BASE / "data").glob("exp1_steady_state_*.csv"))
        e2 = sorted((BASE / "data").glob("exp2_fault_injection_*.csv"))
        if e1:
            args.exp1 = e1
            print(f"Auto exp1: {len(e1)} file(s)")
        if e2:
            args.exp2 = e2
            print(f"Auto exp2: {len(e2)} file(s)")

    if not args.exp1 and not args.exp2:
        sys.exit("No CSVs found. Run benchmark.py and/or inject.py first.")

    if args.exp1:
        analyse_exp1(args.exp1)
    if args.exp2:
        analyse_exp2(args.exp2)

    print(f"\nAll figures saved to: {RESULTS_DIR}")


if __name__ == "__main__":
    main()
