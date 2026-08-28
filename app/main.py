from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core.config import settings
from app.core.limiter import limiter
from app.core.csrf_middleware import CSRFMiddleware
from app.core.security_headers_middleware import SecurityHeadersMiddleware
from app.core.scheduler import start_scheduler, stop_scheduler
from app.routers import (
    auth, users, academic, timetable, attendance, marks, fees, content,
    complaints, notifications, audit, system, student_grades, parent, batches,
    subjects, password_requests, teachers,
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
# Security response headers (HSTS, X-Content-Type-Options, X-Frame-Options,
# Referrer-Policy) — added FIRST so it ends up OUTERMOST in the stack
# (Starlette wraps middlewares in reverse registration order: whatever is
# added first wraps everything else and gets the final say on the way out).
# Being outermost means these headers land on every response this app ever
# returns — including a CORS-rejected request and a CSRFMiddleware 403 —
# without needing to touch either of those. It only ever ADDS headers, so
# it never overwrites or conflicts with the CORS/CSRF headers set below.
# ---------------------------------------------------------------------------
app.add_middleware(SecurityHeadersMiddleware)

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
    # X-CSRF-Token is a custom response header (set on /login and /me) —
    # without this, browsers hide it from JS on cross-origin responses
    # even though allow_headers=["*"] permits it on the *request* side.
    expose_headers=[settings.CSRF_HEADER_NAME],
)

# ---------------------------------------------------------------------------
# CSRF (double-submit cookie) — must be added AFTER CORSMiddleware so CORS
# stays outermost and still attaches its headers to a 403 CSRF rejection;
# see app/core/csrf_middleware.py for what's enforced and why.
# ---------------------------------------------------------------------------
app.add_middleware(CSRFMiddleware)

# ---------------------------------------------------------------------------
# Response compression — must be added AFTER CORSMiddleware (Starlette
# wraps middlewares in reverse registration order, so this keeps CORS
# headers innermost/correct on every response). minimum_size=500 skips
# tiny payloads (e.g. /api/health) where compression overhead isn't worth it.
# ---------------------------------------------------------------------------
app.add_middleware(GZipMiddleware, minimum_size=500)

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
# app/routers/subjects.py: repurposed as the Admin Subjects module (edit
# name/code, activate/deactivate, dependency-checked delete). It no longer
# holds the old schema_update_11 "Subject & Class Management" router (see
# that file's own docstring) — GET/POST /api/academic/subjects (list +
# create the catalog) remain in academic.router above; this one adds the
# admin-only mutations on /api/academic/subjects/{id} and .../{id}/status.
app.include_router(subjects.router)
app.include_router(password_requests.router)
app.include_router(teachers.router)


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