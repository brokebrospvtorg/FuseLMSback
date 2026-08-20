"""
Batch Generator utility.

Single source of truth for what a "standard" FUSE LMS exam batch/session
looks like, on the backend side. Mirrored on the frontend by
src/app/shared/utils/batch-generator.util.ts — keep the two in sync if the
session calendar ever changes (e.g. a school year that adds a third
session). Everything that needs to know what batches SHOULD exist —
the seed script, batch-creation validation, and the "what batches am I
missing" admin view — goes through generate_batches() rather than
hardcoding years or session labels.

Standard shape: for every year in [start_year, start_year + years_ahead],
two sessions:
    "May/June {year}"  -> may 1 .. jun 30
    "Oct/Nov {year}"    -> oct 1 .. nov 30
"""
from dataclasses import dataclass
from datetime import date
from typing import Optional

# Ordered so BATCH_SESSIONS[i] is chronologically followed by
# BATCH_SESSIONS[i + 1] within the same year, and BATCH_SESSIONS[-1] is
# followed by BATCH_SESSIONS[0] of the NEXT year (see next_batch_start_date).
BATCH_SESSIONS: tuple[str, ...] = ("may_june", "oct_nov")

# Display label + calendar month range (inclusive) per session code.
_SESSION_META: dict[str, dict] = {
    "may_june": {"label": "May/June", "start_month": 5, "start_day": 1, "end_month": 6, "end_day": 30},
    "oct_nov": {"label": "Oct/Nov", "start_month": 10, "start_day": 1, "end_month": 11, "end_day": 30},
}

DEFAULT_YEARS_AHEAD = 4  # current year + 4 future years = 5 years total


@dataclass(frozen=True)
class BatchTemplate:
    session: str
    year: int
    name: str
    start_date: date
    end_date: date


def format_batch_name(session: str, year: int) -> str:
    """'may_june', 2026 -> 'May/June 2026'. Raises ValueError for an
    unrecognized session code so bad data fails loudly instead of
    producing a silently wrong label."""
    meta = _SESSION_META.get(session)
    if meta is None:
        raise ValueError(f"Unknown batch session: {session!r}")
    return f"{meta['label']} {year}"


def batch_date_range(session: str, year: int) -> tuple[date, date]:
    """The standard (start_date, end_date) for a session in a given year."""
    meta = _SESSION_META.get(session)
    if meta is None:
        raise ValueError(f"Unknown batch session: {session!r}")
    return (
        date(year, meta["start_month"], meta["start_day"]),
        date(year, meta["end_month"], meta["end_day"]),
    )


def generate_batches(
    start_year: Optional[int] = None,
    years_ahead: int = DEFAULT_YEARS_AHEAD,
    as_of: Optional[date] = None,
) -> list[BatchTemplate]:
    """
    The reusable Batch Generator: current year through `years_ahead` years
    ahead (inclusive), two standardized sessions per year, in chronological
    order.

    start_year defaults to today's year (as_of, defaulting to date.today()).
    Passing as_of/start_year explicitly is what backend validation and tests
    use to generate a deterministic, non-"today"-dependent range.
    """
    if start_year is None:
        start_year = (as_of or date.today()).year

    templates: list[BatchTemplate] = []
    for year in range(start_year, start_year + years_ahead + 1):
        for session in BATCH_SESSIONS:
            start_date, end_date = batch_date_range(session, year)
            templates.append(BatchTemplate(
                session=session,
                year=year,
                name=format_batch_name(session, year),
                start_date=start_date,
                end_date=end_date,
            ))
    return templates


def next_batch_start_date(session: str, year: int) -> date:
    """
    The start_date of whichever standard batch immediately follows the
    given (session, year) — May/June YEAR -> Oct/Nov YEAR's start;
    Oct/Nov YEAR -> May/June (YEAR + 1)'s start.

    This is deliberately NOT the same thing as a batch's own end_date: the
    business rule is "a batch is over once the NEXT batch's month has
    arrived," not "once its own end_date has passed" — the two happen to
    line up for May/June (next batch starts Oct 1, just after Jun 30 ends)
    but there's a real ~4 month gap after Oct/Nov before May/June of the
    following year starts, and the batch is only considered over at that
    later point.
    """
    idx = BATCH_SESSIONS.index(session)
    if idx == len(BATCH_SESSIONS) - 1:
        next_session, next_year = BATCH_SESSIONS[0], year + 1
    else:
        next_session, next_year = BATCH_SESSIONS[idx + 1], year
    next_start, _ = batch_date_range(next_session, next_year)
    return next_start


def is_batch_over(session: str, year: int, as_of: Optional[date] = None) -> bool:
    """True once the next standard batch's month has arrived/passed."""
    today = as_of or date.today()
    return today >= next_batch_start_date(session, year)
