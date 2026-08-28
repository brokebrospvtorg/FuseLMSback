import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license, require_teacher_assigned
from app.core.audit import log_action
from app.core.limiter import limiter
from app.core.notifications import notify
from app.utils.youtube import parse_youtube_video_id
from app.models import (
    HelpingMaterial, Lecture, ClassroomEditRequest, YoutubeEditRequest, SubjectClassroomLink,
    Enrollment, User, Subject,
)
from app.schemas.content import (
    HelpingMaterialCreate, HelpingMaterialOut, LectureCreate, LectureOut,
    SetClassroomUrlRequest, RequestClassroomEditRequest, ClassroomEditRequestOut, ClassroomRequestReview,
    SetYoutubeVideoRequest, RequestYoutubeEditRequest, YoutubeEditRequestOut, YoutubeRequestReview,
    SetSubjectClassroomLinkRequest, UpdateSubjectClassroomLinkRequest, SubjectClassroomLinkOut,
)

router = APIRouter(prefix="/api/content", tags=["content"], dependencies=[Depends(check_license)])

# Lectures Sub-Sprint 2, Task 2.1/2.2: the spec's endpoint paths are
# top-level (/api/classroom-requests, not /api/content/classroom-requests)
# — a second router in this file rather than nesting under the content
# router's /api/content prefix, registered separately in main.py.
classroom_requests_router = APIRouter(
    prefix="/api/classroom-requests", tags=["classroom-requests"], dependencies=[Depends(check_license)],
)

# Lectures Sub-Sprint 3, Task 3.1: same top-level-router reasoning as
# classroom_requests_router above, matching the plan doc's literal
# "/api/youtube-requests" path exactly (unlike the two Sub-Sprint 2
# lecture-scoped endpoints, which stayed nested under /api/content).
youtube_requests_router = APIRouter(
    prefix="/api/youtube-requests", tags=["youtube-requests"], dependencies=[Depends(check_license)],
)


def _student_has_subject_access(db: Session, student_id: uuid.UUID, subject_id: uuid.UUID) -> bool:
    """
    ACCESS CONTROL (app-layer, not schema): student sees content if they have
    ANY non-deleted enrollments row for that subject_id (any batch) — so
    students keep access to past-completed subjects' content indefinitely.
    """
    return db.query(Enrollment).filter(
        Enrollment.student_id == student_id, Enrollment.subject_id == subject_id,
        Enrollment.deleted_at.is_(None),
    ).first() is not None


