import re
import uuid
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
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
    Subject, Enrollment, Batch, TeacherBoard, TeacherLevel,
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

# ---------------------------------------------------------------------------
# Admin Teacher Creation — auto Teacher Code + default credentials
# ---------------------------------------------------------------------------
# Every Teacher account created through POST /api/users (Admin or
# Coordinator) now gets:
#   1. A server-generated Teacher Code (INK-T-XXXX) — never a client-
#      supplied value; UserCreate no longer even has a teacher_code field.
#   2. A fixed starting password + must_change_password=True, immediately
#      active — same "skip the activation email" branch the general
#      initial_password mechanism already provides, just forced for every
#      Teacher rather than left to whatever the caller sent.
# Both are deliberately hardcoded constants, not settings.* — they're a
# fixed onboarding convention (spec-mandated format/value), not
# environment-specific configuration.
DEFAULT_TEACHER_INITIAL_PASSWORD = "Inkling@2026"
_TEACHER_CODE_PREFIX = "INK-T-"
_TEACHER_CODE_RE = re.compile(rf"^{_TEACHER_CODE_PREFIX}(\d{{4,}})$")
_TEACHER_CODE_MAX_ATTEMPTS = 5


def _next_teacher_code(db: Session) -> str:
    """
    Next unused Teacher Code in the INK-T-XXXX format (4-digit,
    zero-padded, monotonically increasing).

    Reads every existing (non-null) teacher_profiles.teacher_code, ignores
    any that don't match the INK-T-#### pattern (legacy/manually-entered
    values from before manual input was disallowed — not treated as an
    error, just not part of the sequence), and returns one past the
    highest matching suffix found.

    This is a plain SELECT + compute, not a DB sequence, so it's only
    collision-free under normal single-request usage — see the retry loop
    around its call site in create_user, which re-derives a fresh code and
    retries (against the real safety net: the partial unique index
    idx_teacher_profiles_teacher_code) if two Add Teacher submissions ever
    race each other for the same number.
    """
    existing_codes = (
        db.query(TeacherProfile.teacher_code)
        .filter(TeacherProfile.teacher_code.isnot(None))
        .all()
    )
    max_seq = 0
    for (code,) in existing_codes:
        match = _TEACHER_CODE_RE.match(code or "")
        if match:
            max_seq = max(max_seq, int(match.group(1)))
    return f"{_TEACHER_CODE_PREFIX}{max_seq + 1:04d}"


# ---------------------------------------------------------------------------
# Admin Student Creation — auto Roll Number + default credentials
# ---------------------------------------------------------------------------
# Every Student account created through POST /api/users (Admin or
# Coordinator) now gets, mirroring Admin Teacher Creation exactly:
#   1. A server-generated Roll Number (INK-{year}-XXXX) — never a client-
#      supplied value; UserCreate no longer even has a roll_number field.
#      The year segment is the current calendar year at creation time; the
#      4-digit sequence resets per year (INK-2026-0001, INK-2026-0002, ...
#      then INK-2027-0001 once the year rolls over) rather than climbing
#      forever, since a fresh admission cycle every year is the expected
#      shape for a roll number.
#   2. A fixed starting password + must_change_password=True, immediately
#      active — same forced-default branch as Admin Teacher Creation.
DEFAULT_STUDENT_INITIAL_PASSWORD = "Inkling@2026"
_ROLL_NUMBER_PREFIX = "INK-"
_STUDENT_CODE_MAX_ATTEMPTS = 5

# Parent Link Flow — auto Parent Reg ID, same server-generated convention.
_PARENT_REG_ID_PREFIX = "INK-P-"
_PARENT_REG_ID_RE = re.compile(rf"^{_PARENT_REG_ID_PREFIX}(\d{{4,}})$")


