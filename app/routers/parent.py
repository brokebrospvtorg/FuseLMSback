import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_roles, check_license
from app.core.audit import log_action
from app.core.notifications import notify
from app.models import (
    ParentStudentLink, StudentProfile, User, Batch, AttendanceRecord, Grade,
    Mark, Assessment, Subject, Enrollment, TimetableSlot,
    StudentLevelEnrollment, SubjectRequest,
)
from app.schemas.parent import (
    ParentChildOut, ParentChildOverviewOut, ParentSubjectTranscriptOut, ParentMarkEntryOut,
    ParentAttendanceSummaryOut, ParentSubjectAttendanceOut, ParentAttendanceActivityOut,
    ParentTimetableEntryOut, ParentSubjectRequestCreate, ParentSubjectRequestOut,
    ParentAvailableSubjectsOut,
)

router = APIRouter(prefix="/api/parent", tags=["parent"], dependencies=[Depends(check_license)])


def _verify_linked_child(db: Session, parent_id: uuid.UUID, student_id: uuid.UUID) -> None:
    """
    The mandatory security rule from the spec: a Parent can only ever hit
    endpoints for their OWN linked student_id(s). Every endpoint below that
    takes a student_id calls this first — 403, not a filtered/empty result,
    if the link doesn't exist, so probing other students' IDs doesn't even
    leak "this id exists but isn't yours" via a 404 vs 403 distinction.
    """
    link = db.query(ParentStudentLink).filter(
        ParentStudentLink.parent_id == parent_id,
        ParentStudentLink.student_id == student_id,
        ParentStudentLink.deleted_at.is_(None),
    ).first()
    if not link:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This student is not linked to your account.",
        )


@router.get("/children", response_model=List[ParentChildOut])
def list_my_children(db: Session = Depends(get_db),
                      current_user: User = Depends(require_roles("parent"))):
    """Every child linked to the logged-in parent — feeds the Child Switcher widget."""
    rows = (
        db.query(ParentStudentLink, User, StudentProfile)
        .join(User, User.id == ParentStudentLink.student_id)
        .outerjoin(StudentProfile, StudentProfile.user_id == User.id)
        .filter(
            ParentStudentLink.parent_id == current_user.id,
            ParentStudentLink.deleted_at.is_(None),
            User.deleted_at.is_(None),
        )
        .order_by(User.full_name)
        .all()
    )
    return [
        ParentChildOut(
            student_id=user.id,
            full_name=user.full_name,
            roll_number=profile.roll_number if profile else None,
            relationship=link.relationship_label,
        )
        for link, user, profile in rows
    ]


@router.get("/child/{student_id}/overview", response_model=ParentChildOverviewOut)
def child_performance_overview(student_id: uuid.UUID, db: Session = Depends(get_db),
                                current_user: User = Depends(require_roles("parent"))):
    """
    The three metric cards: Current Batch, Overall Attendance %, Aggregate
    Grade. "Current Batch" is the school-wide is_current batch (batches.is_current
    is a single global flag, not per-student — see 001_init_schema.sql), not
    something specific to this child; still meaningful to show since it's
    the term everything else on the card is being measured against.
    """
    _verify_linked_child(db, current_user.id, student_id)

    student = db.query(User).filter(User.id == student_id, User.deleted_at.is_(None)).first()
    if not student:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")

    current_batch = db.query(Batch).filter(Batch.is_current.is_(True), Batch.deleted_at.is_(None)).first()

    # Overall attendance % — same "late counts as attended" convention as
    # the student's own /api/attendance/me/summary, just not split by subject.
    att = (
        db.query(
            func.sum(case((AttendanceRecord.status.in_(["present", "late"]), 1), else_=0)).label("attended"),
            func.count(AttendanceRecord.id).label("total"),
        )
        .filter(AttendanceRecord.user_id == student_id, AttendanceRecord.deleted_at.is_(None))
        .first()
    )
    overall_attendance_pct = round((att.attended / att.total) * 100, 1) if att and att.total else None

    # Aggregate grade — plain average of computed_percentage across every
    # subject with a computed grade. A simple, defensible "aggregate"; not
    # weighted by subject credit-hours since nothing in the schema tracks those.
    avg_pct = (
        db.query(func.avg(Grade.computed_percentage))
        .filter(Grade.student_id == student_id, Grade.deleted_at.is_(None), Grade.computed_percentage.isnot(None))
        .scalar()
    )

    return ParentChildOverviewOut(
        student_id=student.id,
        full_name=student.full_name,
        current_batch_name=current_batch.name if current_batch else None,
        current_batch_year=current_batch.year if current_batch else None,
        overall_attendance_percentage=overall_attendance_pct,
        aggregate_grade_percentage=round(float(avg_pct), 1) if avg_pct is not None else None,
    )


