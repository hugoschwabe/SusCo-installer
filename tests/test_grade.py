from susco.grade import (
    answers_equivalent,
    extract_gold,
    extract_prediction,
    grade,
    normalize_answer,
)


def test_extract_gold_and_prediction_markers():
    assert extract_gold("some work\n#### 18") == "18"
    assert extract_prediction("work\n#### 1,234") == "1234"
    assert extract_prediction(r"work \boxed{\frac{1}{2}}") == r"\frac{1}{2}"
    assert extract_prediction("The final answer is 42.") == "42"


def test_extract_prediction_falls_back_to_last_number():
    assert extract_prediction("17 * 23 = 391") == "391"


def test_symbolic_and_numeric_equivalence():
    assert answers_equivalent("1/2", "0.5")
    assert answers_equivalent("2+3", "5")
    assert answers_equivalent("18.0", "18")
    assert not answers_equivalent("18", "19")


def test_strict_match_is_separate_from_symbolic_match():
    assert grade("18.0", "18").correct
    assert not grade("18.0", "18").strict_correct
    assert grade("18", "18").strict_correct


def test_grade_statuses():
    assert grade("#### 18", "18").status == "correct"
    assert grade("#### 17", "18").status == "incorrect"
    assert grade("I cannot solve this", "18").status == "unparseable"
    assert grade("#### 18", "18", done_reason="length").status == "truncated"


def test_normalize_answer_returns_sympy_expression():
    value = normalize_answer("3/4")
    assert value is not None
    assert str(value) == "3/4"
