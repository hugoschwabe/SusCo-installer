import json

import pandas as pd
import pytest

from susco.analysis.metrics import load_records, summarize


def synthetic_frame():
    rows = []
    for rep in range(2):
        for question in range(4):
            correct_think = question < 3
            correct_no = question < 2
            for mode, correct in (("no_think", correct_no), ("think", correct_think)):
                base = 5.0 + question + (3.0 if mode == "think" else 0.0)
                rows.append(
                    {
                        "rep": rep,
                        "question_id": f"q{question}",
                        "mode": mode,
                        "correct": int(correct),
                        "strict_correct": int(correct),
                        "status": "correct" if correct else "incorrect",
                        "energy_j": base,
                        "eval_count": 50 + question + (20 if mode == "think" else 0),
                        "prompt_eval_count": 10,
                        "wall_duration_s": 1.0 + question / 10 + (0.5 if mode == "think" else 0),
                        "time_to_first_token_s": 0.1,
                        "low_confidence": 0,
                    }
                )
    return pd.DataFrame(rows)


def test_summarize_shapes_and_mcnemar():
    summaries = summarize(synthetic_frame(), n_boot=200)
    assert set(summaries) == {"mode", "repetition", "paired", "marginal", "worth_matrix"}
    assert len(summaries["mode"]) == 2
    assert len(summaries["repetition"]) == 4

    paired = summaries["paired"].set_index("metric")
    assert paired.loc["mcnemar_correct", "mcnemar_b_think_only"] == 2
    assert paired.loc["mcnemar_correct", "mcnemar_c_no_think_only"] == 0
    assert paired.loc["mcnemar_correct", "mcnemar_exact_p"] == pytest.approx(0.5)
    assert paired.loc["energy_j", "mean_delta_think_minus_no_think"] == pytest.approx(3.0)


def test_marginal_energy_is_computed():
    summaries = summarize(synthetic_frame(), n_boot=200)
    marginal = summaries["marginal"]
    assert not marginal.empty
    repetitions = marginal[marginal["scope"] == "repetition"]
    assert len(repetitions) == 2
    assert (repetitions["delta_correct"] == 1).all()
    assert (repetitions["extra_energy_per_additional_correct_j"] > 0).all()


def test_load_records_from_jsonl(tmp_path):
    frame = synthetic_frame()
    path = tmp_path / "requests.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in frame.to_dict(orient="records")) + "\n",
        encoding="utf-8",
    )
    loaded = load_records(path)
    assert len(loaded) == len(frame)
    assert set(loaded["mode"]) == {"no_think", "think"}


def test_summarize_groups_by_subject_and_level_when_present():
    frame = synthetic_frame()
    frame["subject"] = ["Algebra"] * len(frame)
    frame["level"] = [1 if i % 2 else 2 for i in range(len(frame))]
    summaries = summarize(frame, n_boot=100)
    assert set(summaries) == {
        "mode",
        "repetition",
        "paired",
        "marginal",
        "worth_matrix",
        "subject",
        "level",
    }
    assert set(summaries["subject"]["subject"]) == {"Algebra"}
    assert set(summaries["level"]["level"]) == {1, 2}


def test_worth_matrix_has_overall_and_level_rows_with_decisions():
    frame = synthetic_frame()
    frame["level"] = [1 if row["question_id"] in {"q0", "q1"} else 2 for row in frame.to_dict(orient="records")]
    matrix = summarize(frame, n_boot=20)["worth_matrix"].set_index("level")

    assert matrix.index.tolist() == ["all", 1, 2]
    assert matrix.loc["all", "scope"] == "overall"
    assert matrix.loc[1, "no_think_correct"] == 4
    assert matrix.loc[1, "think_correct"] == 4
    assert not bool(matrix.loc[1, "thinking_worth_it"])
    assert matrix.loc[2, "no_think_correct"] == 0
    assert matrix.loc[2, "think_correct"] == 2
    assert bool(matrix.loc[2, "thinking_worth_it"])
    assert matrix.loc[2, "extra_energy_per_additional_correct_j"] > 0