@router.get("/child/{student_id}/report-card", response_model=List[ParentSubjectTranscriptOut])
def child_report_card(student_id: uuid.UUID, db: Session = Depends(get_db),
                       current_user: User = Depends(require_roles("parent"))):
    """
    The Academic Transcript grid: every subject the child has a computed
    grade for, each with its assessment-by-assessment breakdown. Mirrors
    the logic in routers/student_grades.py's /me/grades + /me/marks/{id}
    (same 'only published assessments' privacy rule — a parent shouldn't
    see a mark before the student themselves would), just combined into
    one call and scoped to student_id instead of current_user.id.
    """
    _verify_linked_child(db, current_user.id, student_id)

    grade_rows = (
        db.query(Grade, Subject.name.label("subject_name"))
        .join(Subject, Subject.id == Grade.subject_id)
        .filter(Grade.student_id == student_id, Grade.deleted_at.is_(None))
        .all()
    )

    result = []
    for grade, subject_name in grade_rows:
        mark_rows = (
            db.query(Mark, Assessment.name.label("assessment_name"), Assessment.max_marks)
            .join(Assessment, Assessment.id == Mark.assessment_id)
            .filter(
                Mark.student_id == student_id,
                Assessment.subject_id == grade.subject_id,
                Assessment.status == "published",
                Mark.deleted_at.is_(None),
                Assessment.deleted_at.is_(None),
            )
            .all()
        )
        result.append(ParentSubjectTranscriptOut(
            subject_id=grade.subject_id,
            subject_name=subject_name,
            computed_percentage=float(grade.computed_percentage) if grade.computed_percentage is not None else None,
            letter_grade=grade.letter_grade,
            is_overridden=grade.is_overridden,
            assessments=[
                ParentMarkEntryOut(
                    assessment_id=mark.assessment_id,
                    assessment_name=assessment_name,
                    max_marks=float(max_marks),
                    marks_obtained=float(mark.marks_obtained),
                )
                for mark, assessment_name, max_marks in mark_rows
            ],
        ))
    return result


@router.get("/child/{student_id}/attendance-summary", response_model=ParentAttendanceSummaryOut)
def child_attendance_summary(student_id: uuid.UUID, db: Session = Depends(get_db),
                              current_user: User = Depends(require_roles("parent"))):
    """
    Feeds the Attendance View: overall gauge, per-subject breakdown, and a
    recent activity log (last 15 records, most recent first). Same "late
    counts as attended" convention used everywhere else in the app
    (routers/attendance.py's /me/summary, and child_performance_overview above).
    """
    _verify_linked_child(db, current_user.id, student_id)

    rows = (
        db.query(AttendanceRecord, Subject.name.label("subject_name"))
        .join(Subject, Subject.id == AttendanceRecord.subject_id)
        .filter(AttendanceRecord.user_id == student_id, AttendanceRecord.deleted_at.is_(None))
        .order_by(AttendanceRecord.date.desc())
        .all()
    )

    by_subject: dict = {}
    overall = {"present": 0, "absent": 0, "late": 0, "excused": 0}
    recent_activity: List[ParentAttendanceActivityOut] = []

    for rec, subject_name in rows:
        bucket = by_subject.setdefault(rec.subject_id, {
            "subject_name": subject_name, "present": 0, "absent": 0, "late": 0, "excused": 0,
        })
        if rec.status in overall:
            bucket[rec.status] += 1
            overall[rec.status] += 1

        if len(recent_activity) < 15:
            recent_activity.append(ParentAttendanceActivityOut(
                date=rec.date, subject_name=subject_name, status=rec.status,
            ))

    by_subject_out = []
    for subject_id, counts in by_subject.items():
        total = counts["present"] + counts["absent"] + counts["late"] + counts["excused"]
        attended = counts["present"] + counts["late"] + counts["excused"]
        pct = round((attended / total) * 100, 1) if total else 0.0
        by_subject_out.append(ParentSubjectAttendanceOut(
            subject_id=subject_id, subject_name=counts["subject_name"],
            present_count=counts["present"], absent_count=counts["absent"],
            late_count=counts["late"], excused_count=counts["excused"],
            total_periods=total, attendance_percentage=pct,
        ))

    overall_total = overall["present"] + overall["absent"] + overall["late"] + overall["excused"]
    overall_attended = overall["present"] + overall["late"] + overall["excused"]
    overall_pct = round((overall_attended / overall_total) * 100, 1) if overall_total else None

    return ParentAttendanceSummaryOut(
        student_id=student_id,
        overall_present_count=overall["present"], overall_absent_count=overall["absent"],
        overall_late_count=overall["late"], overall_excused_count=overall["excused"],
        overall_total_periods=overall_total, overall_attendance_percentage=overall_pct,
        by_subject=by_subject_out, recent_activity=recent_activity,
    )