# ---------------------------------------------------------------------------
# Helping materials (subject-scoped, not batch-scoped, reusable across years)
# ---------------------------------------------------------------------------
@router.post("/materials", response_model=HelpingMaterialOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def upload_material(request: Request, payload: HelpingMaterialCreate, db: Session = Depends(get_db),
                     current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    # BOLA/IDOR fix: HelpingMaterialCreate only carries subject_id (no
    # batch_id — materials are subject-scoped, reusable across batches),
    # so the check runs on subject_id alone (assigned in ANY batch).
    require_teacher_assigned(subject_id=payload.subject_id, db=db, current_user=current_user)
    material = HelpingMaterial(**payload.model_dump(), uploaded_by=current_user.id)
    db.add(material)
    db.commit()
    db.refresh(material)
    return material


@router.get("/materials", response_model=List[HelpingMaterialOut])
def list_materials(subject_id: uuid.UUID, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    if current_user.role == "student" and not _student_has_subject_access(db, current_user.id, subject_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled in this subject")
    return db.query(HelpingMaterial).filter(
        HelpingMaterial.subject_id == subject_id, HelpingMaterial.deleted_at.is_(None)
    ).order_by(HelpingMaterial.uploaded_at.desc()).all()


@router.get("/materials/me", response_model=List[HelpingMaterialOut])
def list_my_materials(db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("student"))):
    """
    Lectures Sub-Sprint 5, Task 5.1 support — the Student portal's content
    page (ContentComponent) has always called this exact path
    (core/services/content.service.ts: getMyMaterials -> GET /materials/me),
    but nothing on the backend actually implemented it: list_materials above
    requires a single subject_id, there was no aggregate-across-enrollments
    route. That means the Student content page has been 404ing on every
    load — not a Sub-Sprint 5 gap specifically, a pre-existing broken data
    path this sub-sprint's own "Student View" task depends on, so it's
    fixed here rather than filed separately.

    Same access rule as list_materials (_student_has_subject_access: any
    non-deleted enrollment row, any batch, past or present), just applied
    across every subject at once instead of one at a time.
    """
    subject_ids = [
        row[0] for row in db.query(Enrollment.subject_id).filter(
            Enrollment.student_id == current_user.id, Enrollment.deleted_at.is_(None),
        ).distinct().all()
    ]
    if not subject_ids:
        return []

    rows = (
        db.query(HelpingMaterial, Subject.name.label("subject_name"))
        .join(Subject, Subject.id == HelpingMaterial.subject_id)
        .filter(HelpingMaterial.subject_id.in_(subject_ids), HelpingMaterial.deleted_at.is_(None))
        .order_by(HelpingMaterial.uploaded_at.desc())
        .all()
    )
    results = []
    for material, subject_name in rows:
        out = HelpingMaterialOut.model_validate(material)
        out.subject_name = subject_name
        results.append(out)
    return results


@router.delete("/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def replace_material(material_id: uuid.UUID, db: Session = Depends(get_db),
                      current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    """Materials are 'replaced' via soft-delete + new upload, per the persists/reusable design."""
    material = db.query(HelpingMaterial).filter(
        HelpingMaterial.id == material_id, HelpingMaterial.deleted_at.is_(None)
    ).first()
    if not material:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Material not found")

    # BOLA/IDOR fix: role alone let any Teacher delete any Teacher's
    # material by guessing/enumerating material_id. Admin/Coordinator keep
    # full access (they can already fully manage content); a Teacher may
    # only delete material they themselves uploaded.
    if current_user.role == "teacher" and material.uploaded_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                             detail="You can only delete material you uploaded")

    material.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Lectures (YouTube unlisted; same batch-independent access rule)
# ---------------------------------------------------------------------------
@router.post("/lectures", response_model=LectureOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def upload_lecture(request: Request, payload: LectureCreate, db: Session = Depends(get_db),
                    current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    """
    LMS & Study Resources refactor: Title, Description, and YouTube Video
    Link are all submitted together — the video is parsed and locked right
    here, at creation, instead of the old two-step "create empty, then set
    video separately" flow. Google Classroom is intentionally not part of
    this call at all anymore (see the Subject-level classroom link section
    below) — a bad/unrecognized YouTube link still 400s before any row is
    written, same defend-at-the-boundary approach as before.
    """
    # BOLA/IDOR fix: LectureCreate only carries subject_id (no batch_id —
    # same subject-scoped, reusable-across-batches design as materials),
    # so the check runs on subject_id alone (assigned in ANY batch).
    require_teacher_assigned(subject_id=payload.subject_id, db=db, current_user=current_user)

    video_id = parse_youtube_video_id(payload.youtube_url)
    if not video_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not recognize that as a YouTube link or video ID. "
                   "Paste a full YouTube link (youtube.com/watch?v=..., youtu.be/..., "
                   "/embed/..., /shorts/...) or an 11-character video ID.",
        )
    data = payload.model_dump(exclude={"youtube_url"})
    lecture = Lecture(
        **data,
        uploaded_by=current_user.id,
        youtube_video_id=video_id,
        youtube_video_id_locked=True,
    )
    db.add(lecture)
    db.commit()
    db.refresh(lecture)
    return lecture


@router.get("/lectures", response_model=List[LectureOut])
def list_lectures(subject_id: uuid.UUID, mine: bool = False, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    # KNOWN LIMITATION: "unlisted" gating stops browsing but not a copied link
    # being shared outside the app. Accepted v1 trade-off.
    if current_user.role == "student" and not _student_has_subject_access(db, current_user.id, subject_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled in this subject")

    query = db.query(Lecture).filter(Lecture.subject_id == subject_id, Lecture.deleted_at.is_(None))
    if mine:
        # Sub-Sprint 3: the Teacher's own screen only shows what they
        # uploaded, not everything ever uploaded for the subject (a
        # subject can have multiple Teachers across batches/years).
        query = query.filter(Lecture.uploaded_by == current_user.id)
    lectures = query.order_by(Lecture.uploaded_at.desc()).all()

    pending_lecture_ids = {
        row.lecture_id for row in db.query(ClassroomEditRequest.lecture_id).filter(
            ClassroomEditRequest.lecture_id.in_([l.id for l in lectures]),
            ClassroomEditRequest.status == "pending",
            ClassroomEditRequest.deleted_at.is_(None),
        ).all()
    } if lectures else set()

    pending_youtube_lecture_ids = {
        row.lecture_id for row in db.query(YoutubeEditRequest.lecture_id).filter(
            YoutubeEditRequest.lecture_id.in_([l.id for l in lectures]),
            YoutubeEditRequest.status == "pending",
            YoutubeEditRequest.deleted_at.is_(None),
        ).all()
    } if lectures else set()

    results = []
    for lecture in lectures:
        out = LectureOut.model_validate(lecture)
        out.has_pending_edit_request = lecture.id in pending_lecture_ids
        out.has_pending_youtube_edit_request = lecture.id in pending_youtube_lecture_ids
        results.append(out)
    return results


@router.get("/lectures/me", response_model=List[LectureOut])
def list_my_lectures(db: Session = Depends(get_db),
                      current_user: User = Depends(require_roles("student"))):
    """
    Same missing-endpoint situation as list_my_materials above —
    core/services/content.service.ts has always called GET /lectures/me,
    nothing implemented it. Fixed here since Task 5.1 (the "Open Google
    Classroom" button) can't be built on top of a 404.

    has_pending_edit_request is deliberately NOT computed here — that
    flag exists so a *Teacher* knows not to submit a second request
    (Sub-Sprint 3, Task 3.3); a Student has no edit-request action to
    gate, so it isn't meaningful in this response and is left at its
    schema default (False) rather than doing the extra query for nothing.
    """
    subject_ids = [
        row[0] for row in db.query(Enrollment.subject_id).filter(
            Enrollment.student_id == current_user.id, Enrollment.deleted_at.is_(None),
        ).distinct().all()
    ]
    if not subject_ids:
        return []

    rows = (
        db.query(Lecture, Subject.name.label("subject_name"))
        .join(Subject, Subject.id == Lecture.subject_id)
        .filter(Lecture.subject_id.in_(subject_ids), Lecture.deleted_at.is_(None))
        .order_by(Lecture.uploaded_at.desc())
        .all()
    )
    results = []
    for lecture, subject_name in rows:
        out = LectureOut.model_validate(lecture)
        out.subject_name = subject_name
        results.append(out)
    return results


# ---------------------------------------------------------------------------
# Classroom link workflow (Lectures Sub-Sprint 1)
# Teacher sets classroom_url exactly once; further changes require a
# Coordinator/Admin-approved edit request. Deliberately Teacher-only on both
# endpoints below — Admin/Coordinator can create a Lecture (see upload_lecture
# above) but this specific set-once/request-edit flow belongs to whichever
# Teacher owns the lecture's content, matching the spec's stated workflow.
# ---------------------------------------------------------------------------
@router.post("/lectures/{lecture_id}/classroom-url", response_model=LectureOut)
def set_classroom_url(lecture_id: uuid.UUID, payload: SetClassroomUrlRequest, db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("teacher"))):
    lecture = db.query(Lecture).filter(Lecture.id == lecture_id, Lecture.deleted_at.is_(None)).first()
    if not lecture:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lecture not found")
    if lecture.uploaded_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only set the link on your own lecture")
    if lecture.classroom_url_locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A classroom link is already set for this lecture. Use Request Edit to propose a change.",
        )

    lecture.classroom_url = str(payload.classroom_url)
    lecture.classroom_url_locked = True
    log_action(db, current_user.id, "classroom_url_set", "lectures", lecture.id, None, {"classroom_url": lecture.classroom_url})
    db.commit()
    db.refresh(lecture)
    return lecture


@router.post("/lectures/{lecture_id}/request-edit", response_model=ClassroomEditRequestOut,
             status_code=status.HTTP_201_CREATED)
def request_classroom_url_edit(lecture_id: uuid.UUID, payload: RequestClassroomEditRequest,
                                db: Session = Depends(get_db),
                                current_user: User = Depends(require_roles("teacher"))):
    lecture = db.query(Lecture).filter(Lecture.id == lecture_id, Lecture.deleted_at.is_(None)).first()
    if not lecture:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lecture not found")
    if lecture.uploaded_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only request edits on your own lecture")
    if not lecture.classroom_url_locked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No classroom link set yet — use the initial set-link action instead of requesting an edit.",
        )

    existing_pending = db.query(ClassroomEditRequest).filter(
        ClassroomEditRequest.lecture_id == lecture_id,
        ClassroomEditRequest.status == "pending",
        ClassroomEditRequest.deleted_at.is_(None),
    ).first()
    if existing_pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An edit request for this lecture is already pending review.",
        )

    edit_request = ClassroomEditRequest(
        lecture_id=lecture_id,
        requested_by=current_user.id,
        proposed_url=str(payload.proposed_url),
        reason=payload.reason,
    )
    db.add(edit_request)
    db.commit()
    db.refresh(edit_request)
    return edit_request


# ---------------------------------------------------------------------------
# Subject-level Google Classroom link (LMS & Study Resources refactor)
# Replaces the per-lecture classroom_url flow above for the Student "LMS &
# Study Resources" screen and the Teacher "Lectures & Notes" screen: one
# link per Subject, set once, then directly editable — no lock/approval
# queue for this one (that distinction stays specific to the legacy
# per-lecture flow and to YouTube video edits below).
# ---------------------------------------------------------------------------
def _to_subject_classroom_link_out(db: Session, link: SubjectClassroomLink) -> SubjectClassroomLinkOut:
    out = SubjectClassroomLinkOut.model_validate(link)
    subject = db.query(Subject).filter(Subject.id == link.subject_id).first()
    if subject:
        out.subject_name = subject.name
    return out


@router.get("/subjects/{subject_id}/classroom-link", response_model=Optional[SubjectClassroomLinkOut])
def get_subject_classroom_link(subject_id: uuid.UUID, db: Session = Depends(get_db),
                                current_user: User = Depends(get_current_user)):
    """Returns null (not 404) when no link has been set yet for the subject —
    that's the normal "Add Google Classroom Link" state, not an error."""
    if current_user.role == "student" and not _student_has_subject_access(db, current_user.id, subject_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled in this subject")
    link = db.query(SubjectClassroomLink).filter(SubjectClassroomLink.subject_id == subject_id).first()
    if not link:
        return None
    return _to_subject_classroom_link_out(db, link)


@router.get("/classroom-links/me", response_model=List[SubjectClassroomLinkOut])
def list_my_classroom_links(db: Session = Depends(get_db),
                             current_user: User = Depends(require_roles("student"))):
    """Aggregate across every subject the student is (or was) enrolled in —
    same access rule as list_my_materials/list_my_lectures — for the "LMS &
    Study Resources" screen's Google Classroom card grid. Subjects with no
    link set yet are simply omitted; the screen shows what's available."""
    subject_ids = [
        row[0] for row in db.query(Enrollment.subject_id).filter(
            Enrollment.student_id == current_user.id, Enrollment.deleted_at.is_(None),
        ).distinct().all()
    ]
    if not subject_ids:
        return []

    rows = (
        db.query(SubjectClassroomLink, Subject.name.label("subject_name"))
        .join(Subject, Subject.id == SubjectClassroomLink.subject_id)
        .filter(SubjectClassroomLink.subject_id.in_(subject_ids))
        .order_by(Subject.name.asc())
        .all()
    )
    results = []
    for link, subject_name in rows:
        out = SubjectClassroomLinkOut.model_validate(link)
        out.subject_name = subject_name
        results.append(out)
    return results


@router.post("/subjects/{subject_id}/classroom-link", response_model=SubjectClassroomLinkOut,
             status_code=status.HTTP_201_CREATED)
def set_subject_classroom_link(subject_id: uuid.UUID, payload: SetSubjectClassroomLinkRequest,
                                db: Session = Depends(get_db),
                                current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    """"Add Google Classroom Link" — initial set only. 409s if the subject
    already has one; use the PUT below ("Edit Google Classroom Link") to
    change it instead."""
    subject = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")

    existing = db.query(SubjectClassroomLink).filter(SubjectClassroomLink.subject_id == subject_id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Google Classroom link is already set for this subject. Use Edit Google Classroom Link instead.",
        )

    link = SubjectClassroomLink(
        subject_id=subject_id, classroom_url=str(payload.classroom_url), set_by=current_user.id,
    )
    db.add(link)
    db.flush()
    log_action(db, current_user.id, "subject_classroom_link_set", "subject_classroom_links", link.id,
               None, {"classroom_url": link.classroom_url})
    db.commit()
    db.refresh(link)
    return _to_subject_classroom_link_out(db, link)


@router.put("/subjects/{subject_id}/classroom-link", response_model=SubjectClassroomLinkOut)
def update_subject_classroom_link(subject_id: uuid.UUID, payload: UpdateSubjectClassroomLinkRequest,
                                   db: Session = Depends(get_db),
                                   current_user: User = Depends(require_roles("teacher", "admin", "coordinator"))):
    """"Edit Google Classroom Link" — direct update, no approval step."""
    link = db.query(SubjectClassroomLink).filter(SubjectClassroomLink.subject_id == subject_id).first()
    if not link:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Google Classroom link set for this subject yet — add one first.",
        )

    old_url = link.classroom_url
    link.classroom_url = str(payload.classroom_url)
    link.updated_at = datetime.now(timezone.utc)
    log_action(db, current_user.id, "subject_classroom_link_updated", "subject_classroom_links", link.id,
               {"classroom_url": old_url}, {"classroom_url": link.classroom_url})
    db.commit()
    db.refresh(link)
    return _to_subject_classroom_link_out(db, link)


# ---------------------------------------------------------------------------
# YouTube video workflow (Lectures Sub-Sprint 2)
# Teacher sets youtube_video_id exactly once; further changes require a
# Coordinator/Admin-approved edit request (Sub-Sprint 3). Deliberately
# Teacher-only on both endpoints below — same reasoning as the classroom
# pair above: Admin/Coordinator can create a Lecture, but this specific
# set-once/request-edit flow belongs to whichever Teacher owns the content.
#
# NOTE on path: the sub-sprint plan document writes these as top-level
# "/api/lectures/:id/..." — kept nested under /api/content instead (this
# router's prefix), matching where every other lecture-scoped action
# already lives (classroom-url, request-edit, /lectures, /lectures/me
# above). A second top-level prefix for just these two endpoints would
# fragment the API surface for no functional benefit; the review-queue
# endpoints in Sub-Sprint 3 (GET/PATCH /api/youtube-requests) will still
# get their own top-level router, matching /api/classroom-requests below.
# ---------------------------------------------------------------------------
@router.post("/lectures/{lecture_id}/youtube-video", response_model=LectureOut)
def set_youtube_video(lecture_id: uuid.UUID, payload: SetYoutubeVideoRequest, db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("teacher"))):
    lecture = db.query(Lecture).filter(Lecture.id == lecture_id, Lecture.deleted_at.is_(None)).first()
    if not lecture:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lecture not found")
    if lecture.uploaded_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only set the video on your own lecture")
    if lecture.youtube_video_id_locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A video is already set for this lecture. Use Request Edit to propose a change.",
        )

    video_id = parse_youtube_video_id(payload.youtube_url)
    if not video_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Couldn't recognize that as a YouTube link or video ID. Paste the full video URL (youtube.com/watch?v=..., youtu.be/..., /embed/..., or /shorts/...).",
        )

    lecture.youtube_video_id = video_id
    lecture.youtube_video_id_locked = True
    log_action(db, current_user.id, "youtube_video_set", "lectures", lecture.id, None, {"youtube_video_id": video_id})
    db.commit()
    db.refresh(lecture)
    return lecture


