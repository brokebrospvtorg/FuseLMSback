"""
YouTube URL -> 11-character video ID parser.

Lectures Sub-Sprint 1: the app never stores a full YouTube URL — only the
video ID (e.g. "dQw4w9WgXcQ" from a lecture's youtube_video_id column).
Every place that accepts a URL from a Teacher (initial upload, edit
request) runs it through parse_youtube_video_id() first, so a bad/garbage
URL is rejected at the boundary instead of getting stored and only
failing later when the Student tries to play it.

Recognized formats (http/https, with or without "www."):
  - Standard watch page:  https://www.youtube.com/watch?v=dQw4w9WgXcQ
                           (also tolerates extra query params after v=)
  - Shortened:             https://youtu.be/dQw4w9WgXcQ
  - Embed:                 https://www.youtube.com/embed/dQw4w9WgXcQ
  - Shorts:                https://www.youtube.com/shorts/dQw4w9WgXcQ
  - A bare 11-char ID on its own (no URL at all) is also accepted, so a
    Teacher pasting just the ID instead of a full link still works.
"""
import re
from typing import Optional
from urllib.parse import urlparse, parse_qs

# A YouTube video ID is always exactly 11 characters from this set.
# https://developers.google.com/youtube/v3/docs/videos
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "youtu.be", "www.youtu.be",
}


def _is_valid_id(candidate: str) -> bool:
    return bool(_VIDEO_ID_RE.match(candidate))


def parse_youtube_video_id(url_or_id: str) -> Optional[str]:
    """
    Extracts the 11-character YouTube video ID from a URL, or validates a
    bare ID passed on its own. Returns None if nothing valid could be found
    — callers turn that into a 400 at the API boundary (Sub-Sprint 2), this
    function itself never raises on bad input.
    """
    if not url_or_id:
        return None

    candidate = url_or_id.strip()

    # Bare ID, no URL at all.
    if _is_valid_id(candidate):
        return candidate

    # Anything else must parse as an actual URL with a recognized host.
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    host = parsed.netloc.lower()
    if host not in _ALLOWED_HOSTS:
        return None

    path = parsed.path.rstrip("/")

    # youtu.be/<id>
    if host in ("youtu.be", "www.youtu.be"):
        segment = path.lstrip("/")
        return segment if _is_valid_id(segment) else None

    # youtube.com/watch?v=<id>[&...]
    if path == "/watch":
        query_id = parse_qs(parsed.query).get("v", [None])[0]
        return query_id if query_id and _is_valid_id(query_id) else None

    # youtube.com/embed/<id> or youtube.com/shorts/<id>
    for prefix in ("/embed/", "/shorts/"):
        if path.startswith(prefix.rstrip("/")) and path.count("/") == 2:
            segment = path.split("/")[-1]
            return segment if _is_valid_id(segment) else None

    return None