def _next_roll_number(db: Session, year: int) -> str:
    """
    Next unused Roll Number in the INK-{year}-XXXX format (4-digit,
    zero-padded, monotonically increasing WITHIN that year).

    Same shape as _next_teacher_code above: reads every existing
    (non-null) student_profiles.roll_number, keeps only the ones matching
    THIS year's INK-{year}-#### pattern (a prior year's rows, or any
    legacy/manually-entered value that predates this convention, are
    ignored rather than erroring — not part of this year's sequence), and
    returns one past the highest matching suffix found for that year.

    Plain SELECT + compute, not a DB sequence — collision-free only under
    normal single-request usage; see the retry loop around its call site
    in create_user, which re-derives a fresh number and retries (against
    the real safety net: the unique constraint on student_profiles.roll_number)
    if two Add Student submissions ever race each other for the same number.
    """
    year_re = re.compile(rf"^{_ROLL_NUMBER_PREFIX}{year}-(\d{{4,}})$")
    existing_numbers = (
        db.query(StudentProfile.roll_number)
        .filter(StudentProfile.roll_number.isnot(None))
        .all()
    )
    max_seq = 0
    for (roll_number,) in existing_numbers:
        match = year_re.match(roll_number or "")
        if match:
            max_seq = max(max_seq, int(match.group(1)))
    return f"{_ROLL_NUMBER_PREFIX}{year}-{max_seq + 1:04d}"


