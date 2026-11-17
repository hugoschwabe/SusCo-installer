from susco.runner import MODE_NO_THINK, MODE_THINK, completed_keys, simple_summary


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
    assert summary["by_mode"][MODE_NO_THINK]["correct"] == 2
    assert summary["by_mode"][MODE_THINK]["correct"] == 4
    extra = summary["thinking_minus_no_thinking"]
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