@router.post("/lectures/{lecture_id}/request-youtube-edit", response_model=YoutubeEditRequestOut,
             status_code=status.HTTP_201_CREATED)
def request_youtube_video_edit(lecture_id: uuid.UUID, payload: RequestYoutubeEditRequest,
                                db: Session = Depends(get_db),
                                current_user: User = Depends(require_roles("teacher"))):
    lecture = db.query(Lecture).filter(Lecture.id == lecture_id, Lecture.deleted_at.is_(None)).first()
    if not lecture:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lecture not found")
    if lecture.uploaded_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only request edits on your own lecture")
    if not lecture.youtube_video_id_locked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No video set yet — use the initial upload action instead of requesting an edit.",
        )

    # Validated up front (same as set_youtube_video) so a malformed proposal
    # never reaches the pending queue — the Coordinator/Admin reviewing it
    # in Sub-Sprint 3 only ever sees requests that will actually resolve to
    # a playable video ID if approved.
    if not parse_youtube_video_id(payload.proposed_url):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Couldn't recognize that as a YouTube link or video ID. Paste the full video URL (youtube.com/watch?v=..., youtu.be/..., /embed/..., or /shorts/...).",
        )

    existing_pending = db.query(YoutubeEditRequest).filter(
        YoutubeEditRequest.lecture_id == lecture_id,
        YoutubeEditRequest.status == "pending",
        YoutubeEditRequest.deleted_at.is_(None),
    ).first()
    if existing_pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An edit request for this lecture's video is already pending review.",
        )

    edit_request = YoutubeEditRequest(
        lecture_id=lecture_id,
        requested_by=current_user.id,
        proposed_url=payload.proposed_url,
        reason=payload.reason,
    )
    db.add(edit_request)
    db.commit()
    db.refresh(edit_request)
    return edit_request


