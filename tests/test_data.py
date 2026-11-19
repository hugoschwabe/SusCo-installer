from susco.data import (
    dataset_stats,
    load_prepared,
    prepare_rows,
    random_sample,
)


def _raw_rows(n=10):
    return [
        {
            "unique_id": f"test/algebra/{i}.json",
            "problem": f"question {i}",
            "answer": str(i),
            "solution": f"work\n\\boxed{{{i}}}",
            "subject": "Algebra",
            "level": (i % 5) + 1,
        }
        for i in range(n)
    ]


def test_prepare_rows_extracts_math500_fields():
    rows = prepare_rows(
        [
            {
                "unique_id": "test/precalculus/807.json",
                "problem": "Convert the point.",
                "answer": r"\left( 3, \frac{\pi}{2} \right)",
                "solution": r"Therefore, $\boxed{\left( 3, \frac{\pi}{2} \right)}$.",
                "subject": "Precalculus",
                "level": 2,
            }
        ],
    )
    row = rows[0]
    assert row["id"] == "test/precalculus/807.json"
    assert row["gold"] == r"\left( 3, \frac{\pi}{2} \right)"
    assert row["subject"] == "Precalculus"
    assert row["level"] == 2
    assert set(row) == {"id", "question", "answer", "solution", "gold", "subject", "level"}


def test_prepare_rows_falls_back_to_boxed_solution():
    rows = prepare_rows(
        [
            {
                "unique_id": "test/algebra/1.json",
                "problem": "q",
                "answer": "",
                "solution": "work\n\\boxed{\\frac{14}{3}}",
                "subject": "Algebra",
                "level": 3,
            }
        ],
    )
    assert rows[0]["gold"] == r"\frac{14}{3}"


def test_dataset_stats_counts_subjects_and_levels():
    stats = dataset_stats(prepare_rows(_raw_rows(10)))
    assert stats["rows"] == 10
    assert stats["subjects"] == {"Algebra": 10}
    assert stats["levels"] == {"1": 2, "2": 2, "3": 2, "4": 2, "5": 2}
    assert stats["missing_gold"] == 0
    assert stats["duplicate_ids"] == 0


def test_random_sample_is_reproducible_and_exact_size():
    rows = _raw_rows(50)
    first = random_sample(rows, 12, seed=7)
    second = random_sample(rows, 12, seed=7)
    other = random_sample(rows, 12, seed=8)
    assert len(first) == 12
    assert [row["unique_id"] for row in first] == [row["unique_id"] for row in second]
    assert [row["unique_id"] for row in first] != [row["unique_id"] for row in other]


def test_random_sample_all_rows_when_n_is_large():
    rows = _raw_rows(8)
    sample = random_sample(rows, 100, seed=1)
    assert len(sample) == 8


def test_load_prepared_round_trip(tmp_path):
    path = tmp_path / "rows.jsonl"
    rows = prepare_rows(_raw_rows(3))
    path.write_text(
        "\n".join(__import__("json").dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    loaded = load_prepared(path)
    assert len(loaded) == 3
    assert loaded[0]["id"] == "test/algebra/0.json"
