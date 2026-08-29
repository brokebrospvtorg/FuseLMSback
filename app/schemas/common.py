from enum import Enum
import re
from typing import Literal

# ---------------------------------------------------------------------------
# Reusable Literal types for request/input schemas.
#
# These mirror the corresponding Postgres ENUM types in app/models/enums.py
# — kept as plain typing.Literal (not str/Enum classes) to match the
# existing AttendanceStatusInput convention in
# app/schemas/attendance.py. Using these instead of a bare `str` on INPUT
# schemas means FastAPI/Pydantic rejects an invalid value with a clean 422
# at the API boundary, instead of it reaching business logic and only
# failing later as an unhandled DB error against the Postgres enum
# constraint. Response (*Out) schemas are unaffected — those stay `str`,
# since they only reflect already-valid DB state back to the client.
# ---------------------------------------------------------------------------
ApprovalStatus = Literal["approved", "rejected"]
DayOfWeek = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ComplaintStatus = Literal["open", "in_progress", "resolved", "closed"]
MaterialType = Literal["notes", "worksheet", "past_paper", "other"]
UserStatus = Literal["pending", "active", "suspended"]
# Not part of the original 5 but needed for BatchCreate.session /
# BroadcastNotificationCreate.role below, same rationale — matches
# BATCH_SESSIONS in app/core/batch_utils.py and the 5-value user_role enum
# in app/models/enums.py respectively.
BatchSession = Literal["may_june", "oct_nov"]
Role = Literal["admin", "coordinator", "teacher", "student", "parent"]


class GenderEnum(str, Enum):
    MALE = "Male"
    FEMALE = "Female"
    OTHER = "Other"


class ReligionEnum(str, Enum):
    ISLAM = "Islam"
    CHRISTIANITY = "Christianity"
    HINDUISM = "Hinduism"
    SIKHISM = "Sikhism"
    OTHER = "Other"


class NationalityEnum(str, Enum):
    PAKISTANI = "Pakistani"
    OTHER = "Other"


# ---------------------------------------------------------------------------
# Password complexity validator
# ---------------------------------------------------------------------------
# Shared by every "new password" field across the app (activation, self-
# service change, forgot-password reset, admin-set/admin-reset, and Add
# User's initial_password) so the rule lives in exactly one place instead
# of being duplicated — and able to drift — across five schemas.
#
# Deliberately a plain function (not a class/Enum) so it can be reused two
# ways depending on the Pydantic version/style in a given schema:
#   1. @field_validator("new_password") \n def _v(cls, v): return validate_password_strength(v)
#   2. Annotated[str, AfterValidator(validate_password_strength)]
#
# NOTE: the fixed onboarding/admin-reset handoff values in core/config.py
# (DEFAULT_TEACHER_INITIAL_PASSWORD, etc.) intentionally do NOT go through
# this — they're never user-chosen and are always forced through
# must_change_password=True on next login. This validator only governs
# passwords a human is actually choosing for themselves.
#
# Policy (two branches, not "all five criteria required"):
#   Rule 1 (no special symbol present):
#       >= 8 chars AND uppercase AND lowercase AND digit
#   Rule 2 (a special symbol IS present):
#       >= 8 chars AND digit AND (special symbol already satisfied by
#       definition) — case no longer matters
# A password only has to satisfy ONE of the two branches, not both.
_UPPERCASE_RE = re.compile(r"[A-Z]")
_LOWERCASE_RE = re.compile(r"[a-z]")
_DIGIT_RE = re.compile(r"[0-9]")
_SPECIAL_CHAR_RE = re.compile(r"""[!@#$%^&*(),.?":{}|<>]""")

PASSWORD_MIN_LENGTH = 8

_POLICY_MESSAGE = (
    "Password must be at least 8 characters, and either include an "
    "uppercase letter, a lowercase letter, and a number; or, if it "
    "contains a special character, it must include a number as well."
)


def validate_password_strength(password: str) -> str:
    """
    Enforces a two-branch password policy rather than requiring every
    character class at once:

      - Minimum length of 8 characters, always.
      - Rule 1 (no special symbol in the password): must contain at
        least one uppercase letter, one lowercase letter, and one digit.
      - Rule 2 (a special symbol IS present, from
        [!@#$%^&*(),.?":{}|<>]): case no longer matters — the password
        can be all uppercase or all lowercase — but it must still
        contain at least one digit (the special symbol requirement is
        already satisfied by definition of this branch).

    Raises a single, polite ValueError (-> FastAPI 422) describing the
    whole policy in one readable sentence when neither branch is
    satisfied, rather than a generic "too weak" message or a pile of
    "missing X, missing Y" fragments.
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(_POLICY_MESSAGE)

    has_symbol = bool(_SPECIAL_CHAR_RE.search(password))
    has_digit = bool(_DIGIT_RE.search(password))

    if has_symbol:
        # Rule 2: symbol + digit is enough; case doesn't matter.
        if has_digit:
            return password
        raise ValueError(
            "Your password includes a special character, which is great — "
            "it just also needs at least one number. " + _POLICY_MESSAGE
        )

    # Rule 1: no symbol, so fall back to requiring upper + lower + digit.
    has_upper = bool(_UPPERCASE_RE.search(password))
    has_lower = bool(_LOWERCASE_RE.search(password))
    if has_upper and has_lower and has_digit:
        return password

    missing = []
    if not has_upper:
        missing.append("an uppercase letter")
    if not has_lower:
        missing.append("a lowercase letter")
    if not has_digit:
        missing.append("a number")

    raise ValueError(
        "Your password is missing "
        + " and ".join(missing)
        + " — or, as an alternative, you can add a special character "
        "(like !, @, #, or %) plus a number instead. "
        + _POLICY_MESSAGE
    )