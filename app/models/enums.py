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

# Subject & Class Management (see app/models/subject.py + schema_update_10).
# Strictly these 4 values — the frontend cascading form's Class Level
# dropdown mirrors this list by hand, same as every other enum here.
# NOTE (schema_update_11): the free-form "Subject & Class Management"
# feature this enum backed (app/routers/subjects.py, ClassSubject) has been
# superseded by the pre-declared Cambridge Subject catalog + Batch Summary
# view — see app/seeds/seed_subjects.py and app/routers/batches.py. The
# table/enum are left in place (not dropped) purely so existing rows aren't
# destroyed; the router is no longer mounted in app/main.py.
ClassLevel = PGEnum(
    "O Level", "AS Level", "A2 Level", "A Level (Combined)",
    name="class_level", create_type=False,
)

# Exam Board — schema_update_11. Fixed 3-value global catalog used by
# Students (single board they're registered under), Teachers (one or more
# boards they're qualified to teach), and Batches (the board the batch is
# run under). Kept as its own enum (not folded into ClassLevel or Level)
# since board and academic level are orthogonal — a Student/Teacher/Batch
# picks exactly one axis of "O Level vs A Level" (via Level/Subject.level)
# and independently one-or-more of "which examining board".
Board = PGEnum(
    # schema_update_16: 'All' added for catalog Subjects that run under
    # every examining board (POST /api/academic/subjects). Deliberately
    # NOT offered as a choice on Student/Teacher/Batch board dropdowns —
    # those forms' own option lists are unchanged; this only widens what
    # the shared Postgres type accepts.
    "British Council", "Edexcel", "LRN", "All",
    name="board", create_type=False,
)
