"""Human-readable report and plots for a SusCo run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from ..config import resolve_path
from .metrics import load_records, save_summaries, summarize
from .worth import worth_matrix_lines


def _plot_mode_summary(mode: pd.DataFrame, outdir: Path) -> None:
    if mode.empty:
        return
    metrics = [
        ("energy_per_correct_j", "Energy per correct answer (J)"),
        ("eval_tokens_mean", "Generated tokens per request"),
        ("latency_s_mean", "Latency per request (s)"),
    ]
    fig, axes = plt.subplots(1, len(metrics), figsize=(13, 4))
    for axis, (column, title) in zip(axes, metrics):
        sns.barplot(data=mode, x="mode", y=column, hue="mode", ax=axis, legend=False)
        axis.set_title(title)
        axis.set_xlabel("")
        axis.tick_params(axis="x", rotation=0)
    fig.tight_layout()
    fig.savefig(outdir / "mode_summary.png", dpi=200)
    plt.close(fig)


def _plot_paired(paired: pd.DataFrame, outdir: Path) -> None:
    if paired.empty:
        return
    data = paired[paired["metric"].isin(["energy_j", "eval_count", "wall_duration_s"])].copy()
    if data.empty:
        return
    data["label"] = data["metric"].map(
        {
            "energy_j": "Energy (J)",
            "eval_count": "Tokens",
            "wall_duration_s": "Latency (s)",
        }
    )
    fig, axis = plt.subplots(figsize=(7, 4))
    axis.bar(data["label"], data["mean_delta_think_minus_no_think"], color="#d95f02")
    axis.axhline(0, color="black", linewidth=0.8)
    axis.set_ylabel("Thinking minus non-thinking")
    axis.set_title("Paired per-question deltas")
    fig.tight_layout()
    fig.savefig(outdir / "paired_deltas.png", dpi=200)
    plt.close(fig)


def _plot_marginal(marginal: pd.DataFrame, outdir: Path) -> None:
    if marginal.empty:
        return
    data = marginal[marginal["scope"] == "repetition"].dropna(
        subset=["extra_energy_per_additional_correct_j"]
    )
    if data.empty:
        return
    fig, axis = plt.subplots(figsize=(7, 4))
    axis.bar(data["rep"].astype(str), data["extra_energy_per_additional_correct_j"], color="#7570b3")
    axis.set_xlabel("Repetition")
    axis.set_ylabel("Extra energy per additional correct answer (J)")
    axis.set_title("Marginal energy cost of thinking")
    fig.tight_layout()
    fig.savefig(outdir / "marginal_energy.png", dpi=200)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze a SusCo run")
    parser.add_argument("--results", default="results/latest")
    parser.add_argument("--out", default=None)
    parser.add_argument("--boot", type=int, default=2000)
    args = parser.parse_args(argv)

    source = resolve_path(args.results)
    if args.out:
        outdir = resolve_path(args.out)
    elif source.is_dir():
        outdir = source / "analysis"
    else:
        outdir = source.parent / "analysis"
    outdir.mkdir(parents=True, exist_ok=True)

    frame = load_records(source)
    summaries = summarize(frame, n_boot=args.boot)
    save_summaries(summaries, outdir)

    sns.set_theme(style="whitegrid")
    _plot_mode_summary(summaries["mode"], outdir)
    _plot_paired(summaries["paired"], outdir)
    _plot_marginal(summaries["marginal"], outdir)

    errors = int((frame["status"] == "error").sum())
    print(f"Records: {len(frame)} (errors: {errors})")
    matrix = summaries.get("worth_matrix")
    if matrix is not None:
        print()
        print("\n".join(worth_matrix_lines(matrix)))
        print(f"Full thinking-worth matrix: {outdir / 'worth_matrix.csv'}")
    for name, table in summaries.items():
        if name == "worth_matrix":
            continue
        print(f"\n== {name} ==")
        print(table.to_string(index=False))
    print(f"\nWrote analysis to {outdir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