# ---------------------------------------------------------------------------
# Classroom edit request review queue (Lectures Sub-Sprint 2)
# ---------------------------------------------------------------------------
def _to_classroom_request_out(db: Session, req: ClassroomEditRequest) -> ClassroomEditRequestOut:
    """Enriches the raw row with everything the Admin/Coordinator queue
    needs to actually decide — lecture title, subject, requester name, and
    the CURRENT live link so the UI can show a current-vs-proposed
    comparison (Sub-Sprint 4, Task 4.2) without a second round-trip."""
    out = ClassroomEditRequestOut.model_validate(req)
    lecture = db.query(Lecture).filter(Lecture.id == req.lecture_id).first()
    if lecture:
        out.lecture_title = lecture.title
        out.current_url = lecture.classroom_url
        subject = db.query(Subject).filter(Subject.id == lecture.subject_id).first()
        if subject:
            out.subject_name = subject.name
    requester = db.query(User).filter(User.id == req.requested_by).first()
    if requester:
        out.requester_name = requester.full_name
    return out


@classroom_requests_router.get("", response_model=List[ClassroomEditRequestOut])
def list_classroom_requests(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Task 2.1. `status_filter` defaults to unset (returns every request,
    any status) rather than hardcoding "pending" — the same endpoint also
    backs a "past decisions" history view later without needing a second
    route. The frontend queue (Sub-Sprint 4) is expected to pass
    ?status_filter=pending for the default view.
    """
    query = db.query(ClassroomEditRequest).filter(ClassroomEditRequest.deleted_at.is_(None))
    if status_filter:
        if status_filter not in ("pending", "approved", "rejected"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status_filter")
        query = query.filter(ClassroomEditRequest.status == status_filter)
    requests = query.order_by(ClassroomEditRequest.created_at.desc()).all()
    return [_to_classroom_request_out(db, r) for r in requests]


@classroom_requests_router.patch("/{request_id}/review", response_model=ClassroomEditRequestOut)
def review_classroom_request(
    request_id: uuid.UUID,
    payload: ClassroomRequestReview,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Task 2.2 + 2.3. Approve writes proposed_url into lectures.classroom_url
    (the lecture was already locked when the Teacher's initial set-link
    call succeeded — approving an edit doesn't need to re-lock it, it's
    already true). Reject leaves the lecture's link untouched.

    2.3's "duplicate edits can't bypass" guard: the request must currently
    be 'pending' — reviewing an already-approved/rejected request 409s
    instead of silently re-applying (or worse, a second Coordinator
    approving a request a first Coordinator already rejected, overwriting
    that decision without anyone seeing it happened).
    """
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be 'approved' or 'rejected'")

    req = db.query(ClassroomEditRequest).filter(
        ClassroomEditRequest.id == request_id, ClassroomEditRequest.deleted_at.is_(None)
    ).first()
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if req.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request was already {req.status} — it can't be reviewed again.",
        )

    lecture = db.query(Lecture).filter(Lecture.id == req.lecture_id, Lecture.deleted_at.is_(None)).first()
    if not lecture:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The lecture this request belongs to no longer exists")

    old_url = lecture.classroom_url
    req.status = payload.status
    req.reviewed_by = current_user.id
    req.review_note = payload.review_note
    req.reviewed_at = datetime.now(timezone.utc)

    if payload.status == "approved":
        lecture.classroom_url = req.proposed_url
        log_action(db, current_user.id, "classroom_url_edit_approved", "lectures", lecture.id,
                   {"classroom_url": old_url}, {"classroom_url": req.proposed_url})

    requester = db.query(User).filter(User.id == req.requested_by).first()
    if requester:
        if payload.status == "approved":
            message = f"Your classroom link change for '{lecture.title}' was approved and is now live."
        else:
            message = f"Your classroom link change for '{lecture.title}' was rejected."
            if payload.review_note:
                message += f" Note: {payload.review_note}"
        notify(
            db, requester.id, "classroom_url_request_reviewed", message,
            related_entity_type="classroom_edit_requests", related_entity_id=req.id,
            email_to=requester.email, email_subject="Your classroom link change request was reviewed",
        )

    db.commit()
    db.refresh(req)
    return _to_classroom_request_out(db, req)


