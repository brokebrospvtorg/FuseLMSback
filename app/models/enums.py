from sqlalchemy.dialects.postgresql import ENUM as PGEnum

# NOTE: All of these Postgres ENUM types were already created by the raw SQL
# migration (CREATE TYPE ... AS ENUM ...). create_type=False stops SQLAlchemy
# from trying to CREATE TYPE again (which would error since it already exists).

UserRole = PGEnum(
    "admin", "coordinator", "teacher", "student", "parent",
    name="user_role", create_type=False,
)
UserStatus = PGEnum(
    "pending", "active", "suspended",
    name="user_status", create_type=False,
)
TokenType = PGEnum(
    "activation", "password_reset",
    name="token_type", create_type=False,
)
CorrectionStatus = PGEnum(
    "pending", "approved", "rejected",
    name="correction_status", create_type=False,
)
BatchSession = PGEnum(
    "may_june", "oct_nov",
    name="batch_session", create_type=False,
)
LevelEnrollmentStatus = PGEnum(
    "active", "completed", "not_promoted", "withdrawn",
    name="level_enrollment_status", create_type=False,
)
SubjectRequestStatus = PGEnum(
    "requested", "approved", "rejected",
    name="subject_request_status", create_type=False,
)
EnrollmentStatus = PGEnum(
    "active", "dropped",
    name="enrollment_status", create_type=False,
)
DayOfWeek = PGEnum(
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    name="day_of_week", create_type=False,
)
AttendanceStatus = PGEnum(
    "present", "absent", "late", "excused",
    name="attendance_status", create_type=False,
)
AttendanceSource = PGEnum(
    "manual", "auto",
    name="attendance_source", create_type=False,
)
AssessmentStatus = PGEnum(
    "draft", "published",
    name="assessment_status", create_type=False,
)
FeeProofStatus = PGEnum(
    "pending", "approved", "rejected",
    name="fee_proof_status", create_type=False,
)
MaterialType = PGEnum(
    "notes", "worksheet", "past_paper", "other",
    name="material_type", create_type=False,
)
YoutubeVisibility = PGEnum(
    "unlisted",
    name="youtube_visibility", create_type=False,
)
ComplaintStatus = PGEnum(
    "open", "in_progress", "resolved", "closed",
    name="complaint_status", create_type=False,
)
NotificationChannel = PGEnum(
    "email", "in_app", "both",
    name="notification_channel", create_type=False,
)
