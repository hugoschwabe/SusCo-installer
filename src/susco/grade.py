"""Final-answer extraction and equivalence grading for GSM8K."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sympy as sp
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

_TRANSFORMS = standard_transformations + (
    implicit_multiplication_application,
    convert_xor,
)

_HASH_RE = re.compile(r"####\s*([^\n\r]+)")
_ANSWER_RE = re.compile(
    r"(?:final\s+)?answer\s*(?:is|:)\s*([^\n\r]+)", re.IGNORECASE
)
_NUMBER_RE = re.compile(
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*/\s*[-+]?\d+(?:\.\d+)?)?"
)


@dataclass(frozen=True)
class GradeResult:
    correct: bool
    strict_correct: bool
    status: str
    prediction: str | None
    normalized_prediction: str | None
    normalized_gold: str | None


def _last(pattern: re.Pattern[str], text: str) -> str | None:
    matches = pattern.findall(text or "")
    if not matches:
        return None
    value = matches[-1]
    if isinstance(value, tuple):
        value = next((item for item in reversed(value) if item), "")
    return value.strip()


def _last_boxed(text: str) -> str | None:
    marker = "\\boxed{"
    start = (text or "").rfind(marker)
    if start < 0:
        return None
    index = start + len(marker)
    depth = 1
    while index < len(text):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + len(marker) : index]
        index += 1
    return None


def extract_gold(answer: str) -> str | None:
    """Return the number after the final ``####`` marker."""

    value = _last(_HASH_RE, answer)
    return clean_answer(value) if value else None


def extract_prediction(text: str) -> str | None:
    """Extract the final answer from model output.

    Preference order: explicit ``####``, ``\\boxed{}``, an "answer is"
    sentence, then the last number in the response.
    """

    for value in (
        _last(_HASH_RE, text),
        _last_boxed(text),
        _last(_ANSWER_RE, text),
    ):
        if value:
            cleaned = clean_answer(value)
            if cleaned:
                return cleaned

    numbers = _NUMBER_RE.findall(text or "")
    if numbers:
        return clean_answer(numbers[-1])
    return None


def clean_answer(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    text = text.strip("`'\" \t\r\n")
    text = text.rstrip(".,;:!?")
    text = text.replace("$", "").replace(",", "").strip()
    if not text:
        return None
    if "=" in text:
        text = text.rsplit("=", 1)[-1].strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    return text or None


def normalize_answer(value: str | None) -> sp.Basic | str | None:
    """Parse an answer into SymPy when possible, otherwise return its text."""

    cleaned = clean_answer(value)
    if cleaned is None:
        return None
    try:
        expr = parse_expr(cleaned, transformations=_TRANSFORMS, evaluate=True)
        if isinstance(expr, sp.Eq):
            expr = expr.rhs
        return sp.simplify(expr)
    except Exception:
        return cleaned


def _canonical_text(value: str | None) -> str:
    cleaned = clean_answer(value) or ""
    return re.sub(r"\s+", "", cleaned).lower()


def _as_number(value: Any) -> float | None:
    try:
        if isinstance(value, sp.Basic):
            return float(sp.N(value))
        return float(str(value))
    except (TypeError, ValueError, TypeError):
        return None


def answers_equivalent(prediction: str | None, gold: str | None, tol: float = 1e-6) -> bool:
    """Compare two answers numerically or symbolically."""

    left = normalize_answer(prediction)
    right = normalize_answer(gold)
    if left is None or right is None:
        return False
    if isinstance(left, sp.Basic) and isinstance(right, sp.Basic):
        left_num = _as_number(left)
        right_num = _as_number(right)
        if left_num is not None and right_num is not None:
            return abs(left_num - right_num) <= tol
        try:
            return bool(sp.simplify(left - right) == 0)
        except Exception:
            return False
    return str(left) == str(right)


def grade(prediction: str | None, gold: str | None, done_reason: str | None = None) -> GradeResult:
    """Grade a prediction against the GSM8K gold answer."""

    if prediction and any(
        marker in prediction.lower()
        for marker in ("####", "\\boxed", "answer is", "final answer")
    ):
        extracted = extract_prediction(prediction)
        if extracted:
            prediction = extracted

    normalized_gold = normalize_answer(gold)
    normalized_pred = normalize_answer(prediction)
    strict = _canonical_text(prediction) == _canonical_text(gold) and bool(
        _canonical_text(gold)
    )
    correct = answers_equivalent(prediction, gold)

    if done_reason == "length":
        status = "truncated"
    elif normalized_pred is None:
        status = "unparseable"
    elif correct:
        status = "correct"
    elif isinstance(normalized_pred, str):
        status = "unparseable"
    else:
        status = "incorrect"

    return GradeResult(
        correct=correct,
        strict_correct=strict,
        status=status,
        prediction=prediction,
        normalized_prediction=str(normalized_pred) if normalized_pred is not None else None,
        normalized_gold=str(normalized_gold) if normalized_gold is not None else None,
    )
