"""
Unit tests for app.utils.youtube.parse_youtube_video_id.

Run with: pytest tests/test_youtube.py -v
"""
import pytest

from app.utils.youtube import parse_youtube_video_id

VALID_ID = "dQw4w9WgXcQ"


class TestValidFormats:
    def test_standard_watch_url(self):
        assert parse_youtube_video_id(f"https://www.youtube.com/watch?v={VALID_ID}") == VALID_ID

    def test_standard_watch_url_no_www(self):
        assert parse_youtube_video_id(f"https://youtube.com/watch?v={VALID_ID}") == VALID_ID

    def test_standard_watch_url_http(self):
        assert parse_youtube_video_id(f"http://www.youtube.com/watch?v={VALID_ID}") == VALID_ID

    def test_watch_url_with_extra_query_params(self):
        # Timestamp / playlist params after v= shouldn't break extraction.
        url = f"https://www.youtube.com/watch?v={VALID_ID}&t=42s&list=PL123"
        assert parse_youtube_video_id(url) == VALID_ID

    def test_watch_url_with_v_not_first_param(self):
        url = f"https://www.youtube.com/watch?list=PL123&v={VALID_ID}"
        assert parse_youtube_video_id(url) == VALID_ID

    def test_shortened_youtu_be(self):
        assert parse_youtube_video_id(f"https://youtu.be/{VALID_ID}") == VALID_ID

    def test_shortened_youtu_be_with_query(self):
        assert parse_youtube_video_id(f"https://youtu.be/{VALID_ID}?t=10") == VALID_ID

    def test_embed_url(self):
        assert parse_youtube_video_id(f"https://www.youtube.com/embed/{VALID_ID}") == VALID_ID

    def test_shorts_url(self):
        assert parse_youtube_video_id(f"https://www.youtube.com/shorts/{VALID_ID}") == VALID_ID

    def test_mobile_host(self):
        assert parse_youtube_video_id(f"https://m.youtube.com/watch?v={VALID_ID}") == VALID_ID

    def test_bare_video_id(self):
        # A Teacher pasting just the ID, no URL at all.
        assert parse_youtube_video_id(VALID_ID) == VALID_ID

    def test_trims_whitespace(self):
        assert parse_youtube_video_id(f"  {VALID_ID}  ") == VALID_ID
        assert parse_youtube_video_id(f"  https://youtu.be/{VALID_ID}  ") == VALID_ID

    def test_trailing_slash_on_watch_path(self):
        assert parse_youtube_video_id(f"https://www.youtube.com/embed/{VALID_ID}/") == VALID_ID


class TestInvalidInput:
    @pytest.mark.parametrize("bad_input", [
        "",
        None,
        "not a url at all",
        "https://vimeo.com/123456789",
        "https://www.youtube.com/watch?v=tooShort",
        "https://www.youtube.com/watch?v=WayTooLongToBeAVideoId123",
        "https://www.youtube.com/watch",  # no v= at all
        "https://www.youtube.com/",  # channel/home page
        "https://www.youtube.com/embed/",  # missing id
        "https://www.youtube.com/embed/../etc",
        "ftp://youtu.be/dQw4w9WgXcQ",  # wrong scheme
        "javascript:alert(1)",
        "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",  # lookalike host
    ])
    def test_rejects_invalid_input(self, bad_input):
        assert parse_youtube_video_id(bad_input) is None

    def test_rejects_playlist_only_url(self):
        assert parse_youtube_video_id("https://www.youtube.com/playlist?list=PL123") is None

    def test_rejects_id_with_invalid_characters(self):
        assert parse_youtube_video_id("https://youtu.be/dQw4w9WgX!Q") is None
