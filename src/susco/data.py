"""GSM8K loading, difficulty proxy, and stratified sampling."""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Iterable

from .config import resolve_path
from .grade import extract_gold

DEFAULT_OUTPUT = "data/processed/gsm8k_test.jsonl"


def load_gsm8k(split: str = "test") -> list[dict[str, Any]]:
    """Load GSM8K from HuggingFace.

    The import is deliberately lazy so unit tests and analysis do not need the
    ``datasets`` package or network access.
    """

    try:
        from datasets import load_dataset
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "The 'datasets' package is required to download GSM8K. "
            "Run 'make install' or 'pip install datasets'."
        ) from exc

    dataset = load_dataset("openai/gsm8k", "main", split=split)
    return [dict(row) for row in dataset]


def reasoning_steps(solution: str) -> int:
    """Count non-final solution lines as a cheap reasoning-step proxy."""

    lines = [
        line.strip()
        for line in (solution or "").splitlines()
        if line.strip() and not line.strip().startswith("####")
    ]
    return max(1, len(lines))


def difficulty_raw(solution: str) -> float:
    """A continuous difficulty proxy based on solution length and operations.

    GSM8K has no difficulty labels. This proxy is intentionally simple and
    transparent: the number of reasoning lines plus a small bonus for the
    number of arithmetic operators. It is reported alongside every result and
    should not be interpreted as an externally validated difficulty score.
    """

    lines = reasoning_steps(solution)
    operators = sum((solution or "").count(op) for op in ("+", "-", "*", "/", "="))
    return float(lines) + 0.25 * float(operators)


def assign_difficulty(
    rows: Iterable[dict[str, Any]], bins: int = 5
) -> list[dict[str, Any]]:
    """Assign equal-frequency difficulty bins (1..bins) to rows."""

    if bins < 1:
        raise ValueError("bins must be >= 1")
    items = [dict(row) for row in rows]
    if not items:
        return items
    bins = min(bins, len(items))

    raw_values = [difficulty_raw(str(row.get("answer", ""))) for row in items]
    order = sorted(range(len(items)), key=lambda index: (raw_values[index], str(items[index].get("id", index))))
    n = len(order)
    for rank, index in enumerate(order):
        score = int(rank * bins / n) + 1
        items[index]["difficulty_raw"] = round(raw_values[index], 4)
        items[index]["reasoning_steps"] = reasoning_steps(str(items[index].get("answer", "")))
        items[index]["difficulty"] = min(bins, score)
    return items


def prepare_rows(raw_rows: Iterable[dict[str, Any]], bins: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(raw_rows):
        answer = str(row.get("answer", ""))
        rows.append(
            {
                "id": f"test-{index}",
                "question": str(row.get("question", "")).strip(),
                "answer": answer,
                "gold": extract_gold(answer),
            }
        )
    return assign_difficulty(rows, bins=bins)


def prepare_dataset(
    output: str | Path = DEFAULT_OUTPUT,
    split: str = "test",
    bins: int = 5,
    force: bool = False,
) -> Path:
    """Download GSM8K, add the difficulty proxy, and write JSONL."""

    output_path = resolve_path(output)
    if output_path.exists() and not force:
        print(f"Dataset already prepared: {output_path} (use --force to rebuild)")
        return output_path

    raw = load_gsm8k(split=split)
    rows = prepare_rows(raw, bins=bins)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    meta = {
        "source": "openai/gsm8k",
        "subset": "main",
        "split": split,
        "rows": len(rows),
        "difficulty_bins": bins,
        "difficulty_method": "equal-frequency bins over reasoning-line/operator proxy",
    }
    output_path.with_suffix(".meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(rows)} rows to {output_path}")
    return output_path


def load_prepared(path: str | Path = DEFAULT_OUTPUT) -> list[dict[str, Any]]:
    path = resolve_path(path)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    if not rows:
        raise ValueError(f"{path} contains no rows")
    return rows


def random_sample(rows: list[dict[str, Any]], n: int, seed: int = 42) -> list[dict[str, Any]]:
    """Sample exactly ``n`` rows uniformly at random, reproducibly."""

    if n <= 0:
        return []
    rng = random.Random(seed)
    if n >= len(rows):
        sampled = list(rows)
        rng.shuffle(sampled)
        return sampled
    return rng.sample(rows, n)


def stratified_sample(rows: list[dict[str, Any]], n: int, seed: int = 42) -> list[dict[str, Any]]:
    """Sample exactly ``n`` rows, proportionally across difficulty bins."""

    if n <= 0:
        return []
    if n >= len(rows):
        sampled = list(rows)
        random.Random(seed).shuffle(sampled)
        return sampled

    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(int(row.get("difficulty", 1)), []).append(row)
    keys = sorted(groups)
    rng = random.Random(seed)
    for key in keys:
        rng.shuffle(groups[key])

    total = len(rows)
    allocation = {key: 0 for key in keys}
    if n >= len(keys):
        for key in keys:
            allocation[key] = 1
        remaining = n - len(keys)
    else:
        remaining = n

    while remaining > 0:
        candidates = [key for key in keys if allocation[key] < len(groups[key])]
        if not candidates:
            break
        weights = {
            key: (len(groups[key]) / total) * (remaining + 1) - allocation[key]
            for key in candidates
        }
        key = max(candidates, key=lambda item: (weights[item], -item))
        allocation[key] += 1
        remaining -= 1

    sampled: list[dict[str, Any]] = []
    for key in keys:
        sampled.extend(groups[key][: allocation[key]])
    rng.shuffle(sampled)
    return sampled[:n]


def _cmd_prepare(args: argparse.Namespace) -> int:
    prepare_dataset(
        output=args.output,
        split=args.split,
        bins=args.bins,
        force=args.force,
    )
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    rows = load_prepared(args.input)
    counts: dict[int, int] = {}
    for row in rows:
        key = int(row.get("difficulty", 1))
        counts[key] = counts.get(key, 0) + 1
    print(f"rows={len(rows)}")
    for key in sorted(counts):
        print(f"difficulty {key}: {counts[key]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare GSM8K for SusCo")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="download and prepare GSM8K")
    prepare.add_argument("--output", default=DEFAULT_OUTPUT)
    prepare.add_argument("--split", default="test")
    prepare.add_argument("--bins", type=int, default=5)
    prepare.add_argument("--force", action="store_true")
    prepare.set_defaults(func=_cmd_prepare)

    stats = sub.add_parser("stats", help="show prepared difficulty counts")
    stats.add_argument("--input", default=DEFAULT_OUTPUT)
    stats.set_defaults(func=_cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
