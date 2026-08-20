import uuid
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.core.security import generate_verification_token, hash_password
from app.core.config import settings
from app.core.audit import log_action
from app.models import (
    User, StudentProfile, TeacherProfile, ParentProfile, ParentStudentLink,
    VerificationToken, CorrectionRequest, StudentLevelEnrollment, Level,
    Subject, Enrollment, Batch, TeacherBoard,
)
from app.schemas.user import (
    UserCreate, UserUpdate, UserOut, UserDetailOut, StudentProfileOut, TeacherProfileOut, ParentProfileOut,
    ParentStudentLinkCreate, ParentStudentLinkOut, ParentChildRegistryOut,
    CorrectionRequestCreate, CorrectionRequestReview, CorrectionRequestOut,
    MyProfileOut, AdminResetPasswordRequest,
)
from app.utils.email import send_email

logger = logging.getLogger("fuse_lms.users")

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(check_license)])


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Onboarding step 1-3: Admin OR Coordinator manually creates a
    Student/Teacher/Coordinator/Parent account (status='pending',
    password_hash=NULL), generates a 24h activation token, and emails it to
    the user (and the linked Parent, for students).

    Creating an Admin account is not possible through this endpoint for
    either role — payload.role is a Literal at the schema level that
    doesn't include "admin" at all, so that case never reaches here.

    Hierarchy rule (explicit decision): Admin assigns Coordinator;
    Coordinator assigns Teacher/Student/Parent; Admin can do all of it.
    A Coordinator creating another Coordinator was previously allowed by
    accident — the Literal excluded "admin" but not "coordinator" — fixed
    below.
    """
    if payload.role == "coordinator" and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Admin can create Coordinator accounts",
        )

    existing = db.query(User).filter(User.email == payload.email, User.deleted_at.is_(None)).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")

    user = User(
        full_name=payload.full_name,
        email=payload.email,
        role=payload.role,
        status="active" if payload.initial_password else "pending",
        phone_number=payload.phone_number,
        created_by=current_user.id,
    )
    if payload.initial_password:
        # Admin/Coordinator-set path: skip the token/email step entirely,
        # account is usable immediately, but flagged so the frontend can
        # force a change-password prompt on first login.
        user.password_hash = hash_password(payload.initial_password)
        user.must_change_password = True
    db.add(user)
    db.flush()

    if payload.role == "student":
        db.add(StudentProfile(
            user_id=user.id,
            roll_number=payload.roll_number,
            admission_date=payload.admission_date,
            father_name=payload.father_name,
            date_of_birth=payload.date_of_birth,
            gender=payload.gender,
            religion=payload.religion,
            nationality=payload.nationality,
            cnic=payload.cnic,
            registration_id=payload.registration_id,
            # UserCreate's model_validator already guarantees board is set
            # when role == "student".
            board=payload.board.value,
        ))
        if payload.parent_id:
            parent = db.query(User).filter(
                User.id == payload.parent_id, User.role == "parent", User.deleted_at.is_(None)
            ).first()
            if not parent:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="parent_id is not a valid parent user")
            db.add(ParentStudentLink(
                parent_id=parent.id, student_id=user.id, relationship_label=payload.relationship_label,
            ))
    elif payload.role == "teacher":
        db.add(TeacherProfile(
            user_id=user.id, designation=payload.designation, hire_date=payload.hire_date,
            gender=payload.gender, cnic=payload.cnic, teacher_code=payload.teacher_code,
        ))
        # UserCreate's model_validator already guarantees at least one
        # board is set when role == "teacher".
        for board in payload.boards:
            db.add(TeacherBoard(teacher_id=user.id, board=board.value))
    elif payload.role == "parent":
        db.add(ParentProfile(
            user_id=user.id, cnic=payload.cnic, registration_id=payload.registration_id,
        ))
    # coordinator: no dedicated profile table — the users row itself
    # (role='coordinator') is the whole record. Parent<->Student linking,
    # when the parent is created first, happens separately via
    # POST /api/users/parent-links.

    if not payload.initial_password:
        token_str = generate_verification_token()
        vt = VerificationToken(
            user_id=user.id,
            token=token_str,
            token_type="activation",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.ACTIVATION_TOKEN_EXPIRE_HOURS),
        )
        db.add(vt)

    log_action(db, current_user.id, "user_created", "users", user.id, None, {"role": payload.role, "email": payload.email})

    db.commit()
    db.refresh(user)

    if not payload.initial_password:
        activation_link = f"{settings.FRONTEND_ORIGIN}/activate?token={token_str}"
        send_email(user.email, "Activate your FUSE LMS account", f"Activate here: {activation_link}")
        if payload.role == "student" and payload.parent_id:
            parent = db.query(User).filter(User.id == payload.parent_id).first()
            if parent:
                send_email(parent.email, "Your child's FUSE LMS account", f"Activation link: {activation_link}")

    return user


@router.get("", response_model=List[UserOut])
def list_users(
    role: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    query = db.query(User).filter(User.deleted_at.is_(None))
    if role:
        query = query.filter(User.role == role)
    return query.order_by(User.created_at.desc()).all()


@router.get("/{user_id}", response_model=UserDetailOut)
def get_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 5.1 hardening: previously only Student/Parent were blocked from
    # viewing someone else's record, which meant Teacher could fetch ANY
    # user by ID one at a time — a real gap against "Teacher cannot view
    # information registry" (list_users already correctly restricts the
    # full list to admin/coordinator; this single-record lookup didn't).
    # Now: view your own record, or be Admin/Coordinator.
    if current_user.id != user_id and current_user.role not in ("admin", "coordinator"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to view this user")
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        # user_id here is always a real users.id (this is what the Registry
        # table's row.id already is — see admin-registry.component.ts's
        # openEditDetailsDialog(user), which passes the same RegistryUser.id
        # straight through). A 404 this specific — not a generic 500 —
        # is what lets the frontend tell "record was deleted/never existed"
        # apart from "something broke while loading it" (handled below).
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    try:
        detail = UserDetailOut.model_validate(user)
        if user.role == "student":
            sp = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
            if not sp:
                logger.error(
                    "Student %s has no student_profiles row — data integrity gap, not a normal empty state.",
                    user.id,
                )
            detail.student_profile = StudentProfileOut.model_validate(sp) if sp else None
        elif user.role == "teacher":
            tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
            if tp:
                profile_out = TeacherProfileOut.model_validate(tp)
                profile_out.boards = [
                    tb.board for tb in
                    db.query(TeacherBoard).filter(TeacherBoard.teacher_id == user.id).order_by(TeacherBoard.board).all()
                ]
                detail.teacher_profile = profile_out
            else:
                # A teacher-role User with no teacher_profiles row (e.g. an
                # account created before the profile insert completed, or a
                # migration gap) isn't a 404 — the User itself is real and
                # the base fields above are still valid to return — but it
                # IS the exact "Edit Teacher" symptom this was reported as:
                # the dialog opens, then the request fails/looks empty with
                # no indication why. Logged here so it shows up server-side
                # instead of only as a silent None the admin can't diagnose.
                logger.error(
                    "Teacher %s has no teacher_profiles row — Edit Details will show blank profile fields.",
                    user.id,
                )
                detail.teacher_profile = None
        elif user.role == "parent":
            pp = db.query(ParentProfile).filter(ParentProfile.user_id == user.id).first()
            detail.parent_profile = ParentProfileOut.model_validate(pp) if pp else None
        return detail
    except ValidationError:
        # A profile row that doesn't satisfy its own schema (e.g. a legacy
        # board value on teacher_boards that predates the current BoardEnum)
        # would otherwise surface as an opaque 500 with no detail — exactly
        # the "schema mismatch" case called out for this endpoint. Log the
        # full trace for diagnosis, but still tell the caller specifically
        # what happened rather than a bare Internal Server Error.
        logger.exception("GET /api/users/%s: profile data failed schema validation.", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="This user's profile data is inconsistent and couldn't be loaded. Contact support.",
        )
    except Exception:
        logger.exception("GET /api/users/%s: unexpected error while loading user details.", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not load this user's details right now. Please try again.",
        )


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    5.3: full_name, status (suspend/reactivate), and role reassignment.
    Registry-details sprint: also writes whichever profile-table fields
    apply to the user's CURRENT role (spec: "Coordinator can edit these
    details" — module 2). Fields for a role that doesn't apply to this user
    are accepted by the schema (kept role-agnostic there) but silently
    ignored here rather than erroring, so the frontend can send one PATCH
    body without needing to know which profile type it's editing.

    Permission mirrors account creation (5.1) — Admin and Coordinator both
    reach this endpoint, neither can touch an Admin account's role or turn
    anyone into one (payload.role is a Literal that excludes "admin"
    entirely), and nobody can change their own role through here.

    Hierarchy rule: same as create_user — only Admin can promote/reassign
    someone TO Coordinator. A Coordinator can still reassign a user AWAY
    from Coordinator (e.g. demoting to Teacher) since that's a step down,
    not an escalation.
    """
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if payload.role == "coordinator" and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Admin can assign the Coordinator role",
        )

    old_value = {"full_name": user.full_name, "status": user.status, "role": user.role}

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.phone_number is not None:
        user.phone_number = payload.phone_number

    if payload.status is not None:
        if payload.status not in ("active", "suspended"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be active or suspended")
        if user.role == "admin":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Admin accounts cannot be suspended through this endpoint")
        user.status = payload.status

    if payload.role is not None:
        if user.role == "admin":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Admin accounts cannot be reassigned through this endpoint")
        if user.id == current_user.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own role")
        if payload.role != user.role:
            user.role = payload.role
            # Backfill the profile row the new role needs, if missing.
            # Doesn't touch/remove any existing profile row from the old
            # role — nothing in this schema is ever hard-deleted on a role
            # switch, consistent with the soft-delete convention elsewhere.
            if payload.role == "student":
                exists = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
                if not exists:
                    db.add(StudentProfile(user_id=user.id, roll_number=None, admission_date=None))
            elif payload.role == "teacher":
                exists = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
                if not exists:
                    db.add(TeacherProfile(user_id=user.id, designation=None, hire_date=None))
            elif payload.role == "parent":
                exists = db.query(ParentProfile).filter(ParentProfile.user_id == user.id).first()
                if not exists:
                    db.add(ParentProfile(user_id=user.id))

    # Registry-detail edits — role read AFTER any reassignment above, so a
    # combined "switch role + fill in the new role's details" PATCH in one
    # call writes to the right table.
    if user.role == "student":
        sp = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
        if sp:
            for field in ("roll_number", "admission_date", "father_name", "date_of_birth",
                          "gender", "religion", "nationality", "cnic", "registration_id"):
                value = getattr(payload, field)
                if value is not None:
                    setattr(sp, field, value)
            if payload.board is not None:
                sp.board = payload.board.value
    elif user.role == "teacher":
        tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
        if tp:
            for field in ("designation", "hire_date", "gender", "cnic", "teacher_code"):
                value = getattr(payload, field)
                if value is not None:
                    setattr(tp, field, value)
            if payload.boards is not None:
                # Full replacement, same convention as subject_ids below —
                # UserUpdate's validator already rejects an empty list, so
                # this only ever runs with >=1 board.
                db.query(TeacherBoard).filter(TeacherBoard.teacher_id == user.id).delete()
                for board in payload.boards:
                    db.add(TeacherBoard(teacher_id=user.id, board=board.value))
    elif user.role == "parent":
        pp = db.query(ParentProfile).filter(ParentProfile.user_id == user.id).first()
        if pp:
            for field in ("cnic", "registration_id"):
                value = getattr(payload, field)
                if value is not None:
                    setattr(pp, field, value)

    # -----------------------------------------------------------------
    # Admin User Management: academic level + subject assignment.
    # Student-only (silently ignored for any other role, same convention as
    # the registry-detail fields above). Both `level_id` and `subject_ids`
    # are validated in full BEFORE anything is written, then applied — since
    # nothing here is committed until the single db.commit() at the end of
    # this function, an invalid request (bad level, or a subject that
    # doesn't belong to the resolved level) raises before any row is
    # touched, and the whole level+subjects change lands atomically together
    # with the rest of this PATCH, or not at all.
    # -----------------------------------------------------------------
    if user.role == "student" and (payload.level_id is not None or payload.subject_ids is not None):
        current_batch = db.query(Batch).filter(
            Batch.is_current.is_(True), Batch.deleted_at.is_(None)
        ).first()
        if payload.subject_ids is not None and not current_batch:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No current batch is configured — cannot assign subjects",
            )

        if payload.level_id is not None:
            new_level = db.query(Level).filter(
                Level.id == payload.level_id, Level.deleted_at.is_(None)
            ).first()
            if not new_level:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Level not found")

        active_level_enrollment = (
            db.query(StudentLevelEnrollment)
            .filter(
                StudentLevelEnrollment.student_id == user.id,
                StudentLevelEnrollment.status == "active",
                StudentLevelEnrollment.deleted_at.is_(None),
            )
            .order_by(StudentLevelEnrollment.started_at.desc())
            .first()
        )
        # The level subjects are validated against: the incoming level_id if
        # one was sent this call, otherwise whatever level is already active.
        effective_level_id = payload.level_id if payload.level_id is not None else (
            active_level_enrollment.level_id if active_level_enrollment else None
        )

        subjects_by_id: dict = {}
        if payload.subject_ids is not None and payload.subject_ids:
            if effective_level_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Assign an academic level before assigning subjects",
                )
            # Cross-Level Subject Enrollment: a subject no longer has to
            # belong to the student's primary level (effective_level_id) —
            # any subject that exists is a valid choice (e.g. an O-Level
            # student taking an A-Level subject). What each enrollment DOES
            # get tagged with is its own subject's level_id, applied below —
            # that's the "explicit level tagging" requirement, kept separate
            # from "is this pick allowed at all" (which is now: any subject).
            subjects_by_id = {
                row.id: row for row in db.query(Subject).filter(
                    Subject.id.in_(payload.subject_ids),
                    Subject.deleted_at.is_(None),
                ).all()
            }
            invalid_subject_ids = set(payload.subject_ids) - subjects_by_id.keys()
            if invalid_subject_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Subject(s) not found: {sorted(str(i) for i in invalid_subject_ids)}",
                )

        # All validation passed — now apply the changes.
        if payload.level_id is not None:
            if not active_level_enrollment or active_level_enrollment.level_id != payload.level_id:
                if active_level_enrollment:
                    active_level_enrollment.status = "completed"
                    active_level_enrollment.completed_at = date.today()
                db.add(StudentLevelEnrollment(
                    student_id=user.id, level_id=payload.level_id,
                    status="active", started_at=date.today(),
                ))

        if payload.subject_ids is not None:
            desired_subject_ids = set(payload.subject_ids)
            existing_enrollments = {
                e.subject_id: e for e in db.query(Enrollment).filter(
                    Enrollment.student_id == user.id,
                    Enrollment.batch_id == current_batch.id,
                    Enrollment.deleted_at.is_(None),
                ).all()
            }

            # Unassign: any active enrollment for a subject no longer selected.
            for subject_id, enrollment in existing_enrollments.items():
                if subject_id not in desired_subject_ids and enrollment.status == "active":
                    enrollment.status = "dropped"

            # Assign: new subjects get a fresh enrollment row; previously
            # dropped ones are reactivated instead of duplicated. level_id
            # is always the SUBJECT's own level (subjects_by_id, populated
            # above from this same payload) — not effective_level_id, the
            # student's primary level — so a cross-level pick is tagged
            # with the curriculum it actually belongs to, not the
            # student's main track.
            for subject_id in desired_subject_ids:
                enrollment = existing_enrollments.get(subject_id)
                if enrollment:
                    if enrollment.status != "active":
                        enrollment.status = "active"
                    # Backfill for a pre-cross-level row that predates this
                    # column (see schema_update_7.sql) — safe no-op otherwise,
                    # since it was already set correctly when first created.
                    if enrollment.level_id is None and subject_id in subjects_by_id:
                        enrollment.level_id = subjects_by_id[subject_id].level_id
                else:
                    db.add(Enrollment(
                        student_id=user.id, subject_id=subject_id, batch_id=current_batch.id,
                        level_id=subjects_by_id[subject_id].level_id,
                    ))

    log_action(db, current_user.id, "user_updated", "users", user.id, old_value,
               {"full_name": user.full_name, "status": user.status, "role": user.role})
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def soft_delete_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.deleted_at = datetime.now(timezone.utc)
    log_action(db, current_user.id, "user_soft_deleted", "users", user.id, None, None)
    db.commit()
    return None


