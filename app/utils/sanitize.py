"""
Server-side sanitization for free-text user input.

Free-text fields (complaint descriptions, review notes, broadcast
messages, etc.) get written to the DB and later rendered back to other
users — a classic stored-XSS vector if a client ever submits raw HTML/
JS in one of them. Rather than trust the frontend to escape on render
(easy to miss on one screen, e.g. an admin table that uses
dangerouslySetInnerHTML/innerHTML for "rich" display), we strip HTML at
the point of write, so nothing but plain text ever reaches the DB.

Uses `nh3` — Python bindings for Mozilla's `ammonia` HTML sanitizer,
Rust-backed and fast enough to run on every request without a
noticeable cost.
"""
from typing import Optional
import html

import nh3


def sanitize_text(value: Optional[str]) -> Optional[str]:
    """Strip all HTML tags/attributes from `value`, returning plain text.

    - `None` passes through unchanged, so this is safe to use directly
      as a validator on `Optional[str]` fields (don't turn "field not
      provided" into an empty string).
    - `nh3.clean(value, tags=set())` allow-lists zero tags, so every
      tag is stripped. nh3/ammonia's defaults go further than just
      unwrapping tags: `<script>`/`<style>` elements have their inner
      content removed too (not just the tag), so
      `<script>alert(1)</script>` sanitizes to `""`, not `"alert(1)"`.
    - `nh3.clean()` returns an HTML-safe fragment, which means plain
      characters like `&`, `<`, `>` in the surviving text get entity-
      encoded (e.g. "Fish & Chips" -> "Fish &amp; Chips") so the output
      would be safe to re-embed as HTML. Since we've already stripped
      every tag, there's no HTML structure left for those entities to
      reconstruct, so we `html.unescape()` afterwards to hand back
      genuine plain text (matching what the DB/API contract expects)
      instead of leaking sanitizer-internal entity encoding into
      stored data.
    - Leading/trailing whitespace left behind by removed tags is
      trimmed.
    """
    if value is None:
        return None
    cleaned = nh3.clean(value, tags=set())
    return html.unescape(cleaned).strip()


def sanitize_required_text(value: str) -> str:
    """Same as `sanitize_text`, but for fields that are required (`str`,
    not `Optional[str]`).

    A payload like `"<script>alert(1)</script>"` satisfies Pydantic's
    "non-empty string" / `min_length=1` check *before* sanitization runs,
    then sanitizes down to `""` — silently defeating the required-field
    constraint the schema author intended. Use this instead of
    `sanitize_text` on required free-text fields so that case still
    fails validation (422) rather than writing an empty string.
    """
    cleaned = sanitize_text(value)
    if not cleaned:
        raise ValueError("must not be blank")
    return cleaned
