"""Aggregate statistics for the thinking/non-thinking experiment."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from ..config import resolve_path

NUMERIC_COLUMNS = (
    "energy_j",
    "energy_raw_j",
    "idle_power_w",
    "idle_energy_j",
    "request_window_s",
    "eval_count",
    "prompt_eval_count",
    "wall_duration_s",
    "time_to_first_token_s",
    "tokens_per_second",
    "correct",
    "strict_correct",
    "low_confidence",
    "rep",
)

MODES = ("no_think", "think")


def normalize_record_frame(frame: pd.DataFrame) -> pd.DataFrame:
    for column in NUMERIC_COLUMNS:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "correct" in frame:
        frame["correct"] = frame["correct"].fillna(0).astype(int)
    if "strict_correct" in frame:
        frame["strict_correct"] = frame["strict_correct"].fillna(0).astype(int)
    if "low_confidence" in frame:
        frame["low_confidence"] = frame["low_confidence"].fillna(0).astype(int)
    if "mode" not in frame:
        frame["mode"] = "unknown"
    if "status" not in frame:
        frame["status"] = "unknown"
    frame["mode"] = frame["mode"].astype(str)
    frame["status"] = frame["status"].astype(str)
    return frame


def load_records(path: str | Path = "results/latest") -> pd.DataFrame:
    """Load one run directory or a requests.jsonl file into a DataFrame."""

    source = resolve_path(path)
    if source.is_dir():
        source = source / "requests.jsonl"
    if not source.exists():
        raise FileNotFoundError(f"no measurement records at {source}")

    rows: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    if not rows:
        raise ValueError(f"{source} contains no records")

    return normalize_record_frame(pd.DataFrame(rows))


def valid_records(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame[frame["status"] != "error"].copy()
    return data[data["mode"].isin(MODES)]


def _column_sum(group: pd.DataFrame, column: str) -> float:
    if column not in group:
        return 0.0
    return float(group[column].fillna(0).sum())


def _column_mean(group: pd.DataFrame, column: str) -> float | None:
    if column not in group or len(group) == 0:
        return None
    return float(group[column].mean())


def _aggregate_group(group: pd.DataFrame) -> dict[str, Any]:
    n = len(group)
    correct = int(group["correct"].sum()) if n and "correct" in group else 0
    strict = int(group["strict_correct"].sum()) if n and "strict_correct" in group else 0
    energy = _column_sum(group, "energy_j")
    tokens = int(_column_sum(group, "eval_count"))
    prompt_tokens = int(_column_sum(group, "prompt_eval_count"))
    latency = _column_sum(group, "wall_duration_s")
    status = group["status"].astype(str) if n and "status" in group else pd.Series(dtype=str)
    return {
        "n_requests": n,
        "n_questions": int(group["question_id"].nunique()) if n else 0,
        "correct": correct,
        "accuracy": correct / n if n else None,
        "strict_correct": strict,
        "strict_accuracy": strict / n if n else None,
        "incorrect": int((status == "incorrect").sum()),
        "unparseable": int((status == "unparseable").sum()),
        "truncated": int((status == "truncated").sum()),
        "energy_j_total": energy,
        "energy_j_mean": _column_mean(group, "energy_j"),
        "energy_j_median": float(group["energy_j"].median()) if n and "energy_j" in group else None,
        "eval_tokens_total": tokens,
        "eval_tokens_mean": _column_mean(group, "eval_count"),
        "prompt_tokens_total": prompt_tokens,
        "prompt_tokens_mean": _column_mean(group, "prompt_eval_count"),
        "latency_s_total": latency,
        "latency_s_mean": _column_mean(group, "wall_duration_s"),
        "ttft_s_mean": _column_mean(group, "time_to_first_token_s"),
        "tokens_per_second_mean": _column_mean(group, "tokens_per_second"),
        "energy_per_correct_j": energy / correct if correct else None,
        "tokens_per_correct": tokens / correct if correct else None,
        "latency_per_correct_s": latency / correct if correct else None,
        "low_confidence_rate": float(group["low_confidence"].mean()) if n else None,
    }


def _mode_summary(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for mode in MODES:
        group = data[data["mode"] == mode]
        if len(group):
            rows.append({"mode": mode, **_aggregate_group(group)})
    return pd.DataFrame(rows)


def _group_summary(data: pd.DataFrame, by: str) -> pd.DataFrame:
    rows = []
    for (key, mode), group in data.groupby([by, "mode"], dropna=False):
        rows.append({by: key, "mode": mode, **_aggregate_group(group)})
    return pd.DataFrame(rows).sort_values([by, "mode"]).reset_index(drop=True)


def _paired_question_table(data: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        data.groupby(["question_id", "mode"], as_index=False)
        .agg(
            energy_j=("energy_j", "mean"),
            energy_j_sum=("energy_j", "sum"),
            eval_count=("eval_count", "mean"),
            wall_duration_s=("wall_duration_s", "mean"),
            correct=("correct", "mean"),
            strict_correct=("strict_correct", "mean"),
        )
    )
    return grouped


def _bootstrap_mean(values: np.ndarray, n_boot: int, seed: int) -> tuple[float, float, float]:
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    samples = rng.choice(values, size=(n_boot, len(values)), replace=True)
    means = samples.mean(axis=1)
    return (
        float(values.mean()),
        float(np.percentile(means, 2.5)),
        float(np.percentile(means, 97.5)),
    )


def _bootstrap_ratio(
    numerator: np.ndarray, denominator: np.ndarray, n_boot: int, seed: int
) -> tuple[float, float, float, int]:
    mask = np.isfinite(numerator) & np.isfinite(denominator)
    numerator = numerator[mask]
    denominator = denominator[mask]
    if len(numerator) == 0:
        return (float("nan"), float("nan"), float("nan"), 0)
    rng = np.random.default_rng(seed)
    values: list[float] = []
    indices = np.arange(len(numerator))
    for _ in range(n_boot):
        sample = rng.choice(indices, size=len(indices), replace=True)
        denom = denominator[sample].sum()
        if denom > 0:
            values.append(float(numerator[sample].sum() / denom))
    point_denom = denominator.sum()
    point = float(numerator.sum() / point_denom) if point_denom > 0 else float("nan")
    if not values:
        return (point, float("nan"), float("nan"), 0)
    return (
        point,
        float(np.percentile(values, 2.5)),
        float(np.percentile(values, 97.5)),
        len(values),
    )


def _paired_summary(data: pd.DataFrame, n_boot: int = 2000, seed: int = 42) -> pd.DataFrame:
    pivots: dict[str, pd.DataFrame] = {}
    for metric in ("energy_j", "eval_count", "wall_duration_s", "correct"):
        table = data.groupby(["question_id", "mode"])[metric].mean().unstack("mode")
        if set(MODES).issubset(table.columns):
            pivots[metric] = table[list(MODES)]

    rows: list[dict[str, Any]] = []
    for metric, table in pivots.items():
        diff = (table["think"] - table["no_think"]).dropna()
        if diff.empty:
            continue
        try:
            p_value = float(stats.wilcoxon(diff, zero_method="wilcox").pvalue)
        except ValueError:
            p_value = 1.0
        mean, low, high = _bootstrap_mean(diff.to_numpy(dtype=float), n_boot, seed)
        rows.append(
            {
                "metric": metric,
                "n_questions": int(len(diff)),
                "mean_delta_think_minus_no_think": mean,
                "median_delta": float(diff.median()),
                "ci95_low": low,
                "ci95_high": high,
                "wilcoxon_p": p_value,
            }
        )

    # McNemar on paired per-repetition correctness.
    pairs = data.pivot_table(
        index=["rep", "question_id"], columns="mode", values="correct", aggfunc="first"
    )
    if set(MODES).issubset(pairs.columns):
        pairs = pairs.dropna(subset=list(MODES))
        no = pairs["no_think"].astype(bool)
        think = pairs["think"].astype(bool)
        b = int((~no & think).sum())
        c = int((no & ~think).sum())
        discordant = b + c
        if discordant:
            exact_p = float(stats.binomtest(b, discordant, 0.5).pvalue)
            chi2 = (abs(b - c) - 1) ** 2 / discordant
            chi2_p = float(stats.chi2.sf(chi2, 1))
        else:
            exact_p = 1.0
            chi2_p = 1.0
        rows.append(
            {
                "metric": "mcnemar_correct",
                "n_questions": int(len(pairs)),
                "mean_delta_think_minus_no_think": float(think.mean() - no.mean()),
                "median_delta": float("nan"),
                "ci95_low": float("nan"),
                "ci95_high": float("nan"),
                "wilcoxon_p": exact_p,
                "mcnemar_b_think_only": b,
                "mcnemar_c_no_think_only": c,
                "mcnemar_exact_p": exact_p,
                "mcnemar_chi2_p": chi2_p,
            }
        )
    return pd.DataFrame(rows)


def _marginal_summary(data: pd.DataFrame, n_boot: int = 2000, seed: int = 42) -> pd.DataFrame:
    rep = (
        data.groupby(["rep", "mode"])
        .agg(energy_j=("energy_j", "sum"), correct=("correct", "sum"))
        .reset_index()
    )
    pivot_energy = rep.pivot(index="rep", columns="mode", values="energy_j")
    pivot_correct = rep.pivot(index="rep", columns="mode", values="correct")
    rows: list[dict[str, Any]] = []
    if set(MODES).issubset(pivot_energy.columns):
        for rep_index in pivot_energy.index:
            delta_energy = float(pivot_energy.loc[rep_index, "think"] - pivot_energy.loc[rep_index, "no_think"])
            delta_correct = float(
                pivot_correct.loc[rep_index, "think"] - pivot_correct.loc[rep_index, "no_think"]
            )
            rows.append(
                {
                    "scope": "repetition",
                    "rep": int(rep_index),
                    "delta_energy_j": delta_energy,
                    "delta_correct": delta_correct,
                    "extra_energy_per_additional_correct_j": (
                        delta_energy / delta_correct if delta_correct > 0 else None
                    ),
                }
            )

    question = (
        data.groupby(["question_id", "mode"])
        .agg(energy_j=("energy_j", "sum"), correct=("correct", "sum"))
        .reset_index()
    )
    e = question.pivot(index="question_id", columns="mode", values="energy_j")
    c = question.pivot(index="question_id", columns="mode", values="correct")
    if set(MODES).issubset(e.columns):
        delta_e = (e["think"] - e["no_think"]).to_numpy(dtype=float)
        delta_c = (c["think"] - c["no_think"]).to_numpy(dtype=float)
        point, low, high, n_valid = _bootstrap_ratio(delta_e, delta_c, n_boot, seed)
        rows.append(
            {
                "scope": "question_bootstrap",
                "rep": None,
                "delta_energy_j": float(np.nansum(delta_e)),
                "delta_correct": float(np.nansum(delta_c)),
                "extra_energy_per_additional_correct_j": point,
                "ci95_low": low,
                "ci95_high": high,
                "n_bootstrap_valid": n_valid,
            }
        )
    return pd.DataFrame(rows)


def summarize(frame: pd.DataFrame, n_boot: int = 2000, seed: int = 42) -> dict[str, pd.DataFrame]:
    data = valid_records(frame)
    if data.empty:
        raise ValueError("no successful measurement records to analyze")
    summaries = {
        "mode": _mode_summary(data),
        "repetition": _group_summary(data, "rep"),
        "paired": _paired_summary(data, n_boot=n_boot, seed=seed),
        "marginal": _marginal_summary(data, n_boot=n_boot, seed=seed),
    }
    for column in ("subject", "level"):
        if column in data and data[column].notna().any():
            summaries[column] = _group_summary(data, column)
    from .worth import worth_matrix

    summaries["worth_matrix"] = worth_matrix(data)
    return summaries


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if pd.isna(value):
        return None
    return str(value)


def frame_to_json(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [{key: _json_value(value) for key, value in row.items()} for row in frame.to_dict(orient="records")]


def _frame_to_json(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return frame_to_json(frame)


def save_summaries(
    summaries: dict[str, pd.DataFrame],
    outdir: str | Path = "results/latest/analysis",
) -> Path:
    out = resolve_path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    for name, frame in summaries.items():
        frame.to_csv(out / f"{name}.csv", index=False)
        payload[name] = _frame_to_json(frame)
    (out / "summary.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if "worth_matrix" in payload:
        (out / "worth_matrix.json").write_text(
            json.dumps(payload["worth_matrix"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return out