# ---------------------------------------------------------------------------
# YouTube video edit request review queue (Lectures Sub-Sprint 3)
# ---------------------------------------------------------------------------
def _to_youtube_request_out(db: Session, req: YoutubeEditRequest) -> YoutubeEditRequestOut:
    """Enriches the raw row with everything the Admin/Coordinator queue
    needs to actually decide — lecture title, subject, requester name, and
    the CURRENT live video ID so the UI can show a current-vs-proposed
    preview comparison (Sub-Sprint 6) without a second round-trip."""
    out = YoutubeEditRequestOut.model_validate(req)
    lecture = db.query(Lecture).filter(Lecture.id == req.lecture_id).first()
    if lecture:
        out.lecture_title = lecture.title
        out.current_video_id = lecture.youtube_video_id
        subject = db.query(Subject).filter(Subject.id == lecture.subject_id).first()
        if subject:
            out.subject_name = subject.name
    requester = db.query(User).filter(User.id == req.requested_by).first()
    if requester:
        out.requester_name = requester.full_name
    return out


@youtube_requests_router.get("", response_model=List[YoutubeEditRequestOut])
def list_youtube_requests(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Task 3.1 (GET /api/youtube-requests). status_filter defaults to unset
    (returns every request, any status) — same reasoning as
    list_classroom_requests: this one endpoint also backs a "past
    decisions" history view without needing a second route. The approval
    dashboard (Sub-Sprint 6) is expected to pass ?status_filter=pending
    for the default view.
    """
    query = db.query(YoutubeEditRequest).filter(YoutubeEditRequest.deleted_at.is_(None))
    if status_filter:
        if status_filter not in ("pending", "approved", "rejected"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status_filter")
        query = query.filter(YoutubeEditRequest.status == status_filter)
    requests = query.order_by(YoutubeEditRequest.created_at.desc()).all()
    return [_to_youtube_request_out(db, r) for r in requests]


@youtube_requests_router.patch("/{request_id}/review", response_model=YoutubeEditRequestOut)
def review_youtube_request(
    request_id: uuid.UUID,
    payload: YoutubeRequestReview,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Task 3.2 (PATCH /api/youtube-requests/:id/review). Approve re-parses
    proposed_url and overwrites lectures.youtube_video_id (the lecture was
    already locked when the Teacher's initial upload call succeeded —
    approving an edit doesn't need to re-lock it, it's already true).
    Reject leaves the lecture's video untouched.

    Re-parsing on approve (rather than trusting the value stored at
    submission time) is deliberate: request_youtube_video_edit already
    validated proposed_url before it could ever reach 'pending', so this
    should always succeed — it's a defensive re-check, not the primary
    validation, in case that guarantee is ever weakened later.

    Same "duplicate edits can't bypass" guard as classroom requests: the
    request must currently be 'pending' — reviewing an already-decided
    request 409s instead of silently re-applying.
    """
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be 'approved' or 'rejected'")

    req = db.query(YoutubeEditRequest).filter(
        YoutubeEditRequest.id == request_id, YoutubeEditRequest.deleted_at.is_(None)
    ).first()
    if not req:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if req.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request was already {req.status} — it can't be reviewed again.",
        )

    lecture = db.query(Lecture).filter(Lecture.id == req.lecture_id, Lecture.deleted_at.is_(None)).first()
    if not lecture:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The lecture this request belongs to no longer exists")

    video_id = None
    if payload.status == "approved":
        video_id = parse_youtube_video_id(req.proposed_url)
        if not video_id:
            # Should be unreachable (see docstring) — surfaced as 400 rather
            # than silently approving with a broken video ID.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The proposed URL on this request is no longer valid and can't be applied. Reject it instead.",
            )

    old_video_id = lecture.youtube_video_id
    req.status = payload.status
    req.reviewed_by = current_user.id
    req.review_note = payload.review_note
    req.reviewed_at = datetime.now(timezone.utc)

    if payload.status == "approved":
        lecture.youtube_video_id = video_id
        log_action(db, current_user.id, "youtube_video_edit_approved", "lectures", lecture.id,
                   {"youtube_video_id": old_video_id}, {"youtube_video_id": video_id})

    requester = db.query(User).filter(User.id == req.requested_by).first()
    if requester:
        if payload.status == "approved":
            message = f"Your video change for '{lecture.title}' was approved and is now live."
        else:
            message = f"Your video change for '{lecture.title}' was rejected."
            if payload.review_note:
                message += f" Note: {payload.review_note}"
        notify(
            db, requester.id, "youtube_video_request_reviewed", message,
            related_entity_type="youtube_edit_requests", related_entity_id=req.id,
            email_to=requester.email, email_subject="Your video change request was reviewed",
        )

    db.commit()
    db.refresh(req)
    return _to_youtube_request_out(db, req)
