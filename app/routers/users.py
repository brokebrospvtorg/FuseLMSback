import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_roles, check_license
from app.core.security import generate_verification_token
from app.core.config import settings
from app.core.audit import log_action
from app.models import (
    User, StudentProfile, TeacherProfile, ParentProfile, ParentStudentLink,
    VerificationToken, CorrectionRequest, StudentLevelEnrollment, Level,
)
from app.schemas.user import (
    UserCreate, UserUpdate, UserOut, UserDetailOut, StudentProfileOut, TeacherProfileOut, ParentProfileOut,
    ParentStudentLinkCreate, ParentStudentLinkOut, ParentChildRegistryOut,
    CorrectionRequestCreate, CorrectionRequestReview, CorrectionRequestOut,
    MyProfileOut,
)
from app.utils.email import send_email

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
        status="pending",
        phone_number=payload.phone_number,
        created_by=current_user.id,
    )
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
    elif payload.role == "parent":
        db.add(ParentProfile(
            user_id=user.id, cnic=payload.cnic, registration_id=payload.registration_id,
        ))
    # coordinator: no dedicated profile table — the users row itself
    # (role='coordinator') is the whole record. Parent<->Student linking,
    # when the parent is created first, happens separately via
    # POST /api/users/parent-links.

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    detail = UserDetailOut.model_validate(user)
    if user.role == "student":
        sp = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
        detail.student_profile = StudentProfileOut.model_validate(sp) if sp else None
    elif user.role == "teacher":
        tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
        detail.teacher_profile = TeacherProfileOut.model_validate(tp) if tp else None
    elif user.role == "parent":
        pp = db.query(ParentProfile).filter(ParentProfile.user_id == user.id).first()
        detail.parent_profile = ParentProfileOut.model_validate(pp) if pp else None
    return detail


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
    elif user.role == "teacher":
        tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
        if tp:
            for field in ("designation", "hire_date", "gender", "cnic", "teacher_code"):
                value = getattr(payload, field)
                if value is not None:
                    setattr(tp, field, value)
    elif user.role == "parent":
        pp = db.query(ParentProfile).filter(ParentProfile.user_id == user.id).first()
        if pp:
            for field in ("cnic", "registration_id"):
                value = getattr(payload, field)
                if value is not None:
                    setattr(pp, field, value)

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
