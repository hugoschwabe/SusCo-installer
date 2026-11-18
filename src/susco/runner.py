"""Paired thinking/non-thinking GSM8K measurement runner."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import random
import re
import socket
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tqdm import tqdm

from .config import Config, HostConfig, load_config
from .data import load_prepared, random_sample, stratified_sample
from .energy.kepler import EnergyDelta, EnergySample, KeplerError, KeplerMeter
from .grade import grade
from .ollama_client import ChatResult, OllamaClient, OllamaError
from .preflight import print_checks, raise_for_failures, run_preflight

MODE_NO_THINK = "no_think"
MODE_THINK = "think"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def profile_size(config: Config, profile: str) -> tuple[int, int]:
    if profile == "test":
        return config.test_questions, config.test_repetitions
    if profile == "pilot":
        return config.pilot_questions, config.pilot_repetitions
    if profile == "full":
        return 0, config.full_repetitions  # 0 means all rows
    raise ValueError(f"unknown profile: {profile}")


def unique_run_dir(base: Path, run_id: str) -> Path:
    candidate = base / run_id
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = base / f"{run_id}-{index}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free run directory under {base}")


def latest_run_dir(base: Path) -> Path | None:
    candidates = sorted(
        (path for path in base.glob("run-*") if (path / "requests.jsonl").exists()),
        key=lambda path: path.stat().st_mtime,
    )
    return candidates[-1] if candidates else None


def completed_keys(path: Path) -> set[tuple[int, str, str]]:
    keys: set[tuple[int, str, str]] = set()
    if not path.exists():
        return keys
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == "error":
                continue
            keys.add((int(row.get("rep", 0)), str(row.get("question_id")), str(row.get("mode"))))
    return keys


@dataclass
class Measurement:
    energy: EnergyDelta
    idle_power_w: float | None
    idle_energy_j: float
    energy_j: float
    counter_start_j: float
    counter_end_j: float
    low_confidence: bool
    note: str


class RequestMeasurer:
    def __init__(self, meter: KeplerMeter, config: Config) -> None:
        self.meter = meter
        self.config = config

    def scrape(self) -> EnergySample:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self.meter.scrape()
            except KeplerError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
        raise KeplerError(str(last_error) if last_error else "Kepler scrape failed")

    def measure_idle(self) -> tuple[float | None, EnergyDelta]:
        before = self.scrape()
        time.sleep(max(0.0, self.config.idle_window_s))
        after = self.scrape()
        delta = self.meter.delta(before, after)
        return (delta.watts if delta.valid else None), delta

    def measure_request(self, call) -> tuple[ChatResult, Measurement]:
        idle_power, idle_delta = self.measure_idle()
        start = self.scrape()
        result = call()
        end = self.scrape()
        delta = self.meter.delta(start, end)
        idle_energy = (idle_power or 0.0) * delta.duration_s
        energy_j = max(0.0, delta.joules - idle_energy) if delta.valid else 0.0

        interval_s = self.config.kepler_monitor_interval_ms / 1000.0
        reasons: list[str] = []
        if not delta.valid:
            reasons.append(delta.reason or "invalid counter window")
        if idle_power is None:
            reasons.append("idle baseline unavailable")
        if delta.duration_s < self.config.low_confidence_window_factor * interval_s:
            reasons.append(
                f"window {delta.duration_s:.3f}s < "
                f"{self.config.low_confidence_window_factor:g}x monitor interval"
            )
        if delta.valid and delta.joules < idle_energy:
            reasons.append("idle estimate exceeds measured energy")
        low_confidence = bool(reasons)

        return result, Measurement(
            energy=delta,
            idle_power_w=idle_power,
            idle_energy_j=idle_energy,
            energy_j=energy_j,
            counter_start_j=start.joules,
            counter_end_j=end.joules,
            low_confidence=low_confidence,
            note="; ".join(reasons) + (f" (idle window {idle_delta.duration_s:.3f}s)" if idle_delta else ""),
        )


def record_from_result(
    *,
    run_id: str,
    profile: str,
    rep: int,
    row: dict[str, Any],
    mode: str,
    think: bool,
    result: ChatResult,
    measurement: Measurement,
    config: Config,
    model_digest: str,
    started_at: str,
) -> dict[str, Any]:
    graded = grade(result.content, row.get("gold"), result.done_reason)
    return {
        "run_id": run_id,
        "profile": profile,
        "rep": rep,
        "mode": mode,
        "think": think,
        "question_id": str(row.get("id")),
        "question": row.get("question"),
        "gold": row.get("gold"),
        "difficulty": int(row.get("difficulty", 1)),
        "difficulty_raw": row.get("difficulty_raw"),
        "reasoning_steps": row.get("reasoning_steps"),
        "prediction": graded.prediction,
        "normalized_prediction": graded.normalized_prediction,
        "normalized_gold": graded.normalized_gold,
        "correct": graded.correct,
        "strict_correct": graded.strict_correct,
        "status": graded.status,
        "content": result.content,
        "thinking": result.thinking,
        "content_chars": len(result.content),
        "thinking_chars": len(result.thinking),
        "eval_count": result.eval_count,
        "prompt_eval_count": result.prompt_eval_count,
        "total_duration_ns": result.total_duration_ns,
        "load_duration_ns": result.load_duration_ns,
        "prompt_eval_duration_ns": result.prompt_eval_duration_ns,
        "eval_duration_ns": result.eval_duration_ns,
        "wall_duration_s": result.wall_duration_s,
        "time_to_first_token_s": result.time_to_first_token_s,
        "tokens_per_second": result.tokens_per_second,
        "done_reason": result.done_reason,
        "fallback_prompt_suffix": result.fallback_prompt_suffix,
        "energy_j": measurement.energy_j,
        "energy_raw_j": measurement.energy.joules,
        "energy_valid": measurement.energy.valid,
        "idle_power_w": measurement.idle_power_w,
        "idle_energy_j": measurement.idle_energy_j,
        "request_window_s": measurement.energy.duration_s,
        "counter_start_j": measurement.counter_start_j,
        "counter_end_j": measurement.counter_end_j,
        "energy_zones": list(measurement.energy.zones),
        "low_confidence": measurement.low_confidence,
        "energy_note": measurement.note,
        "model": config.model,
        "model_digest": model_digest,
        "kepler_zone": config.kepler_zone,
        "monitor_interval_ms": config.kepler_monitor_interval_ms,
        "started_at": started_at,
        "finished_at": now_iso(),
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def environment_payload() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "loadavg": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "utc": now_iso(),
    }


def simple_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-mode and per-repetition aggregates written at the end of a run."""

    errors = sum(1 for row in records if row.get("status") == "error")
    records = [row for row in records if row.get("status") != "error"]

    def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rows)
        correct = sum(1 for row in rows if row.get("correct"))
        energy = sum(float(row.get("energy_j") or 0.0) for row in rows)
        tokens = sum(int(row.get("eval_count") or 0) for row in rows)
        latency = sum(float(row.get("wall_duration_s") or 0.0) for row in rows)
        low = sum(1 for row in rows if row.get("low_confidence"))
        return {
            "n": n,
            "correct": correct,
            "accuracy": correct / n if n else None,
            "energy_j": energy,
            "tokens": tokens,
            "latency_s": latency,
            "energy_per_correct_j": energy / correct if correct else None,
            "tokens_per_correct": tokens / correct if correct else None,
            "latency_per_correct_s": latency / correct if correct else None,
            "low_confidence_windows": low,
        }

    by_mode = {
        mode: aggregate([row for row in records if row.get("mode") == mode])
        for mode in (MODE_NO_THINK, MODE_THINK)
    }
    by_rep: dict[str, Any] = {}
    for rep in sorted({int(row.get("rep", 0)) for row in records}):
        by_rep[str(rep)] = {
            mode: aggregate(
                [row for row in records if int(row.get("rep", 0)) == rep and row.get("mode") == mode]
            )
            for mode in (MODE_NO_THINK, MODE_THINK)
        }

    no_think = by_mode[MODE_NO_THINK]
    think = by_mode[MODE_THINK]
    extra_energy = (think["energy_j"] or 0.0) - (no_think["energy_j"] or 0.0)
    extra_correct = (think["correct"] or 0) - (no_think["correct"] or 0)
    return {
        "records": len(records),
        "errors": errors,
        "by_mode": by_mode,
        "by_rep": by_rep,
        "total": aggregate(records),
        "thinking_minus_no_thinking": {
            "extra_energy_j": extra_energy,
            "extra_tokens": (think["tokens"] or 0) - (no_think["tokens"] or 0),
            "extra_latency_s": (think["latency_s"] or 0.0) - (no_think["latency_s"] or 0.0),
            "extra_correct": extra_correct,
            "extra_energy_per_additional_correct_j": (
                extra_energy / extra_correct if extra_correct > 0 else None
            ),
            "energy_per_correct_ratio": (
                think["energy_per_correct_j"] / no_think["energy_per_correct_j"]
                if think["energy_per_correct_j"] and no_think["energy_per_correct_j"]
                else None
            ),
        },
    }


