"""
Seed script — default grading-scheme bands (A* through U) for every active
Level.

The auto-grading calculation in routers/marks.py::_recompute_grades_for_subject_batch
already falls back to the hardcoded standard thresholds in core/grading.py
(calculate_grade) whenever a level has no matching `grading_schemes` row, so
running this script is NOT required for auto-grading to work out of the box.

What it IS for: making the standard scale visible and editable as real,
admin-configurable `grading_schemes` rows (GET/POST /api/academics/grading-schemes)
instead of only existing as a Python-side fallback a school admin can't see
or tweak per level. Run it once per environment (or as part of a deploy
hook) to give every level a sane, editable starting point:

    A*  90-100
    A   80-89.99
    B   70-79.99
    C   60-69.99
    D   50-59.99
    U    0-49.99

Idempotent — safe to re-run; skips any level that already has grading-scheme
rows instead of creating duplicates.

Usage:
    python -m app.seeds.seed_grading_schemes
"""
from decimal import Decimal

from app.core.database import SessionLocal
from app.models import Level, GradingScheme

# Mirrors app/core/grading.py::STANDARD_GRADE_THRESHOLDS / STANDARD_FAIL_GRADE —
# keep both in sync if the school-wide scale ever changes.
STANDARD_BANDS = [
    (Decimal("90"), Decimal("100"), "A*"),
    (Decimal("80"), Decimal("89.99"), "A"),
    (Decimal("70"), Decimal("79.99"), "B"),
    (Decimal("60"), Decimal("69.99"), "C"),
    (Decimal("50"), Decimal("59.99"), "D"),
    (Decimal("0"), Decimal("49.99"), "U"),
]


def _seed_level(db, level: Level) -> None:
    existing = db.query(GradingScheme).filter(
        GradingScheme.level_id == level.id, GradingScheme.deleted_at.is_(None)
    ).first()
    if existing:
        print(f"  - {level.name}: grading scheme already configured, skipping")
        return
    for min_pct, max_pct, letter in STANDARD_BANDS:
        db.add(GradingScheme(
            level_id=level.id, min_percentage=min_pct, max_percentage=max_pct, letter_grade=letter,
        ))
    print(f"  + {level.name}: seeded standard A*-U grading scheme")


def seed_grading_schemes() -> None:
    db = SessionLocal()
    try:
        print("Seeding default grading schemes...")
        levels = db.query(Level).filter(Level.deleted_at.is_(None), Level.is_active.is_(True)).all()
        for level in levels:
            _seed_level(db, level)
        db.commit()
        total = db.query(GradingScheme).filter(GradingScheme.deleted_at.is_(None)).count()
        print(f"Done. {total} grading-scheme row(s) total.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_grading_schemes()
