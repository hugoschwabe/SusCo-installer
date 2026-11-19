from susco.grade import (
    answers_equivalent,
    clean_answer,
    extract_gold,
    extract_prediction,
    grade,
    normalize_answer,
)


def test_extract_gold_and_prediction_markers():
    assert extract_gold("some work\n#### 18") == "18"
    assert extract_gold(r"some work\n\boxed{\frac{14}{3}}") == r"\frac{14}{3}"
    assert extract_prediction("work\n#### 1,234") == "1234"
    assert extract_prediction(r"work \boxed{\frac{1}{2}}") == r"\frac{1}{2}"
    assert extract_prediction("The final answer is 42.") == "42"


def test_clean_answer_preserves_math_structure():
    assert clean_answer(r"\$32,\!348") == "32348"
    assert clean_answer("58,500") == "58500"
    assert clean_answer("5x - 7y + 11z + 4 = 0") == "5x - 7y + 11z + 4 = 0"
    assert clean_answer(r"(2,12) \cup (12,102)") == r"(2,12) \cup (12,102)"


def test_extract_prediction_falls_back_to_last_number():
    assert extract_prediction("17 * 23 = 391") == "391"


def test_symbolic_and_numeric_equivalence():
    assert answers_equivalent("1/2", "0.5")
    assert answers_equivalent("2+3", "5")
    assert answers_equivalent("18.0", "18")
    assert not answers_equivalent("18", "19")


def test_latex_equivalence_for_math500():
    assert answers_equivalent(r"\frac{14}{3}", "14/3")
    assert answers_equivalent(
        r"\left( 3, \frac{\pi}{2} \right)",
        r"\left(3,\frac{\pi}{2}\right)",
    )
    assert answers_equivalent(r"90^\circ", "90")
    assert answers_equivalent(r"\sqrt{2}", "1.4142135623730951")
    assert not answers_equivalent(r"\frac{14}{3}", r"\frac{13}{3}")


def test_strict_match_is_separate_from_symbolic_match():
    assert grade("18.0", "18").correct
    assert not grade("18.0", "18").strict_correct
    assert grade("18", "18").strict_correct


def test_grade_statuses():
    assert grade(r"\boxed{18}", "18").status == "correct"
    assert grade(r"\boxed{17}", "18").status == "incorrect"
    assert grade("I cannot solve this", "18").status == "unparseable"
    assert grade(r"\boxed{18}", "18", done_reason="length").status == "truncated"


def test_grade_uses_last_boxed_and_answer_sentence():
    assert grade(r"First \boxed{1}, final \boxed{2}", "2").correct
    assert grade("Answer: 42", "42").correct
    assert grade("The final answer is 0.5", "1/2").correct
    assert grade("I do not know", "18").status == "unparseable"


def test_normalize_answer_returns_sympy_expression():
    value = normalize_answer("3/4")
    assert value is not None
    assert str(value) == "3/4"