@router.get("/child/{student_id}/timetable", response_model=List[ParentTimetableEntryOut])
def child_timetable(student_id: uuid.UUID, db: Session = Depends(get_db),
                     current_user: User = Depends(require_roles("parent"))):
    """
    Feeds the Timetable View's weekly grid. Scoped the same way as a
    student's own GET /api/academic/timetable/me: active-enrollment subject
    ids joined to timetable_slots, just keyed off student_id instead of
    current_user.id.
    """
    _verify_linked_child(db, current_user.id, student_id)

    subject_ids = [row.subject_id for row in db.query(Enrollment.subject_id).filter(
        Enrollment.student_id == student_id, Enrollment.status == "active",
        Enrollment.deleted_at.is_(None),
    ).all()]
    if not subject_ids:
        return []

    rows = (
        db.query(TimetableSlot, Subject.name.label("subject_name"), User.full_name.label("teacher_name"))
        .join(Subject, Subject.id == TimetableSlot.subject_id)
        .join(User, User.id == TimetableSlot.teacher_id)
        .filter(TimetableSlot.subject_id.in_(subject_ids), TimetableSlot.deleted_at.is_(None))
        .order_by(TimetableSlot.day_of_week, TimetableSlot.period_number)
        .all()
    )

    return [
        ParentTimetableEntryOut(
            id=slot.id, subject_id=slot.subject_id, subject_name=subject_name,
            teacher_name=teacher_name, day_of_week=slot.day_of_week,
            period_number=slot.period_number,
            start_time=slot.start_time.strftime("%H:%M"), end_time=slot.end_time.strftime("%H:%M"),
        )
        for slot, subject_name, teacher_name in rows
    ]


@router.get("/child/{student_id}/available-subjects", response_model=ParentAvailableSubjectsOut)
def child_available_subjects(student_id: uuid.UUID, db: Session = Depends(get_db),
                              current_user: User = Depends(require_roles("parent"))):
    """
    Subjects the Parent can request for this child: in one of the child's
    active Levels (student_level_enrollments.status = 'active'), for the
    current batch, and not already enrolled or already requested (pending
    or approved) for that same batch — otherwise duplicate requests would
    pile up every time this list is opened.

    Response includes the batch_id it was computed against — the request
    form submits that value back, rather than the frontend separately
    determining "current batch" and risking it drifting out of sync with
    what this endpoint actually filtered on.
    """
    _verify_linked_child(db, current_user.id, student_id)

    current_batch = db.query(Batch).filter(Batch.is_current.is_(True), Batch.deleted_at.is_(None)).first()
    if not current_batch:
        return ParentAvailableSubjectsOut(batch_id=None, batch_name=None, subjects=[])

    active_level_ids = [
        row.level_id for row in db.query(StudentLevelEnrollment.level_id).filter(
            StudentLevelEnrollment.student_id == student_id,
            StudentLevelEnrollment.status == "active",
            StudentLevelEnrollment.deleted_at.is_(None),
        ).all()
    ]
    if not active_level_ids:
        return ParentAvailableSubjectsOut(batch_id=current_batch.id, batch_name=current_batch.name, subjects=[])

    already_enrolled_subject_ids = {
        row.subject_id for row in db.query(Enrollment.subject_id).filter(
            Enrollment.student_id == student_id,
            Enrollment.batch_id == current_batch.id,
            Enrollment.deleted_at.is_(None),
        ).all()
    }
    already_requested_subject_ids = {
        row.subject_id for row in db.query(SubjectRequest.subject_id).filter(
            SubjectRequest.student_id == student_id,
            SubjectRequest.batch_id == current_batch.id,
            SubjectRequest.status.in_(("requested", "approved")),
            SubjectRequest.deleted_at.is_(None),
        ).all()
    }
    exclude_ids = already_enrolled_subject_ids | already_requested_subject_ids

    query = db.query(Subject).filter(
        Subject.level_id.in_(active_level_ids), Subject.deleted_at.is_(None),
    )
    if exclude_ids:
        query = query.filter(Subject.id.notin_(exclude_ids))
    subjects = query.order_by(Subject.name).all()

    return ParentAvailableSubjectsOut(batch_id=current_batch.id, batch_name=current_batch.name, subjects=subjects)