def _number(value: Any, spec: str = ".2f", dash: str = "-") -> str:
    if value is None:
        return dash
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return dash


def _summary_lines(summary: dict[str, Any]) -> list[str]:
    lines = [
        f"{'mode':<10} {'n':>5} {'correct':>8} {'acc':>7} "
        f"{'energy_J':>14} {'tokens':>9} {'latency_s':>11} {'J/correct':>14}"
    ]
    for mode in (MODE_NO_THINK, MODE_THINK):
        a = summary["by_mode"][mode]
        lines.append(
            f"{mode:<10} {a['n']:>5} {a['correct']:>8} {_number(a['accuracy'], '.3f'):>7} "
            f"{_number(a['energy_j'], '.2f'):>14} {a['tokens']:>9} "
            f"{_number(a['latency_s'], '.1f'):>11} {_number(a['energy_per_correct_j'], '.2f'):>14}"
        )
    total = summary["total"]
    lines.append("-" * 82)
    lines.append(
        f"{'total':<10} {total['n']:>5} {total['correct']:>8} {_number(total['accuracy'], '.3f'):>7} "
        f"{_number(total['energy_j'], '.2f'):>14} {total['tokens']:>9} "
        f"{_number(total['latency_s'], '.1f'):>11} {_number(total['energy_per_correct_j'], '.2f'):>14}"
    )
    return lines


