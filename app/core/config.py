from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    COOKIE_SECURE: bool = False
    COOKIE_DOMAIN: str = "localhost"
    COOKIE_NAME: str = "access_token"

    CSRF_COOKIE_NAME: str = "csrf_token"
    CSRF_HEADER_NAME: str = "X-CSRF-Token"

    FRONTEND_ORIGIN: str = "http://localhost:4200"
    ENVIRONMENT: str = "development"

    ACTIVATION_TOKEN_EXPIRE_HOURS: int = 24
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 30

    # --- Scheduled jobs ---
    ENABLE_SCHEDULER: bool = True
    TOKEN_CLEANUP_RETENTION_DAYS: int = 30
    TOKEN_CLEANUP_CRON_HOUR: int = 2  # runs daily at 02:00 server time

    FEE_VOUCHER_DEFAULT_AMOUNT: float = 15000.0
    FEE_VOUCHER_DUE_DAYS: int = 14  # due_date = generation date + this many days
    FEE_VOUCHER_CRON_DAY: int = 1  # day-of-month the generation job runs
    FEE_VOUCHER_CRON_HOUR: int = 3

    # --- Fee proof local-disk storage (no external storage provider) ---
    # Relative to the backend process's working directory. Not web-served
    # directly (no StaticFiles mount) — always read through the protected
    # /api/fees/proofs/{id}/file endpoint so RBAC applies to every read.
    FEE_PROOF_UPLOAD_DIR: str = "uploads/fee_proofs"
    # Frontend compresses images to ~150KB before upload, but that's a
    # client-side step we can't trust — this is the real, enforced ceiling.
    # Generous over 150KB since PDFs (not just images) go through here too.
    FEE_PROOF_MAX_SIZE_MB: float = 5.0
    FEE_PROOF_ALLOWED_EXTENSIONS: set[str] = {".pdf", ".jpg", ".jpeg", ".png"}

    # --- Fee proof retention policy ---
    FEE_PROOF_RETENTION_DAYS: int = 90
    FEE_PROOF_RETENTION_CRON_HOUR: int = 4  # runs daily at 04:00 server time
    FEE_PROOF_RETENTION_PLACEHOLDER: str = "DELETED_DUE_TO_RETENTION_POLICY"

    # --- Batch expiry (Batch Generator utility, app/core/batch_utils.py) ---
    # A batch is "over" once the NEXT standard batch's month arrives (see
    # batch_utils.is_batch_over) — checked daily since that's cheap and
    # months don't roll over predictably on any single day-of-month.
    BATCH_EXPIRY_CRON_HOUR: int = 1  # runs daily at 01:00 server time

    # --- Onboarding / admin-reset default passwords ---
    # Fixed, predictable handoff values — never the account's standing
    # password, since must_change_password=True is forced on every branch
    # that uses them. Still Settings-backed (so an env var can override
    # per deployment if you ever want to), but each one now has a safe
    # fallback default matching the original hardcoded value — no .env
    # changes required. Optionally override any of these in .env:
    #   DEFAULT_TEACHER_INITIAL_PASSWORD=...
    #   DEFAULT_STUDENT_INITIAL_PASSWORD=...
    #   DEFAULT_PARENT_INITIAL_PASSWORD=...
    #   ADMIN_RESET_TEMP_PASSWORD=...
    DEFAULT_TEACHER_INITIAL_PASSWORD: str = "Inkling@2026"
    DEFAULT_STUDENT_INITIAL_PASSWORD: str = "Inkling@2026"
    DEFAULT_PARENT_INITIAL_PASSWORD: str = "Inkling@2026"
    ADMIN_RESET_TEMP_PASSWORD: str = "Inkling@2026"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()