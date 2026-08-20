from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core.config import settings
from app.core.limiter import limiter
from app.core.scheduler import start_scheduler, stop_scheduler
from app.routers import (
    auth, users, academic, timetable, attendance, marks, fees, content,
    complaints, notifications, audit, system, student_grades, parent, batches,
)

app = FastAPI(
    title="FUSE LMS API",
    description="Backend for Inklings Academy's FUSE Learning Management System",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Rate limiting (slowapi) — protects auth/verification endpoints from
# brute-force. Limits are declared with @limiter.limit(...) in app/routers/auth.py.
# ---------------------------------------------------------------------------
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# CORS — only the Angular frontend origin, credentials allowed for the
# HTTP-Only cookie to be sent cross-origin (Vercel/Netlify -> Render).
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(academic.router)
app.include_router(timetable.router)
app.include_router(attendance.router)
app.include_router(marks.router)
app.include_router(student_grades.router)
app.include_router(fees.router)
app.include_router(content.router)
app.include_router(content.classroom_requests_router)
app.include_router(content.youtube_requests_router)
app.include_router(complaints.router)
app.include_router(notifications.router)
app.include_router(audit.router)
app.include_router(system.router)
app.include_router(parent.router)
app.include_router(batches.router)
# NOTE (schema_update_11): app/routers/subjects.py ("Subject & Class
# Management" — free-form, batch-scoped subject creation with a required
# subject code) is intentionally NOT mounted anymore. It has been replaced
# by the pre-declared Cambridge subject catalog (app/seeds/seed_subjects.py,
# GET /api/academic/subjects) plus the Batch Summary endpoint below
# (GET /api/v1/batches/{batch_id}/summary). The file/table are left on disk
# only so any historical rows aren't destroyed — no route in this app
# reaches them anymore.


@app.get("/api/health")
def health_check():
    return {"status": "ok", "environment": settings.ENVIRONMENT}


# ---------------------------------------------------------------------------
# Background jobs: fee voucher generation (monthly) + expired token cleanup
# (daily). See app/core/jobs.py and app/core/scheduler.py. Also triggerable
# on-demand via the Admin-only endpoints in app/routers/system.py.
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _on_startup():
    start_scheduler()


@app.on_event("shutdown")
def _on_shutdown():
    stop_scheduler()