def _next_parent_reg_id(db: Session) -> str:
    """
    Next unused Parent Registration ID in the INK-P-XXXX format — same
    shape/convention as _next_teacher_code and _next_roll_number, scoped to
    parent_profiles.registration_id instead. Not year-scoped (a Parent
    account isn't tied to an admission cycle the way a Student's roll
    number is), so this climbs monotonically forever, same as Teacher Code.
    """
    existing_ids = (
        db.query(ParentProfile.registration_id)
        .filter(ParentProfile.registration_id.isnot(None))
        .all()
    )
    max_seq = 0
    for (reg_id,) in existing_ids:
        match = _PARENT_REG_ID_RE.match(reg_id or "")
        if match:
            max_seq = max(max_seq, int(match.group(1)))
    return f"{_PARENT_REG_ID_PREFIX}{max_seq + 1:04d}"


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

    Admin Teacher Creation (role == "teacher" only): the account is always
    created with the fixed default password (DEFAULT_TEACHER_INITIAL_PASSWORD)
    and must_change_password=True, regardless of whatever payload.initial_password
    did or didn't carry — same immediately-active branch as the general
    mechanism below, just non-optional for this role. The Teacher Code
    written to teacher_profiles is always server-generated (_next_teacher_code)
    — see UserCreate, which no longer accepts teacher_code as input at all.

    Admin Student Creation (role == "student" only): same forced-default
    shape as Teacher — DEFAULT_STUDENT_INITIAL_PASSWORD, must_change_password=True,
    regardless of payload.initial_password. The Roll Number written to
    student_profiles is always server-generated in the INK-{year}-XXXX
    format (_next_roll_number) — UserCreate no longer accepts roll_number
    as input at all. The Admin additionally chooses, per payload.parent_link_mode,
    whether to link an existing Parent now ("existing", parent_id required)
    or defer it ("later"/omitted — Link Parent from the Registry row action
    covers it afterwards), and may optionally set an initial Batch -> Level
    -> Subject enrollment (Cascading Scope) — see the student branch below.
    """
    if payload.role == "coordinator" and current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Admin can create Coordinator accounts",
        )

    existing = db.query(User).filter(User.email == payload.email, User.deleted_at.is_(None)).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")

    # Cascading Scope: validate the Batch/Level/Subjects exist BEFORE any
    # row is written — same "validate in full, then apply" pattern as
    # update_user's level/subject block — so a bad batch_id/level_id/
    # subject_id fails cleanly (404/400) instead of leaving a half-created
    # account behind. Only meaningful for role == "student"; UserCreate's
    # own validator already guarantees subject_ids implies batch_id + level_id.
    cascade_batch = None
    cascade_level = None
    cascade_subjects_by_id: dict = {}
    if payload.role == "student" and payload.batch_id is not None:
        cascade_batch = db.query(Batch).filter(
            Batch.id == payload.batch_id, Batch.deleted_at.is_(None)
        ).first()
        if not cascade_batch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    if payload.role == "student" and payload.level_id is not None:
        cascade_level = db.query(Level).filter(
            Level.id == payload.level_id, Level.deleted_at.is_(None)
        ).first()
        if not cascade_level:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Level not found")
    if payload.role == "student" and payload.subject_ids:
        cascade_subjects_by_id = {
            row.id: row for row in db.query(Subject).filter(
                Subject.id.in_(payload.subject_ids),
                Subject.deleted_at.is_(None),
            ).all()
        }
        invalid_subject_ids = set(payload.subject_ids) - cascade_subjects_by_id.keys()
        if invalid_subject_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Subject(s) not found: {sorted(str(i) for i in invalid_subject_ids)}",
            )

    # Parent Link Flow: "existing" requires the referenced parent to be a
    # real, non-deleted Parent user — validated up front for the same
    # "fail before writing anything" reason as the cascade above. UserCreate's
    # validator already guarantees parent_id is set when parent_link_mode
    # == "existing"; parent_link_mode == "later" (or omitted) never looks
    # at parent_id here even if one happened to be sent.
    link_parent_now = payload.role == "student" and payload.parent_link_mode == "existing"
    cascade_parent = None
    if link_parent_now:
        cascade_parent = db.query(User).filter(
            User.id == payload.parent_id, User.role == "parent", User.deleted_at.is_(None)
        ).first()
        if not cascade_parent:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="parent_id is not a valid parent user")

    # Admin Teacher/Student Creation, point 3: a Teacher's or Student's
    # initial password is never the caller's choice — always the fixed
    # default, always immediately active. Every other role keeps today's
    # behaviour exactly (payload.initial_password when the caller set one,
    # else the pending + activation-email path further below).
    if payload.role == "teacher":
        effective_initial_password = DEFAULT_TEACHER_INITIAL_PASSWORD
    elif payload.role == "student":
        effective_initial_password = DEFAULT_STUDENT_INITIAL_PASSWORD
    else:
        effective_initial_password = payload.initial_password

    user = User(
        full_name=payload.full_name,
        email=payload.email,
        role=payload.role,
        status="pending",  # ALWAYS pending now — see requirement 2
        phone_number=payload.phone_number,
        created_by=current_user.id,
    )
    if effective_initial_password:
        # Admin/Coordinator-set path (or the forced Teacher default above):
        # skip the token/email step entirely, account is usable
        # immediately, but flagged so the frontend can force a
        # change-password prompt on first login.
        user.password_hash = hash_password(effective_initial_password)
        user.must_change_password = True
    db.add(user)
    db.flush()

    if payload.role == "student":
        # Auto Roll Number: same retry-against-the-unique-constraint shape
        # as Admin Teacher Creation's Teacher Code loop — each attempt runs
        # inside its own SAVEPOINT so a failed attempt only unwinds the
        # StudentProfile insert, not the User row already flushed above.
        current_year = date.today().year
        for attempt in range(_STUDENT_CODE_MAX_ATTEMPTS):
            roll_number = _next_roll_number(db, current_year)
            savepoint = db.begin_nested()
            try:
                db.add(StudentProfile(
                    user_id=user.id,
                    roll_number=roll_number,
                    admission_date=payload.admission_date,
                    father_name=payload.father_name,
                    date_of_birth=payload.date_of_birth,
                    gender=payload.gender,
                    religion=payload.religion,
                    nationality=payload.nationality,
                    cnic=payload.cnic,
                    registration_id=payload.registration_id,
                    # UserCreate's model_validator already guarantees board
                    # is set when role == "student".
                    board=payload.board.value,
                ))
                db.flush()
                savepoint.commit()
                break
            except IntegrityError:
                savepoint.rollback()
                if attempt == _STUDENT_CODE_MAX_ATTEMPTS - 1:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Could not generate a unique Roll Number — please retry.",
                    )

        # Parent Link Flow: only "existing" creates a link now; "later"
        # (or parent_link_mode omitted) deliberately creates none — Link
        # Parent from the Registry row action is the follow-up path.
        if link_parent_now:
            db.add(ParentStudentLink(
                parent_id=cascade_parent.id, student_id=user.id, relationship_label=payload.relationship_label,
            ))

        # Cascading Scope: Batch -> Level -> Subject initial enrollment,
        # same shape as update_user's level/subject block — Level first
        # (StudentLevelEnrollment), then Subjects (Enrollment, one per
        # subject, tagged with that SUBJECT's own level_id so a cross-level
        # pick is recorded against the curriculum it actually belongs to,
        # not necessarily payload.level_id). Every piece here was already
        # validated to exist further up, before the User row was even
        # created — this is purely the "apply" half.
        if cascade_level is not None:
            db.add(StudentLevelEnrollment(
                student_id=user.id, level_id=cascade_level.id,
                status="active", started_at=date.today(),
            ))
        if payload.subject_ids and cascade_batch is not None:
            for subject_id in payload.subject_ids:
                db.add(Enrollment(
                    student_id=user.id, subject_id=subject_id, batch_id=cascade_batch.id,
                    level_id=cascade_subjects_by_id[subject_id].level_id,
                ))
    elif payload.role == "teacher":
        # Admin Teacher Creation, point 1: Teacher Code is always
        # server-generated — nothing on payload to read. Small retry loop
        # against the partial unique index idx_teacher_profiles_teacher_code
        # (schema_update.sql) so two concurrent Add Teacher submissions
        # that happened to compute the same next-in-sequence code still
        # each end up with a distinct one instead of one of them 500ing.
        # Each attempt runs inside its own SAVEPOINT (db.begin_nested())
        # so a failed attempt only unwinds the TeacherProfile insert, not
        # the User row already flushed above.
        for attempt in range(_TEACHER_CODE_MAX_ATTEMPTS):
            teacher_code = _next_teacher_code(db)
            savepoint = db.begin_nested()
            try:
                db.add(TeacherProfile(
                    user_id=user.id, hire_date=payload.hire_date,
                    gender=payload.gender, cnic=payload.cnic, teacher_code=teacher_code,
                ))
                db.flush()
                savepoint.commit()
                break
            except IntegrityError:
                savepoint.rollback()
                if attempt == _TEACHER_CODE_MAX_ATTEMPTS - 1:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Could not generate a unique Teacher Code — please retry.",
                    )
        # UserCreate's model_validator already guarantees at least one
        # board is set when role == "teacher".
        for board in payload.boards:
            db.add(TeacherBoard(teacher_id=user.id, board=board.value))
        # UserCreate's model_validator already guarantees at least one
        # level is set when role == "teacher".
        for level_id in payload.level_ids:
            db.add(TeacherLevel(teacher_id=user.id, level_id=level_id))
    elif payload.role == "parent":
        # Parent Link Flow: Parent Reg ID is always server-generated
        # (INK-P-XXXX), same retry-against-the-unique-constraint shape as
        # Roll Number / Teacher Code above — payload.registration_id (if
        # the caller sent one anyway) is intentionally ignored here, not
        # merged in, so there's exactly one source of truth for this ID.
        for attempt in range(_STUDENT_CODE_MAX_ATTEMPTS):
            parent_reg_id = _next_parent_reg_id(db)
            savepoint = db.begin_nested()
            try:
                db.add(ParentProfile(
                    user_id=user.id, cnic=payload.cnic, registration_id=parent_reg_id,
                ))
                db.flush()
                savepoint.commit()
                break
            except IntegrityError:
                savepoint.rollback()
                if attempt == _STUDENT_CODE_MAX_ATTEMPTS - 1:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Could not generate a unique Parent Reg ID — please retry.",
                    )
    # coordinator: no dedicated profile table — the users row itself
    # (role='coordinator') is the whole record. Parent<->Student linking,
    # when the parent is created first, happens separately via
    # POST /api/users/parent-links.

    if not effective_initial_password:
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

    if not effective_initial_password:
        activation_link = f"{settings.FRONTEND_ORIGIN}/activate?token={token_str}"
        send_email(user.email, "Activate your FUSE LMS account", f"Activate here: {activation_link}")
        if link_parent_now and cascade_parent:
            send_email(cascade_parent.email, "Your child's FUSE LMS account", f"Activation link: {activation_link}")

    return user


@router.get("", response_model=List[UserOut])
def list_users(
    role: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    query = db.query(User).filter(User.deleted_at.is_(None))
    if role == "teacher":
        # Dual-role support: nothing in this schema is hard-deleted on a
        # role switch (see update_user's role-reassignment comment above) —
        # a user who was a Teacher and got promoted to Coordinator keeps
        # their teacher_profiles row. That coordinator can still be
        # legitimately picked as a Teacher on a Batch/Subject/Timetable
        # Slot (Coordinator Portal's Teacher Assignee dropdown resolves
        # names for existing TeacherSubjectAssignment rows against exactly
        # this list — see coordinator-timetable's loadTeacherAssigneesFor,
        # which was silently dropping any assignee not present here), so
        # ?role=teacher includes them even though User.role itself now
        # reads 'coordinator'. A coordinator with no teacher_profiles row
        # was never a Teacher and is correctly excluded.
        query = query.filter(
            or_(
                User.role == "teacher",
                and_(
                    User.role == "coordinator",
                    User.id.in_(db.query(TeacherProfile.user_id)),
                ),
            )
        )
        # Dropdown-only ordering — alphabetical is what a picker needs,
        # unlike the registry's default recency order below.
        return query.order_by(User.full_name).all()
    if role:
        query = query.filter(User.role == role)
    return query.order_by(User.created_at.desc()).all()

@router.get("/{user_id}", response_model=UserDetailOut)
def get_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.id != user_id and current_user.role not in ("admin", "coordinator"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to view this user")
    user = db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()
    if not user:
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
                # Ensure we extract the string/enum value from TeacherBoard objects or use direct string values
                raw_boards = db.query(TeacherBoard).filter(TeacherBoard.teacher_id == user.id).all()
                boards_list = []
                for tb in raw_boards:
                    val = tb.board.value if hasattr(tb.board, "value") else tb.board
                    boards_list.append(val)

                levels_list = [
                    tl.level_id for tl in
                    db.query(TeacherLevel).filter(TeacherLevel.teacher_id == user.id).all()
                ]
                
                detail.teacher_profile = TeacherProfileOut(
                    user_id=tp.user_id,
                    hire_date=tp.hire_date,
                    gender=tp.gender,
                    cnic=tp.cnic,
                    teacher_code=tp.teacher_code,
                    boards=boards_list,
                    level_ids=levels_list
                )
                
            else:
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

    Suspension hierarchy (status field): Admin accounts can never be
    suspended through this endpoint, by anyone, Admin included — that's a
    direct-database action ("Super Admin" tier in this schema). Below
    that, a Coordinator cannot suspend/reactivate another Coordinator
    account (peers, not subordinates) — only Admin can. A Coordinator can
    still suspend/reactivate Teacher, Student, and Parent accounts.
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
        # Suspension hierarchy (Registry Security & Suspension Rules):
        # Admin is the top ("Super Admin") tier in this schema — nobody,
        # not even another Admin, can suspend/reactivate an Admin account
        # through this endpoint; that stays a direct-database action.
        if user.role == "admin":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Admin accounts cannot be suspended through this endpoint")
        # A Coordinator can suspend/reactivate Teacher, Student, and Parent
        # accounts, but NOT another Coordinator account — that's a peer,
        # not a subordinate, and suspending peers is reserved for Admin
        # ("Super Admin"). Only checked against current_user.role, so this
        # never restricts an Admin acting on a Coordinator.
        if user.role == "coordinator" and current_user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Admin can suspend or reactivate a Coordinator account",
            )
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
                    db.add(TeacherProfile(user_id=user.id, hire_date=None))
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
            # Read-Only Reg ID & Roll Number: roll_number and registration_id
            # are deliberately excluded from this loop — both are
            # server-generated at account creation (_next_roll_number /
            # the caller-supplied exam-board id captured once, at
            # creation-time only) and are no longer writable through the
            # Edit Details PATCH, matching the frontend's [readonly]
            # treatment of both fields. UserUpdate still accepts them on
            # the wire (so an older, not-yet-updated client doesn't 422)
            # but any value sent for either is now silently ignored here rather
            # than applied — the field-level enforcement, not just the UI,
            # is what actually prevents a bypass via direct API calls.
            for field in ("admission_date", "father_name", "date_of_birth",
                          "gender", "religion", "nationality", "cnic"):
                value = getattr(payload, field)
                if value is not None:
                    setattr(sp, field, value)
            if payload.board is not None:
                sp.board = payload.board.value
    elif user.role == "teacher":
        tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
        if tp:
            for field in ("hire_date", "gender", "cnic", "teacher_code"):
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
            # ADD: level_ids full-replacement, same pattern as boards above.
            if payload.level_ids is not None:
                db.query(TeacherLevel).filter(TeacherLevel.teacher_id == user.id).delete()
                for level_id in payload.level_ids:
                    db.add(TeacherLevel(teacher_id=user.id, level_id=level_id))
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
        # Batch -> Level -> Subject cascade (Registry Cascading Dropdowns):
        # the Batch that subject_ids resolves against is now whichever one
        # the caller explicitly picked (payload.batch_id) — falling back
        # to the globally is_current batch only when the caller didn't
        # send one at all (e.g. an older client, or a PATCH that's only
        # touching level_id and leaves batch/subjects alone). This is what
        # lets an Admin manage a Student's subject enrollment against ANY
        # batch, not just whichever one happens to be flagged current —
        # each Enrollment row already carries its own batch_id regardless.
        if payload.batch_id is not None:
            target_batch = db.query(Batch).filter(
                Batch.id == payload.batch_id, Batch.deleted_at.is_(None)
            ).first()
            if not target_batch:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
        else:
            target_batch = db.query(Batch).filter(
                Batch.is_current.is_(True), Batch.deleted_at.is_(None)
            ).first()
        if payload.subject_ids is not None and not target_batch:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No batch was specified and no current batch is configured — cannot assign subjects",
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
                    Enrollment.batch_id == target_batch.id,
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
                        student_id=user.id, subject_id=subject_id, batch_id=target_batch.id,
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
    """
    De-link Parent (delete_parent_link below) only soft-deletes the row —
    it never removes it — so a previously de-linked (parent_id,
    student_id) pair still has a row sitting here with deleted_at set.
    Re-linking the same two people used to blindly INSERT a new row,
    which collided with the unique constraint on that pair and raised an
    unhandled IntegrityError (500, no `detail` — surfaced to the Admin as
    the generic "Something went wrong" fallback). Now: if a soft-deleted
    row for this exact pair already exists, revive it (clear deleted_at,
    refresh relationship_label) instead of inserting a duplicate. If an
    ACTIVE link for this pair already exists, that's a real conflict —
    surfaced as a clean 400 rather than a DB-level error.
    """
    existing = db.query(ParentStudentLink).filter(
        ParentStudentLink.parent_id == payload.parent_id,
        ParentStudentLink.student_id == payload.student_id,
    ).first()

    if existing and existing.deleted_at is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This parent is already linked to this student.",
        )

    if existing:
        existing.deleted_at = None
        existing.relationship_label = payload.relationship_label
        log_action(db, current_user.id, "parent_link_revived", "parent_student_links", existing.id, None, None)
        db.commit()
        db.refresh(existing)
        return existing

    link = ParentStudentLink(**payload.model_dump())
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


@router.delete("/parent-links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_parent_link(
    link_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "coordinator")),
):
    """
    Parent Management (Student Edit Details) — De-link Parent. Reverse of
    create_parent_link above: soft-deletes the parent_student_links row so
    the Parent and Student accounts are no longer connected, without
    touching either account itself. Same Admin/Coordinator permission as
    creating a link — de-linking isn't a more sensitive action than
    linking, so it isn't gated any tighter.
    """
    link = db.query(ParentStudentLink).filter(
        ParentStudentLink.id == link_id, ParentStudentLink.deleted_at.is_(None)
    ).first()
    if not link:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent link not found")
    link.deleted_at = datetime.now(timezone.utc)
    log_action(db, current_user.id, "parent_link_removed", "parent_student_links", link.id, None, None)
    db.commit()
    return None


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