@router.get("/child/{student_id}/subject-requests", response_model=List[ParentSubjectRequestOut])
def child_subject_requests(student_id: uuid.UUID, db: Session = Depends(get_db),
                            current_user: User = Depends(require_roles("parent"))):
    """Request history table — every request ever made for this child, any status."""
    _verify_linked_child(db, current_user.id, student_id)

    rows = (
        db.query(SubjectRequest, Subject.name.label("subject_name"), Batch.name.label("batch_name"))
        .join(Subject, Subject.id == SubjectRequest.subject_id)
        .join(Batch, Batch.id == SubjectRequest.batch_id)
        .filter(SubjectRequest.student_id == student_id, SubjectRequest.deleted_at.is_(None))
        .order_by(SubjectRequest.requested_at.desc())
        .all()
    )
    return [
        ParentSubjectRequestOut(
            id=req.id, subject_id=req.subject_id, subject_name=subject_name,
            batch_id=req.batch_id, batch_name=batch_name, status=req.status,
            requested_at=req.requested_at, actioned_at=req.actioned_at,
        )
        for req, subject_name, batch_name in rows
    ]


@router.post("/child/{student_id}/subject-requests", response_model=ParentSubjectRequestOut,
             status_code=status.HTTP_201_CREATED)
def create_child_subject_request(student_id: uuid.UUID, payload: ParentSubjectRequestCreate,
                                  db: Session = Depends(get_db),
                                  current_user: User = Depends(require_roles("parent"))):
    """
    Parent-initiated subject request on behalf of a linked child. Deliberately
    a separate endpoint from POST /api/academic/subject-requests (that one is
    hard-locked to require_roles("student") and always sets
    student_id=current_user.id — a Parent has no "own" student_id to submit
    as, so extending that endpoint's role check would still need this same
    student_id-in-payload + link-verification logic bolted on. Cleaner as
    its own endpoint, same creation/notification behavior underneath.
    """
    _verify_linked_child(db, current_user.id, student_id)

    subject = db.query(Subject).filter(Subject.id == payload.subject_id, Subject.deleted_at.is_(None)).first()
    if not subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")
    batch = db.query(Batch).filter(Batch.id == payload.batch_id, Batch.deleted_at.is_(None)).first()
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    req = SubjectRequest(student_id=student_id, subject_id=payload.subject_id, batch_id=payload.batch_id)
    db.add(req)
    db.flush()

    student = db.query(User).filter(User.id == student_id).first()
    student_name = student.full_name if student else "A student"
    message = f"{current_user.full_name} requested {subject.name} for {student_name}."
    if payload.reason:
        message += f" Reason: {payload.reason}"

    reviewers = db.query(User).filter(
        User.role.in_(("admin", "coordinator")), User.deleted_at.is_(None), User.status == "active",
    ).all()
    for reviewer in reviewers:
        notify(
            db, reviewer.id, "subject_request_submitted", message,
            related_entity_type="subject_requests", related_entity_id=req.id,
        )

    db.commit()
    db.refresh(req)
    return ParentSubjectRequestOut(
        id=req.id, subject_id=subject.id, subject_name=subject.name,
        batch_id=batch.id, batch_name=batch.name, status=req.status,
        requested_at=req.requested_at, actioned_at=req.actioned_at,
    )
