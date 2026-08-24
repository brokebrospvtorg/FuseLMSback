from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.core.database import get_db
from app.core.dependencies import get_current_user, coerce_expiry_to_utc_datetime
from app.core.limiter import limiter
from app.core.security import (
    verify_password, hash_password, create_access_token,
    set_auth_cookie, clear_auth_cookie, generate_verification_token,
)
from app.core.config import settings
from app.models import (
    User, VerificationToken, CorrectionRequest, StudentProfile, TeacherProfile,
    PasswordResetRequest,
)
from app.schemas.auth import (
    LoginRequest, UserOut, TokenVerifyRequest, TokenVerifyResponse,
    ActivationSubmitRequest, CorrectionOnActivationRequest,
    PasswordResetRequestSchema, PasswordResetSubmitRequest, ChangePasswordRequest,
    AdminPasswordResetRequestCreate,
)
from app.utils.email import send_email

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=UserOut)
@limiter.limit("5/minute")
def login(request: Request, payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    license_check = db.execute(text("SELECT license_expiry_date FROM system_settings WHERE id = 1")).fetchone()
    if license_check and license_check[0]:
        expiry_date = coerce_expiry_to_utc_datetime(license_check[0])
        if datetime.now(timezone.utc) > expiry_date:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="System Locked: School core subscription has expired. Please contact administration."
            )

    user = db.query(User).filter(User.email == payload.email, User.deleted_at.is_(None)).first()

    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    if user.status == "pending":
        if not (user.password_hash and user.must_change_password):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your profile is pending activation. Please verify via email token."
            )
    elif user.status == "suspended":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account suspended due to policy violations."
        )
    elif user.status != "active" and not (user.status == "pending" and user.must_change_password):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Account is {user.status}")

    token = create_access_token(str(user.id), user.role)
    set_auth_cookie(response, token)

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return user


@router.post("/logout")
def logout(response: Response):
    clear_auth_cookie(response)
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/verify-token", response_model=TokenVerifyResponse)
@limiter.limit("10/minute")
def verify_token(request: Request, payload: TokenVerifyRequest, db: Session = Depends(get_db)):
    vt = (
        db.query(VerificationToken)
        .filter(VerificationToken.token == payload.token, VerificationToken.used_at.is_(None))
        .first()
    )
    if not vt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or already-used token")

    expires_at = vt.expires_at if vt.expires_at.tzinfo else vt.expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token has expired")

    user = db.query(User).filter(User.id == vt.user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return TokenVerifyResponse(
        full_name=user.full_name,
        email=user.email,
        role=user.role,
        token_type=vt.token_type,
        expires_at=vt.expires_at,
    )


@router.post("/submit-activation", response_model=UserOut)
@limiter.limit("10/minute")
def submit_activation(request: Request, payload: ActivationSubmitRequest, db: Session = Depends(get_db)):
    vt = (
        db.query(VerificationToken)
        .filter(
            VerificationToken.token == payload.token,
            VerificationToken.used_at.is_(None),
            VerificationToken.token_type == "activation",
        )
        .first()
    )
    if not vt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or already-used token")

    expires_at = vt.expires_at if vt.expires_at.tzinfo else vt.expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Activation link has expired")

    user = db.query(User).filter(User.id == vt.user_id, User.deleted_at.is_(None)).first()
    if not user or user.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Account is not pending activation")

    user.password_hash = hash_password(payload.password)
    user.status = "active"
    user.must_change_password = False
    vt.used_at = datetime.now(timezone.utc)

    try:
        db.commit()
        db.refresh(user)
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database commit failed: {str(e)}"
        )

    return user


@router.post("/submit-correction", status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def submit_correction_on_activation(
    request: Request, payload: CorrectionOnActivationRequest, db: Session = Depends(get_db)
):
    vt = (
        db.query(VerificationToken)
        .filter(
            VerificationToken.token == payload.token,
            VerificationToken.used_at.is_(None),
            VerificationToken.token_type == "activation",
        )
        .first()
    )
    if not vt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or already-used token")

    user = db.query(User).filter(User.id == vt.user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    correction = CorrectionRequest(student_id=user.id, requested_changes=payload.requested_changes)
    db.add(correction)
    db.commit()
    return {"detail": "Correction request submitted. An Admin will review it."}


@router.post("/request-password-reset")
@limiter.limit("5/minute")
def request_password_reset(request: Request, payload: PasswordResetRequestSchema, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email, User.deleted_at.is_(None)).first()
    if user and user.status == "active":
        token_str = generate_verification_token()
        vt = VerificationToken(
            user_id=user.id,
            token=token_str,
            token_type="password_reset",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES),
        )
        db.add(vt)
        db.commit()
        send_email(user.email, "Reset your FUSE LMS password", f"Reset link token: {token_str}")

    return {"detail": "If that email exists, a reset link has been sent."}


@router.post("/request-password-reset-approval", status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def request_password_reset_approval(
    request: Request, payload: AdminPasswordResetRequestCreate, db: Session = Depends(get_db),
):
    identifier = payload.identifier.strip()

    user = (
        db.query(User)
        .filter(User.email == identifier, User.deleted_at.is_(None))
        .first()
    )
    if not user:
        student = (
            db.query(StudentProfile)
            .join(User, User.id == StudentProfile.user_id)
            .filter(StudentProfile.roll_number == identifier, User.deleted_at.is_(None))
            .first()
        )
        if student:
            user = db.query(User).filter(User.id == student.user_id).first()
    if not user:
        teacher = (
            db.query(TeacherProfile)
            .join(User, User.id == TeacherProfile.user_id)
            .filter(TeacherProfile.teacher_code == identifier, User.deleted_at.is_(None))
            .first()
        )
        if teacher:
            user = db.query(User).filter(User.id == teacher.user_id).first()

    if user and user.status == "active":
        existing_pending = (
            db.query(PasswordResetRequest)
            .filter(
                PasswordResetRequest.user_id == user.id,
                PasswordResetRequest.status == "pending",
                PasswordResetRequest.deleted_at.is_(None),
            )
            .first()
        )
        if not existing_pending:
            reset_request = PasswordResetRequest(
                user_id=user.id,
                identifier_submitted=identifier,
            )
            db.add(reset_request)
            db.commit()

    return {"detail": "Reset request sent to Admin successfully."}


@router.post("/submit-password-reset")
@limiter.limit("10/minute")
def submit_password_reset(request: Request, payload: PasswordResetSubmitRequest, db: Session = Depends(get_db)):
    vt = (
        db.query(VerificationToken)
        .filter(
            VerificationToken.token == payload.token,
            VerificationToken.used_at.is_(None),
            VerificationToken.token_type == "password_reset",
        )
        .first()
    )
    if not vt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or already-used token")

    expires_at = vt.expires_at if vt.expires_at.tzinfo else vt.expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset link has expired")

    user = db.query(User).filter(User.id == vt.user_id, User.deleted_at.is_(None)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.password_hash = hash_password(payload.new_password)
    vt.used_at = datetime.now(timezone.utc)
    db.commit()
    return {"detail": "Password updated. You can now log in."}


@router.post("/change-password")
@limiter.limit("5/minute")
def change_password(
    request: Request, payload: ChangePasswordRequest,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    if not current_user.password_hash or not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")

    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from your current password",
        )

    current_user.password_hash = hash_password(payload.new_password)
    current_user.must_change_password = False
    if current_user.status == "pending":
        current_user.status = "active"
    db.commit()
    return {"detail": "Password changed successfully."}