@router.post("/{user_id}/reset-password", status_code=status.HTTP_200_OK)
def reset_user_password(
    user_id: uuid.UUID,
    payload: AdminResetPasswordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Admin/Coordinator sets a new (temporary) password for someone else.
    Always sets must_change_password=True, so the target is prompted to
    pick their own password on next login — the temp password set here
    is meant to be short-lived, not the account's permanent password.

    Two deliberate hardening rules beyond the literal request, both
    mirroring the existing role-hierarchy precedent already established in
    create_user (Admin assigns Coordinator; Coordinator can't):
      1. Can't target your own account — self-service change-password
         exists specifically because it requires proving you know the
         CURRENT password. Allowing self-reset here would let a
         Coordinator/Admin silently rewrite their own password with no
         such proof, from this endpoint alone.
      2. A Coordinator can't reset an Admin's or another Coordinator's
         password — only Admin can. Otherwise a Coordinator account could
         reset a peer or superior's password and log in as them, which is
         a privilege-escalation path this system otherwise takes care to
         close (same reasoning as the Coordinator-can't-create-Coordinator
         rule already in create_user).
    """
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use Change Password (self-service) to update your own password.",
        )

    target = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if target.role in ("admin", "coordinator") and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Admin can reset an Admin's or Coordinator's password",
        )

    target.password_hash = hash_password(payload.new_password)
    target.must_change_password = True
    log_action(db, current_user.id, "password_reset_by_admin", "users", target.id, None,
               {"reset_by_role": current_user.role})
    db.commit()
    return {"detail": f"Password reset for {target.full_name}. They must change it on next login."}


# ---------------------------------------------------------------------------
# Parent <-> Student links
# ---------------------------------------------------------------------------
@router.post("/parent-links", response_model=ParentStudentLinkOut, status_code=status.HTTP_201_CREATED)
def create_parent_link(
    payload: ParentStudentLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    link = ParentStudentLink(**payload.model_dump())
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


@router.get("/{student_id}/parents", response_model=List[ParentStudentLinkOut])
def list_parents_for_student(
    student_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator", "teacher")),
):
    return db.query(ParentStudentLink).filter(
        ParentStudentLink.student_id == student_id, ParentStudentLink.deleted_at.is_(None)
    ).all()


@router.get("/{parent_id}/children", response_model=List[ParentChildRegistryOut])
def list_children_for_parent(
    parent_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Information Registry (spec module 2): "Parent info... child ID and name."
    Reverse direction of list_parents_for_student above. Deliberately
    Admin/Coordinator-only and separate from GET /api/parent/children (which
    is parent-self-scoped, require_roles("parent") only, and returns their
    OWN children) — this is the registry-viewing path for looking up any
    parent's children by ID, not a "my children" endpoint.
    """
    rows = (
        db.query(ParentStudentLink, User, StudentProfile)
        .join(User, User.id == ParentStudentLink.student_id)
        .outerjoin(StudentProfile, StudentProfile.user_id == User.id)
        .filter(
            ParentStudentLink.parent_id == parent_id,
            ParentStudentLink.deleted_at.is_(None),
            User.deleted_at.is_(None),
        )
        .order_by(User.full_name)
        .all()
    )
    return [
        ParentChildRegistryOut(
            student_id=user.id, full_name=user.full_name,
            roll_number=profile.roll_number if profile else None,
            relationship=link.relationship_label,
        )
        for link, user, profile in rows
    ]


# ---------------------------------------------------------------------------
# Correction requests
# ---------------------------------------------------------------------------
@router.post("/{student_id}/correction-requests", response_model=CorrectionRequestOut, status_code=status.HTTP_201_CREATED)
def create_correction_request(
    student_id: uuid.UUID,
    payload: CorrectionRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == "student" and current_user.id != student_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Students may only request corrections for themselves")
    cr = CorrectionRequest(student_id=student_id, requested_changes=payload.requested_changes)
    db.add(cr)
    db.commit()
    db.refresh(cr)
    return cr


@router.get("/correction-requests/pending", response_model=List[CorrectionRequestOut])
def list_pending_corrections(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    return db.query(CorrectionRequest).filter(
        CorrectionRequest.status == "pending", CorrectionRequest.deleted_at.is_(None)
    ).order_by(CorrectionRequest.created_at.asc()).all()


@router.patch("/correction-requests/{correction_id}", response_model=CorrectionRequestOut)
def review_correction_request(
    correction_id: uuid.UUID,
    payload: CorrectionRequestReview,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
):
    cr = db.query(CorrectionRequest).filter(
        CorrectionRequest.id == correction_id, CorrectionRequest.deleted_at.is_(None)
    ).first()
    if not cr:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Correction request not found")
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be approved or rejected")

    cr.status = payload.status
    cr.admin_notes = payload.admin_notes
    cr.reviewed_by = current_user.id
    cr.reviewed_at = datetime.now(timezone.utc)

    if payload.status == "approved":
        student = db.query(User).filter(User.id == cr.student_id).first()
        if student:
            for field, value in cr.requested_changes.items():
                if hasattr(student, field):
                    setattr(student, field, value)

    log_action(db, current_user.id, "correction_reviewed", "correction_requests", cr.id, None,
               {"status": payload.status})
    db.commit()
    db.refresh(cr)
    return cr


# ---------------------------------------------------------------------------
# Self-profile — powers the student dashboard's Profile card. Deliberately
# separate from GET /{user_id} (which is admin-facing and needs a real ID).
# ---------------------------------------------------------------------------
@router.get("/me/profile", response_model=MyProfileOut)
def my_profile(db: Session = Depends(get_db), current_user: User = Depends(require_roles("student"))):
    student_profile = db.query(StudentProfile).filter(
        StudentProfile.user_id == current_user.id
    ).first()

    class_name = None
    active_level_enrollment = (
        db.query(StudentLevelEnrollment)
        .filter(
            StudentLevelEnrollment.student_id == current_user.id,
            StudentLevelEnrollment.status == "active",
            StudentLevelEnrollment.deleted_at.is_(None),
        )
        .order_by(StudentLevelEnrollment.started_at.desc())
        .first()
    )
    if active_level_enrollment:
        level = db.query(Level).filter(Level.id == active_level_enrollment.level_id).first()
        class_name = level.name if level else None

    return MyProfileOut(user=current_user, student_profile=student_profile, class_name=class_name)
