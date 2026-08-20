"""
Shared grading helpers — pure functions, no DB access, no request/response
models. Two responsibilities live here:

  1. Level abbreviation badges ("O-LEVEL" -> "OL") for anywhere a subject
     name needs a short, clean level tag next to it (student subject
     lists, dashboard, grade report, etc).

  2. Standard percentage -> letter-grade thresholds, used as the
     school-wide default grading scale. Levels can still override this via
     the admin-configurable `grading_schemes` table (see
     routers/marks.py::_recompute_grades_for_subject_batch) — a configured
     GradingScheme always wins when one matches; these thresholds are the
     guaranteed fallback so every student ends up with a percentage AND a
     grade even when a level has no custom scheme set up yet.

Kept dependency-free on purpose so both routers/marks.py (teacher/admin
grade computation) and routers/student_grades.py (student-facing "me"
endpoints) can import it without any risk of circular imports.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Union

Number = Union[int, float, Decimal]


# ---------------------------------------------------------------------------
# Level abbreviation badges
# ---------------------------------------------------------------------------
# Keys match Level.code exactly (see app/routers/academic.py::STANDARD_LEVEL_CODES
# and app/models/academic.py::Level.code) — the 4 standardized, pinned rows.
LEVEL_ABBREVIATIONS: dict[str, str] = {
    "O-LEVEL": "OL",
    "AS-LEVEL": "AS",
    "A2-LEVEL": "A2",
    "A-LEVEL": "Composite",
}


def get_level_abbreviation(level_code: Optional[str]) -> Optional[str]:
    """Map a DB level code to its short display badge.

    Returns None for an unrecognized/missing code rather than raising —
    callers (schemas/serializers) treat a missing badge as "don't render
    one" instead of failing the whole response over a display-only field.
    """
    if not level_code:
        return None
    return LEVEL_ABBREVIATIONS.get(level_code.upper())


def format_subject_with_level(subject_name: str, level_code: Optional[str]) -> str:
    """Convenience formatter for the "Mathematics [AS]" display pattern,
    for any server-rendered text (emails, PDFs, notifications) that wants
    the same badge the Angular UI shows. The Angular UI itself renders the
    badge client-side (see shared/utils/level-badge.util.ts) rather than
    calling this — this exists for non-Angular, server-side text output."""
    badge = get_level_abbreviation(level_code)
    return f"{subject_name} [{badge}]" if badge else subject_name


# ---------------------------------------------------------------------------
# Percentage + standard grade thresholds
# ---------------------------------------------------------------------------
# Ordered highest -> lowest; first threshold the percentage clears wins.
STANDARD_GRADE_THRESHOLDS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("90"), "A*"),
    (Decimal("80"), "A"),
    (Decimal("70"), "B"),
    (Decimal("60"), "C"),
    (Decimal("50"), "D"),
)
STANDARD_FAIL_GRADE = "U"


def calculate_percentage(obtained_total: Optional[Number], max_total: Optional[Number]) -> Decimal:
    """(obtained / max) * 100, pooled across every published assessment.

    Cleanly returns Decimal("0") — never raises and never produces NaN —
    for every edge case a caller might hand in: max_total that is 0,
    None, negative, or otherwise falsy; obtained_total/max_total that are
    None, missing, or not parseable as a number. Callers (this module's
    calculate_grade, routers/marks.py::_recompute_grades_for_subject_batch)
    never need to guard a divide-by-zero or invalid-input case themselves."""
    try:
        obtained = Decimal(str(obtained_total)) if obtained_total is not None else Decimal("0")
        maximum = Decimal(str(max_total)) if max_total is not None else Decimal("0")
    except (ArithmeticError, ValueError, TypeError):
        return Decimal("0")
    if maximum <= 0:
        return Decimal("0")
    return (obtained / maximum * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_grade(percentage: Number) -> str:
    """Standard school-wide grading scale:
        >= 90% -> A*
        >= 80% -> A
        >= 70% -> B
        >= 60% -> C
        >= 50% -> D
        <  50% -> U
    Used as the fallback whenever a level has no matching admin-configured
    GradingScheme row, and always used to fill in a letter grade when a
    configured GradingScheme doesn't cover the computed percentage — see
    _recompute_grades_for_subject_batch. Never raises: an invalid/missing
    percentage is treated as 0 (-> U) rather than propagating a crash."""
    try:
        pct = Decimal(str(percentage)) if percentage is not None else Decimal("0")
    except (ArithmeticError, ValueError, TypeError):
        pct = Decimal("0")
    for threshold, letter in STANDARD_GRADE_THRESHOLDS:
        if pct >= threshold:
            return letter
    return STANDARD_FAIL_GRADE