def _natural_id(value: Any) -> tuple[Any, ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", str(value))
    )


def _question_lines(records: list[dict[str, Any]]) -> list[str]:
    lines = [
        f"{'question_id':<13} {'d':>2} {'mode':<9} {'ok':<3} "
        f"{'energy_J':>10} {'tok':>5} {'lat_s':>7}  {'gold':<14} {'prediction':<14} question"
    ]
    ordered = sorted(
        records,
        key=lambda row: (int(row.get("rep", 0)), _natural_id(row.get("question_id")), row.get("mode", "")),
    )
    for row in ordered:
        if row.get("status") == "error":
            lines.append(
                f"{row.get('question_id', ''):<13} {row.get('difficulty', ''):>2} "
                f"{row.get('mode', ''):<9} {'ERR':<3} {row.get('error', '')}"
            )
            continue
        question = " ".join(str(row.get("question") or "").split())
        if len(question) > 72:
            question = question[:69] + "..."
        lines.append(
            f"{str(row.get('question_id', '')):<13} {int(row.get('difficulty', 1)):>2} "
            f"{row.get('mode', ''):<9} {'Y' if row.get('correct') else 'N':<3} "
            f"{_number(row.get('energy_j'), '.2f'):>10} {int(row.get('eval_count') or 0):>5} "
            f"{_number(row.get('wall_duration_s'), '.1f'):>7}  "
            f"{str(row.get('gold'))[:14]:<14} {str(row.get('prediction'))[:14]:<14} {question}"
        )
    return lines


