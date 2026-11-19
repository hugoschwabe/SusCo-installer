"""MATH-500 loading and reproducible question sampling."""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .config import resolve_path
from .grade import clean_answer, extract_gold

DEFAULT_OUTPUT = "data/processed/math500_test.jsonl"
DATASET_NAME = "HuggingFaceH4/MATH-500"


def load_math500(split: str = "test") -> list[dict[str, Any]]:
    """Load MATH-500 from HuggingFace.

    The import is deliberately lazy so unit tests and analysis do not need the
    ``datasets`` package or network access.
    """

    try:
        from datasets import load_dataset
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "The 'datasets' package is required to download MATH-500. "
            "Run 'make install' or 'pip install datasets'."
        ) from exc

    dataset = load_dataset(DATASET_NAME, split=split)
    return [dict(row) for row in dataset]


def prepare_rows(raw_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(raw_rows):
        answer = str(row.get("answer", "") or "").strip()
        solution = str(row.get("solution", "") or "")
        gold = clean_answer(answer) or extract_gold(solution)
        level = row.get("level")
        rows.append(
            {
                "id": str(row.get("unique_id") or f"test-{index}"),
                "question": str(row.get("problem", "") or "").strip(),
                "answer": answer,
                "solution": solution,
                "gold": gold,
                "subject": str(row.get("subject", "") or "").strip() or None,
                "level": int(level) if level is not None else None,
            }
        )
    return rows


def prepare_dataset(
    output: str | Path = DEFAULT_OUTPUT,
    split: str = "test",
    force: bool = False,
) -> Path:
    """Download MATH-500 and write JSONL."""

    output_path = resolve_path(output)
    if output_path.exists() and not force:
        print(f"Dataset already prepared: {output_path} (use --force to rebuild)")
        return output_path

    raw = load_math500(split=split)
    rows = prepare_rows(raw)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    subjects = Counter(row["subject"] or "unknown" for row in rows)
    levels = Counter(str(row["level"]) if row["level"] is not None else "unknown" for row in rows)
    meta = {
        "source": DATASET_NAME,
        "split": split,
        "rows": len(rows),
        "subjects": dict(sorted(subjects.items())),
        "levels": dict(sorted(levels.items())),
        "missing_gold": sum(1 for row in rows if not row["gold"]),
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


def dataset_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    subjects = Counter(str(row.get("subject") or "unknown") for row in rows)
    levels = Counter(
        str(row["level"]) if row.get("level") is not None else "unknown" for row in rows
    )
    missing_gold = sum(1 for row in rows if not row.get("gold"))
    duplicate_ids = len(rows) - len({str(row.get("id")) for row in rows})
    return {
        "rows": len(rows),
        "subjects": dict(sorted(subjects.items())),
        "levels": dict(sorted(levels.items())),
        "missing_gold": missing_gold,
        "duplicate_ids": duplicate_ids,
    }


def _cmd_prepare(args: argparse.Namespace) -> int:
    prepare_dataset(
        output=args.output,
        split=args.split,
        force=args.force,
    )
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    rows = load_prepared(args.input)
    stats = dataset_stats(rows)
    if args.json:
        print(json.dumps(stats, indent=2))
        return 0
    print(f"Rows: {stats['rows']}")
    print(f"Missing gold: {stats['missing_gold']}")
    print(f"Duplicate ids: {stats['duplicate_ids']}")
    print("Subjects:")
    for subject, count in stats["subjects"].items():
        print(f"  {subject:<24} {count:>4}")
    print("Levels:")
    for level, count in stats["levels"].items():
        print(f"  {level:<24} {count:>4}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and inspect MATH-500 for SusCo")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="download and prepare MATH-500")
    prepare.add_argument("--output", default=DEFAULT_OUTPUT)
    prepare.add_argument("--split", default="test")
    prepare.add_argument("--force", action="store_true")
    prepare.set_defaults(func=_cmd_prepare)

    stats = sub.add_parser("stats", help="summarize a prepared MATH-500 file")
    stats.add_argument("--input", default=DEFAULT_OUTPUT)
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=_cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
