import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, HttpUrl


class HelpingMaterialCreate(BaseModel):
    subject_id: uuid.UUID
    material_type: str  # notes | worksheet | past_paper | other
    title: str
    description: Optional[str] = None
    gcr_resource_id: Optional[str] = None
    gcr_link: str


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
    # Lectures Sub-Sprint 2 (YouTube): youtube_video_id removed from here —
    # a lecture is now created empty and the video is set afterward via the
    # dedicated POST .../youtube-video endpoint below, same two-step shape
    # classroom_url already used (it was never part of this schema either).
    subject_id: uuid.UUID
    title: str
    description: Optional[str] = None


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
    status: str  # approved | rejected
    review_note: Optional[str] = None


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
    status: str  # approved | rejected
    review_note: Optional[str] = None
