import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, HttpUrl, field_validator

from app.schemas.common import ApprovalStatus, MaterialType as MaterialTypeInput
from app.utils.sanitize import sanitize_required_text, sanitize_text


class HelpingMaterialCreate(BaseModel):
    subject_id: uuid.UUID
    material_type: MaterialTypeInput
    title: str
    description: Optional[str] = None
    gcr_resource_id: Optional[str] = None
    gcr_link: str

    # Stored-XSS defense-in-depth: strip any HTML/script payload out of
    # free-text input before it ever reaches the DB. title is required, so
    # a markup-only payload (e.g. "<b></b>") must still 422 rather than
    # sanitize down to an empty string.
    @field_validator("title")
    @classmethod
    def _sanitize_title(cls, v: str) -> str:
        return sanitize_required_text(v)

    @field_validator("description")
    @classmethod
    def _sanitize_description(cls, v: Optional[str]) -> Optional[str]:
        return sanitize_text(v)


class HelpingMaterialOut(BaseModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: Optional[str] = None
    material_type: str
    title: str
    description: Optional[str]
    gcr_link: str
    uploaded_by: uuid.UUID
    uploaded_at: datetime

    class Config:
        from_attributes = True


class LectureCreate(BaseModel):
    # LMS & Study Resources refactor: Upload Lecture is a single step again —
    # Title, Description, and YouTube Video Link are all submitted together.
    # youtube_url accepts any recognized YouTube format (standard watch URL,
    # youtu.be, Shorts, or Embed) or a bare 11-character video ID; the
    # router parses it via parse_youtube_video_id() and 400s before a
    # Lecture row is ever created if it doesn't resolve. The resulting
    # youtube_video_id is locked immediately on creation, same as before —
    # only *how* it gets set changed (one call instead of two).
    #
    # classroom_url is deliberately NOT here — Google Classroom is now a
    # single per-Subject setting (see SubjectClassroomLinkCreate below),
    # decoupled entirely from individual lecture uploads.
    subject_id: uuid.UUID
    title: str
    description: Optional[str] = None
    youtube_url: str

    @field_validator("title")
    @classmethod
    def _sanitize_title(cls, v: str) -> str:
        return sanitize_required_text(v)

    @field_validator("description")
    @classmethod
    def _sanitize_description(cls, v: Optional[str]) -> Optional[str]:
        return sanitize_text(v)


class LectureOut(BaseModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: Optional[str] = None
    title: str
    description: Optional[str]
    youtube_video_id: Optional[str] = None
    youtube_video_id_locked: bool
    youtube_visibility: str
    uploaded_by: uuid.UUID
    uploaded_at: datetime
    classroom_url: Optional[str] = None
    classroom_url_locked: bool
    # Sub-Sprint 3, Task 3.3: lets the Teacher's own list show the "pending
    # approval" badge without needing the admin-only classroom-requests
    # queue endpoint (which a Teacher can't call at all — 403). Computed
    # per-lecture in list_lectures; defaults to False elsewhere (accurate
    # immediately after upload/set-link, since no request could exist yet
    # at either of those moments).
    has_pending_edit_request: bool = False
    # Same idea, separate flag — a lecture can have a pending classroom-url
    # request and a pending youtube-video request at the same time, so this
    # can't reuse has_pending_edit_request above without conflating the two.
    has_pending_youtube_edit_request: bool = False

    class Config:
        from_attributes = True


class SetClassroomUrlRequest(BaseModel):
    """Task 1.3 — initial link set only. Fails with 409 if classroom_url_locked
    is already true; this is not an edit path, that's request-edit below."""
    classroom_url: HttpUrl


class RequestClassroomEditRequest(BaseModel):
    """Task 1.4 — Teacher proposes a change to an already-locked link."""
    proposed_url: HttpUrl
    reason: str

    @field_validator("reason")
    @classmethod
    def _sanitize_reason(cls, v: str) -> str:
        return sanitize_required_text(v)


class ClassroomEditRequestOut(BaseModel):
    id: uuid.UUID
    lecture_id: uuid.UUID
    lecture_title: Optional[str] = None
    subject_name: Optional[str] = None
    current_url: Optional[str] = None  # the live classroom_url at request time, for compare-view
    requested_by: uuid.UUID
    requester_name: Optional[str] = None
    proposed_url: str
    reason: Optional[str]
    status: str
    reviewed_by: Optional[uuid.UUID]
    review_note: Optional[str]
    reviewed_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class ClassroomRequestReview(BaseModel):
    """Task 2.2 — Approve/Reject. review_note is the Coordinator/Admin's
    optional note (shown to the Teacher either way); required for neither
    outcome, but most useful on reject."""
    status: ApprovalStatus
    review_note: Optional[str] = None

    @field_validator("review_note")
    @classmethod
    def _sanitize_review_note(cls, v: Optional[str]) -> Optional[str]:
        return sanitize_text(v)


# ---------------------------------------------------------------------------
# YouTube video workflow (Lectures Sub-Sprint 2) — same set-once/request-edit
# shape as the classroom-link schemas above, one field renamed
# (proposed_url -> youtube_url) to match this sub-sprint's own naming.
#
# youtube_url is typed as plain `str`, not HttpUrl like the classroom
# fields: parse_youtube_video_id() (app/utils/youtube.py) also accepts a
# bare 11-character ID with no URL at all, which HttpUrl would reject
# outright before the request even reached the handler. Actual validation
# happens in the router via that parser, not at the schema layer.
# ---------------------------------------------------------------------------
class SetYoutubeVideoRequest(BaseModel):
    """Initial video set only. Fails with 409 if youtube_video_id_locked is
    already true; this is not an edit path, that's request-youtube-edit below."""
    youtube_url: str


class RequestYoutubeEditRequest(BaseModel):
    """Teacher proposes a change to an already-locked video."""
    proposed_url: str
    reason: str

    @field_validator("reason")
    @classmethod
    def _sanitize_reason(cls, v: str) -> str:
        return sanitize_required_text(v)


class YoutubeEditRequestOut(BaseModel):
    id: uuid.UUID
    lecture_id: uuid.UUID
    lecture_title: Optional[str] = None
    subject_name: Optional[str] = None
    current_video_id: Optional[str] = None  # the live youtube_video_id at request time, for compare-view
    requested_by: uuid.UUID
    requester_name: Optional[str] = None
    proposed_url: str
    reason: Optional[str]
    status: str
    reviewed_by: Optional[uuid.UUID]
    review_note: Optional[str]
    reviewed_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class YoutubeRequestReview(BaseModel):
    """Approve/Reject. review_note is the Coordinator/Admin's optional note
    (shown to the Teacher either way); required for neither outcome, but
    most useful on reject."""
    status: ApprovalStatus
    review_note: Optional[str] = None

    @field_validator("review_note")
    @classmethod
    def _sanitize_review_note(cls, v: Optional[str]) -> Optional[str]:
        return sanitize_text(v)


# ---------------------------------------------------------------------------
# Subject-level Google Classroom link (LMS & Study Resources refactor)
# One link per Subject, set once by a Teacher, then directly editable (no
# lock/approval step — that distinction only ever applied to the legacy
# per-lecture classroom_url, see ClassroomEditRequest above).
# ---------------------------------------------------------------------------
class SetSubjectClassroomLinkRequest(BaseModel):
    """Initial set only. The router 409s if a link already exists for the
    subject — use UpdateSubjectClassroomLinkRequest / PUT to change it."""
    classroom_url: HttpUrl


class UpdateSubjectClassroomLinkRequest(BaseModel):
    """Direct edit of an already-set link — this is the 'Edit Google
    Classroom Link' action; no reason/approval required."""
    classroom_url: HttpUrl


class SubjectClassroomLinkOut(BaseModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_name: Optional[str] = None
    classroom_url: str
    set_by: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
