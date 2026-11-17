from susco.data import (
    assign_difficulty,
    difficulty_raw,
    load_prepared,
    prepare_rows,
    reasoning_steps,
    stratified_sample,
)


def _raw_rows(n=10):
    return [
        {
            "id": f"test-{i}",
            "question": f"question {i}",
            "answer": "\n".join(["step"] * (i + 1)) + f"\n#### {i}",
        }
        for i in range(n)
    ]


def test_reasoning_steps_and_proxy():
    solution = "add 1+1\nsubtract 2-1\n#### 4"
    assert reasoning_steps(solution) == 2
    assert difficulty_raw(solution) > 2


def test_assign_difficulty_is_balanced_and_in_range():
    rows = assign_difficulty(_raw_rows(10), bins=5)
    assert all(1 <= row["difficulty"] <= 5 for row in rows)
    counts = {level: sum(row["difficulty"] == level for row in rows) for level in range(1, 6)}
    assert set(counts.values()) == {2}


def test_prepare_rows_extracts_gold():
    rows = prepare_rows(
        [{"question": "q", "answer": "work\n#### 1,234"}],
        bins=1,
    )
    assert rows[0]["id"] == "test-0"
    assert rows[0]["gold"] == "1234"
    assert rows[0]["difficulty"] == 1


def test_stratified_sample_returns_exact_size():
    rows = assign_difficulty(_raw_rows(50), bins=5)
    sample = stratified_sample(rows, 12, seed=7)
    assert len(sample) == 12
    assert len({row["id"] for row in sample}) == 12


def test_stratified_sample_all_rows_when_n_is_large():
    rows = assign_difficulty(_raw_rows(8), bins=4)
    sample = stratified_sample(rows, 100, seed=1)
    assert len(sample) == 8


def test_load_prepared_round_trip(tmp_path):
    path = tmp_path / "rows.jsonl"
    rows = prepare_rows(_raw_rows(3), bins=3)
    path.write_text(
        "\n".join(__import__("json").dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    loaded = load_prepared(path)
    assert len(loaded) == 3
    assert loaded[0]["id"] == "test-0"
