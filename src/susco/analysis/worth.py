"""Difficulty-level thinking-worth matrices for SusCo runs."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .metrics import MODES, _aggregate_group, frame_to_json, normalize_record_frame, valid_records

MATRIX_COLUMNS = (
    "n_requests",
    "n_questions",
    "correct",
    "accuracy",
    "strict_correct",
    "strict_accuracy",
    "incorrect",
    "unparseable",
    "truncated",
    "energy_j_total",
    "energy_j_mean",
    "energy_j_median",
    "eval_tokens_total",
    "eval_tokens_mean",
    "prompt_tokens_total",
    "prompt_tokens_mean",
    "latency_s_total",
    "latency_s_mean",
    "ttft_s_mean",
    "tokens_per_second_mean",
    "energy_per_correct_j",
    "tokens_per_correct",
    "latency_per_correct_s",
    "low_confidence_rate",
)

PRINT_COLUMNS = (
    "level",
    "n_questions",
    "no_think_correct",
    "think_correct",
    "delta_correct",
    "no_think_energy_j",
    "think_energy_j",
    "delta_energy_j",
    "no_think_energy_per_correct_j",
    "think_energy_per_correct_j",
    "energy_per_correct_ratio",
    "extra_energy_per_additional_correct_j",
    "thinking_worth_it",
)


def _clean_level(value: Any) -> Any:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return int(number) if math.isfinite(number) and number.is_integer() else number
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            return value
        return int(number) if math.isfinite(number) and number.is_integer() else number
    return value


def _safe_div(numerator: Any, denominator: Any) -> float | None:
    try:
        if numerator is None or denominator is None:
            return None
        num = float(numerator)
        den = float(denominator)
        if den == 0 or not math.isfinite(num) or not math.isfinite(den):
            return None
        return num / den
    except (TypeError, ValueError):
        return None


def _delta(think_value: Any, no_think_value: Any) -> float | None:
    if think_value is None or no_think_value is None:
        return None
    try:
        return float(think_value) - float(no_think_value)
    except (TypeError, ValueError):
        return None


def _side(prefix: str, aggregate: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{column}": aggregate.get(column) for column in MATRIX_COLUMNS}


def _decision(
    no: dict[str, Any],
    think: dict[str, Any],
    extra_energy: float | None,
    extra_correct: int,
) -> tuple[bool, str]:
    if no.get("n_requests", 0) == 0 or think.get("n_requests", 0) == 0:
        return False, "missing no_think or think measurements"
    if extra_correct < 0:
        return False, "thinking answered fewer questions correctly"
    if extra_correct == 0:
        if extra_energy is not None and extra_energy <= 0:
            return True, "same correct answers with no extra energy"
        return False, "no additional correct answers"
    if extra_energy is None:
        return False, "missing energy measurement"
    if extra_energy <= 0:
        return True, "more correct answers with no extra energy"
    baseline = no.get("energy_per_correct_j")
    if baseline is None:
        return True, "only thinking produced correct answers"
    marginal = _safe_div(extra_energy, extra_correct)
    if marginal is None:
        return False, "missing marginal energy estimate"
    if marginal <= float(baseline):
        return True, f"marginal cost {marginal:.2f} J <= baseline {float(baseline):.2f} J per correct"
    return False, f"marginal cost {marginal:.2f} J > baseline {float(baseline):.2f} J per correct"


def _level_groups(data: pd.DataFrame) -> list[tuple[Any, pd.DataFrame]]:
    if "level" not in data.columns:
        return []
    numeric = pd.to_numeric(data["level"], errors="coerce")
    level_values = numeric if numeric.notna().any() else data["level"]
    grouped = data.assign(_level=level_values).dropna(subset=["_level"]).groupby("_level", dropna=False)
    return [(_clean_level(level), group) for level, group in grouped]


def worth_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    data = valid_records(frame)
    scopes: list[tuple[str, Any, pd.DataFrame]] = [("overall", "all", data)]
    scopes.extend(("level", level, group) for level, group in _level_groups(data))

    rows: list[dict[str, Any]] = []
    for scope, level, group in scopes:
        no = _aggregate_group(group[group["mode"] == MODES[0]])
        think = _aggregate_group(group[group["mode"] == MODES[1]])
        extra_energy = _delta(think.get("energy_j_total"), no.get("energy_j_total"))
        extra_correct = int((think.get("correct") or 0) - (no.get("correct") or 0))
        marginal = _safe_div(extra_energy, extra_correct) if extra_correct > 0 else None
        worth, reason = _decision(no, think, extra_energy, extra_correct)
        rows.append(
            {
                "scope": scope,
                "level": level,
                "n_requests": no.get("n_requests", 0) + think.get("n_requests", 0),
                "n_questions": int(group["question_id"].nunique()) if len(group) else 0,
                **_side("no_think", no),
                **_side("think", think),
                "delta_correct": extra_correct,
                "delta_accuracy": _delta(think.get("accuracy"), no.get("accuracy")),
                "delta_energy_j": extra_energy,
                "delta_tokens": _delta(think.get("eval_tokens_total"), no.get("eval_tokens_total")),
                "delta_latency_s": _delta(think.get("latency_s_total"), no.get("latency_s_total")),
                "energy_per_correct_ratio": _safe_div(
                    think.get("energy_per_correct_j"), no.get("energy_per_correct_j")
                ),
                "extra_energy_j": extra_energy,
                "extra_correct": extra_correct,
                "extra_energy_per_additional_correct_j": marginal,
                "thinking_worth_it": worth,
                "worth_reason": reason,
            }
        )
    return pd.DataFrame(rows)


def write_worth_matrix(records: list[dict[str, Any]], run_dir: str | Path) -> pd.DataFrame:
    out = Path(run_dir)
    out.mkdir(parents=True, exist_ok=True)
    matrix = worth_matrix(normalize_record_frame(pd.DataFrame(records)))
    matrix.to_csv(out / "worth_matrix.csv", index=False)
    (out / "worth_matrix.json").write_text(
        json.dumps(frame_to_json(matrix), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return matrix


def worth_matrix_lines(matrix: pd.DataFrame) -> list[str]:
    if matrix is None or matrix.empty:
        return ["Thinking-worth matrix: no valid paired records."]
    columns = [column for column in PRINT_COLUMNS if column in matrix.columns]
    return ["Thinking-worth matrix", matrix[columns].to_string(index=False)]
