from susco.runner import (
    MODE_NO_THINK,
    MODE_THINK,
    completed_keys,
    print_report,
    simple_summary,
)


def _record(rep, question, mode, correct, energy, tokens=10, latency=1.0, status="correct"):
    return {
        "rep": rep,
        "question_id": question,
        "mode": mode,
        "correct": correct,
        "energy_j": energy,
        "eval_count": tokens,
        "wall_duration_s": latency,
        "low_confidence": False,
        "status": status,
    }


def test_simple_summary_excludes_errors_and_computes_marginals():
    records = [
        _record(0, "q0", MODE_NO_THINK, True, 10),
        _record(0, "q0", MODE_THINK, True, 15),
        _record(0, "q1", MODE_NO_THINK, False, 10),
        _record(0, "q1", MODE_THINK, True, 15),
        _record(1, "q0", MODE_NO_THINK, True, 10),
        _record(1, "q0", MODE_THINK, True, 15),
        _record(1, "q1", MODE_NO_THINK, False, 10),
        _record(1, "q1", MODE_THINK, True, 15),
        _record(1, "q2", MODE_THINK, False, None, status="error"),
    ]
    summary = simple_summary(records)
    assert summary["records"] == 8
    assert summary["errors"] == 1
    assert summary["total"]["n"] == 8
    assert summary["total"]["correct"] == 6
    assert summary["total"]["energy_j"] == 100
    assert summary["by_mode"][MODE_NO_THINK]["correct"] == 2
    assert summary["by_mode"][MODE_THINK]["correct"] == 4
    extra = summary["thinking_minus_no_thinking"]
    assert extra["no_think_correct"] == 2
    assert extra["think_correct"] == 4
    assert extra["total_correct"] == 6
    assert extra["no_think_n"] == 4
    assert extra["think_n"] == 4
    assert extra["total_n"] == 8
    assert extra["extra_energy_j"] == 20
    assert extra["extra_correct"] == 2
    assert extra["extra_energy_per_additional_correct_j"] == 10


def test_completed_keys_skips_errors(tmp_path):
    path = tmp_path / "requests.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"rep":0,"question_id":"q0","mode":"think","status":"correct"}',
                '{"rep":0,"question_id":"q1","mode":"think","status":"error"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert completed_keys(path) == {(0, "q0", "think")}


def test_print_report_contains_all_metric_sections(tmp_path):
    records = [
        _record(0, "q0", MODE_NO_THINK, True, 10, tokens=12, latency=2.0),
        _record(0, "q0", MODE_THINK, False, 15, tokens=30, latency=3.0),
    ]
    summary = simple_summary(records)
    summary["completed_requests"] = 2
    summary["expected_requests"] = 2
    summary["elapsed_s"] = 5.0

    print_report(records, tmp_path, "test", summary=summary)
    report = (tmp_path / "report.txt").read_text(encoding="utf-8")

    for text in (
        "Aggregate metrics",
        "Per-repetition metrics",
        "Thinking vs non-thinking",
        "Correct answers:",
        "Extra energy:",
        "Extra tokens:",
        "Extra latency:",
        "Extra correct:",
        "Extra energy per additional correct:",
        "Energy per correct ratio",
        "Prompt tokens total/mean:",
        "Thinking-worth matrix",
        "Per-question:",
    ):
        assert text in report
    assert (tmp_path / "worth_matrix.csv").exists()
    assert (tmp_path / "worth_matrix.json").exists()


def test_print_report_write_matrix_false_skips_matrix_files(tmp_path):
    records = [
        _record(0, "q0", MODE_NO_THINK, True, 10),
        _record(0, "q0", MODE_THINK, True, 15),
    ]
    print_report(records, tmp_path, "test", summary=simple_summary(records), write_matrix=False)

    assert not (tmp_path / "worth_matrix.csv").exists()
    assert not (tmp_path / "worth_matrix.json").exists()
