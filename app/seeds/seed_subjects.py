"""
Seed script — pre-declared Cambridge O Level / A Level subject catalog.

schema_update_11: subjects are no longer freely created through the app
(the old POST /api/academic/subjects and the "Subject & Class Management"
custom-subject form with codes are both gone — see app/routers/academic.py
and app/main.py). This script is now the ONLY way rows land in the
`subjects` table. It is idempotent — safe to re-run on every deploy — and
never assigns a subject code, since codes aren't tracked anywhere in this
system by design.

Usage:
    python -m app.seeds.seed_subjects

Also runnable as part of a startup/deploy hook if you'd rather not run it
by hand — see the __main__ guard at the bottom.
"""
from app.core.database import SessionLocal
from app.models import Level, Subject

O_LEVEL = "O Level"
A_LEVEL = "A Level"

# Exactly the two pre-declared academic levels this system supports.
# display_order controls dropdown ordering everywhere Level is listed.
LEVELS = [
    {"name": O_LEVEL, "display_order": 1},
    {"name": A_LEVEL, "display_order": 2},
]

# Cambridge O Level subjects (name only — no subject codes).
O_LEVEL_SUBJECTS = [
    "English Language",
    "Mathematics",
    "Urdu (First Language)",
    "Urdu (Second Language)",
    "Islamiyat",
    "Pakistan Studies",
    "Physics",
    "Chemistry",
    "Biology",
    "Computer Science",
    "Additional Mathematics",
    "Accounting",
    "Business Studies",
    "Economics",
    "Commerce",
    "Global Perspectives",
    "Sociology",
    "Environmental Management",
    "Art & Design",
]

# Cambridge A Level subjects (name only — no subject codes).
A_LEVEL_SUBJECTS = [
    "Mathematics",
    "Further Mathematics",
    "Physics",
    "Chemistry",
    "Computer Science",
    "Biology",
    "Psychology",
    "Accounting",
    "Business",
    "Economics",
    "Law",
    "Sociology",
    "Global Perspectives & Research",
    "English Literature",
    "History",
    "Art & Design",
    "Media Studies",
]


def _get_or_create_level(db, name: str, display_order: int) -> Level:
    level = db.query(Level).filter(Level.name == name, Level.deleted_at.is_(None)).first()
    if level:
        return level
    level = Level(name=name, display_order=display_order)
    db.add(level)
    db.flush()  # get level.id without committing yet
    print(f"  + created Level: {name}")
    return level


def _get_or_create_subject(db, name: str, level: Level) -> None:
    existing = db.query(Subject).filter(
        Subject.name == name, Subject.level_id == level.id, Subject.deleted_at.is_(None)
    ).first()
    if existing:
        return
    db.add(Subject(name=name, level_id=level.id))
    print(f"    + seeded Subject: {name} [{level.name}]")


def seed_subjects() -> None:
    db = SessionLocal()
    try:
        print("Seeding Levels...")
        levels_by_name = {}
        for lvl in LEVELS:
            levels_by_name[lvl["name"]] = _get_or_create_level(db, lvl["name"], lvl["display_order"])
        db.commit()

        print("Seeding O Level subjects...")
        o_level = levels_by_name[O_LEVEL]
        for name in O_LEVEL_SUBJECTS:
            _get_or_create_subject(db, name, o_level)
        db.commit()

        print("Seeding A Level subjects...")
        a_level = levels_by_name[A_LEVEL]
        for name in A_LEVEL_SUBJECTS:
            _get_or_create_subject(db, name, a_level)
        db.commit()

        total = db.query(Subject).filter(Subject.deleted_at.is_(None)).count()
        print(f"Done. {total} subject(s) total in the catalog.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_subjects()
