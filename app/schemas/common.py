from enum import Enum


class BoardEnum(str, Enum):
    """
    Mirrors the `board` Postgres enum (schema_update_11) and the frontend's
    Board enum (src/app/core/models/enums.ts) — keep all three in sync by
    hand, same convention as every other enum in this project (see the note
    atop app/models/enums.py). Used by Student registration/edit (single,
    required), Teacher registration/edit (one or more), and Batch
    creation/edit (single).
    """
    BRITISH_COUNCIL = "British Council"
    EDEXCEL = "Edexcel"
    LRN = "LRN"
    # schema_update_16: catalog Subjects only (POST /api/academic/subjects)
    # — not offered on Student/Teacher/Batch forms.
    ALL = "All"