def print_report(
    records: list[dict[str, Any]],
    run_dir: Path,
    profile: str,
    max_print_rows: int = 200,
) -> None:
    summary = simple_summary(records)
    header = [
        f"Results: {run_dir}",
        f"Raw log: {run_dir / 'requests.jsonl'}",
        f"Summary: {run_dir / 'summary.json'}",
        f"Records: {summary['records']}; errors: {summary['errors']}",
    ]
    aggregate = _summary_lines(summary)
    question_lines = _question_lines(records)
    report_lines = header + [""] + aggregate + [
        "",
        "thinking - no_think:",
        json.dumps(summary["thinking_minus_no_thinking"], indent=2),
        "",
        "Per-question:",
    ] + question_lines
    report_path = run_dir / "report.txt"
    try:
        report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not write {report_path}: {exc}", file=sys.stderr)

    row_count = max(0, len(question_lines) - 1)
    if profile in ("test", "pilot") or row_count <= max_print_rows:
        display = question_lines
    else:
        display = question_lines[: max_print_rows + 1]
        display.append(
            f"... {row_count - max_print_rows} more rows; full table: {report_path}"
        )

    print("\n" + "\n".join(header + [""] + aggregate))
    print()
    print("thinking - no_think:")
    print(json.dumps(summary["thinking_minus_no_thinking"], indent=2))
    print()
    print("Per-question:")
    print("\n".join(display))
    print(f"\nFull report: {report_path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run paired Qwen3 GSM8K measurements")
    parser.add_argument("--config", default="configs/experiment.yaml")
    parser.add_argument("--host-config", default="configs/host.local.yaml")
    parser.add_argument("--profile", choices=("test", "pilot", "full"), default="test")
    parser.add_argument("--limit", type=int, default=None, help="override number of questions")
    parser.add_argument(
        "--sample-mode",
        choices=("random", "stratified"),
        default="stratified",
        help="question sampling method when --limit is used",
    )
    parser.add_argument("--sample-seed", type=int, default=None, help="seed for question selection")
    parser.add_argument("--reps", type=int, default=None, help="override repetitions")
    parser.add_argument("--out", default=None, help="run directory (default results/run-<timestamp>-<profile>)")
    parser.add_argument("--resume", action="store_true", help="resume the newest run or --out")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--kepler-zone", default=None)
    parser.add_argument("--seed", type=int, default=None, help="seed for run order; also sampling unless --sample-seed is set")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--max-print-rows", type=int, default=200, help="max per-question rows printed for full runs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config, host = load_config(args.config, args.host_config)
    overrides: dict[str, Any] = {}
    if args.model:
        overrides["model"] = args.model
    if args.kepler_zone:
        overrides["kepler_zone"] = args.kepler_zone
    if args.seed is not None:
        overrides["seed"] = args.seed
    config = config.with_overrides(**overrides)

    n_questions, reps = profile_size(config, args.profile)
    if args.limit is not None:
        n_questions = args.limit
    if args.reps is not None:
        reps = args.reps
    if reps < 1:
        raise SystemExit("--reps must be >= 1")

    if not args.skip_preflight:
        checks = run_preflight(config, host)
        print_checks(checks)
        raise_for_failures(checks)

    sample_seed = (
        args.sample_seed
        if args.sample_seed is not None
        else (args.seed if args.seed is not None else config.sample_seed)
    )
    rows = load_prepared(config.dataset_path)
    if n_questions <= 0 or n_questions >= len(rows):
        questions = list(rows)
        random.Random(sample_seed).shuffle(questions)
    elif args.sample_mode == "random":
        questions = random_sample(rows, n_questions, seed=sample_seed)
    else:
        questions = stratified_sample(rows, n_questions, seed=sample_seed)

    base = config.results_path
    base.mkdir(parents=True, exist_ok=True)
    if args.resume:
        if args.out:
            run_dir = Path(args.out).resolve()
        else:
            run_dir = latest_run_dir(base)
            if run_dir is None:
                raise SystemExit("--resume requested but no previous run was found")
    elif args.out:
        run_dir = Path(args.out).resolve()
    else:
        run_id = "run-" + datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{args.profile}"
        run_dir = unique_run_dir(base, run_id)

    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_dir.name
    requests_path = run_dir / "requests.jsonl"
    completed = completed_keys(requests_path) if args.resume else set()

    lock_path = base / ".lock"
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another SusCo run is active (lock: {lock_path})")

    write_json(
        run_dir / "config.json",
        {
            "experiment": asdict(config),
            "host": asdict(host),
            "run": {
                "profile": args.profile,
                "questions": len(questions),
                "repetitions": reps,
                "sample_mode": args.sample_mode,
                "sample_seed": sample_seed,
                "order_seed": config.seed,
            },
        },
    )
    write_json(run_dir / "environment.json", environment_payload())

    client = OllamaClient(config.ollama_url, config.model, config.request_timeout_s)
    try:
        info = client.check_model(config.model_digest)
    except OllamaError as exc:
        raise SystemExit(str(exc))
    model_digest = str(info.get("digest", "")) or config.model_digest
    meter = KeplerMeter(config.kepler_url, config.kepler_zone, config.kepler_timeout_s)
    measurer = RequestMeasurer(meter, config)

    if not args.skip_preflight:
        print(f"Kepler zone: {config.kepler_zone}; monitor interval: {config.kepler_monitor_interval_ms} ms")
        print(
            f"Questions: {len(questions)}; repetitions: {reps}; modes: {MODE_NO_THINK}/{MODE_THINK}; "
            f"sampling: {args.sample_mode} (seed {sample_seed})"
        )

    if not completed:
        warmup_question = questions[0]["question"]
        for think in (False, True):
            for _ in range(max(0, config.warmup_prompts)):
                try:
                    client.chat(warmup_question, think, config)
                except OllamaError as exc:
                    raise SystemExit(f"warmup failed: {exc}")
        time.sleep(2.0)

    total = len(questions) * reps * 2
    progress = tqdm(
        total=total,
        initial=min(len(completed), total),
        disable=args.no_progress,
        desc=run_dir.name,
        unit="req",
    )
    records: list[dict[str, Any]] = []
    if requests_path.exists():
        with requests_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    records.append(json.loads(line))

    consecutive_errors = 0
    started = time.monotonic()
    try:
        with requests_path.open("a", encoding="utf-8") as handle:
            for rep in range(reps):
                rep_questions = list(questions)
                random.Random(config.seed + rep).shuffle(rep_questions)
                for index, row in enumerate(rep_questions):
                    modes = [(MODE_NO_THINK, False), (MODE_THINK, True)]
                    random.Random(config.seed + rep * 100003 + index).shuffle(modes)
                    for mode, think in modes:
                        key = (rep, str(row.get("id")), mode)
                        if key in completed:
                            continue
                        started_at = now_iso()
                        try:
                            result, measurement = measurer.measure_request(
                                lambda: client.chat(row["question"], think, config)
                            )
                        except (OllamaError, KeplerError) as exc:
                            consecutive_errors += 1
                            record = {
                                "run_id": run_id,
                                "profile": args.profile,
                                "rep": rep,
                                "mode": mode,
                                "think": think,
                                "question_id": str(row.get("id")),
                                "question": row.get("question"),
                                "gold": row.get("gold"),
                                "difficulty": int(row.get("difficulty", 1)),
                                "status": "error",
                                "correct": False,
                                "energy_j": None,
                                "error": str(exc),
                                "started_at": started_at,
                                "finished_at": now_iso(),
                            }
                            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                            handle.flush()
                            progress.update(1)
                            if consecutive_errors >= 3:
                                raise SystemExit(f"aborting after 3 consecutive errors: {exc}")
                            continue
                        consecutive_errors = 0
                        record = record_from_result(
                            run_id=run_id,
                            profile=args.profile,
                            rep=rep,
                            row=row,
                            mode=mode,
                            think=think,
                            result=result,
                            measurement=measurement,
                            config=config,
                            model_digest=model_digest,
                            started_at=started_at,
                        )
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                        handle.flush()
                        records.append(record)
                        progress.update(1)
                        progress.set_postfix(
                            mode=mode,
                            correct=int(record["correct"]),
                            energy=f"{record['energy_j']:.2f}J",
                            low=int(bool(record["low_confidence"])),
                        )
                        time.sleep(max(0.0, config.inter_request_s))
                if rep + 1 < reps:
                    tqdm.write(f"repetition {rep + 1}/{reps} done; cooling down {config.inter_repetition_s:g}s")
                    time.sleep(max(0.0, config.inter_repetition_s))
    except KeyboardInterrupt:
        tqdm.write("interrupted; partial results are kept")
    finally:
        progress.close()
        summary = simple_summary(records)
        summary["elapsed_s"] = time.monotonic() - started
        summary["completed_requests"] = len(records)
        summary["expected_requests"] = total
        write_json(run_dir / "summary.json", summary)
        latest = base / "latest"
        try:
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(run_dir.name)
        except OSError:
            pass
        lock_handle.close()

    print_report(records, run_dir, args.profile, max_print_rows=args.max_print_rows)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
