# FUSE LMS — Backend (FastAPI)

Full backend for all 26 tables / 7 sprints: auth (HTTP-Only cookie JWT),
onboarding/activation, RBAC, batches/levels/subjects/enrollments, timetable +
attendance, assessments/marks/grades, fees, content (materials/lectures),
complaints/notifications, audit log, and the license kill-switch.

## Project layout

```
app/
  core/         config, DB session, JWT/bcrypt, RBAC + license dependencies, rate limiter
  models/       SQLAlchemy models (1:1 with the SQL migration you already ran)
  schemas/      Pydantic request/response schemas
  routers/      one router per domain, mounted in main.py
  utils/        email stub
  main.py       FastAPI app, CORS, rate-limit handler, router registration
requirements.txt
.env.example
```

## 1. Prerequisites

- Python 3.11+ (`python3 --version`)
- PostgreSQL running locally, with your existing migration already applied
  (the one with all 26 tables — you already tested this in TablePlus)
- VS Code with the **Python** extension (Microsoft) installed

## 2. Get the code into VS Code

1. Unzip/copy the `fuse_lms_backend` folder somewhere on your machine.
2. Open VS Code → `File > Open Folder...` → select `fuse_lms_backend`.

## 3. Create and activate a virtual environment

Open a terminal in VS Code (`` Ctrl+` ``) and run:

**Windows (PowerShell):**
```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

When active, your terminal prompt will show `(venv)`. In VS Code, also click
the Python version in the bottom-right status bar and pick the
`./venv` interpreter, so IntelliSense and debugging use the same environment.

## 4. Install dependencies

```bash
pip install -r requirements.txt
```

## 5. Configure environment variables

```bash
cp .env.example .env      # macOS/Linux
copy .env.example .env    # Windows
```

Edit `.env`:
- `DATABASE_URL` — point it at your local Postgres, e.g.
  `postgresql+psycopg2://postgres:YOUR_PASSWORD@localhost:5432/fuse_lms`
- `JWT_SECRET_KEY` — replace with a long random string
  (`python -c "import secrets; print(secrets.token_urlsafe(48))"`)
- `FRONTEND_ORIGIN` — your Angular dev server, default `http://localhost:4200`
- Leave `COOKIE_SECURE=false` for local HTTP development. **Set it to `true`
  once you deploy behind HTTPS on Render** — the cookie won't be sent over
  plain HTTP otherwise.

`.env` is already gitignored by convention — never commit real secrets.

## 6. Make sure your database is seeded

Your schema and seed script (from the SQL doc) already:
- enabled `pgcrypto`
- created all 26 tables + enums
- inserted the root Admin row and the `system_settings` row

If you haven't run that migration against the DB pointed to by `DATABASE_URL`
yet, run it now (e.g. via `psql` or TablePlus) before starting the API —
this backend does **not** auto-create tables; it maps onto your existing
schema.

Update the seeded Admin's `password_hash` to a real bcrypt hash before you
try logging in:
```bash
python -c "from app.core.security import hash_password; print(hash_password('YourAdminPassword123!'))"
```
Then `UPDATE users SET password_hash = '<paste hash>' WHERE role = 'admin';`

## 7. Run the API

```bash
uvicorn app.main:app --reload --port 8000
```

- API root: `http://localhost:8000`
- Interactive docs (Swagger UI): `http://localhost:8000/docs`
- Health check: `http://localhost:8000/api/health`

`--reload` restarts the server on file changes — keep it on while developing.

## 8. VS Code debugging (optional but recommended)

Create `.vscode/launch.json`:
```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "FastAPI: uvicorn",
      "type": "debugpy",
      "request": "launch",
      "module": "uvicorn",
      "args": ["app.main:app", "--reload", "--port", "8000"],
      "jinja": true,
      "justMyCode": true
    }
  ]
}
```
Then press `F5` to run with breakpoints.

## 9. Quick smoke test

1. Open `http://localhost:8000/docs`
2. `POST /api/auth/login` with the Admin email/password you hashed in step 6
   — this sets the HTTP-Only cookie automatically in the Swagger UI session.
3. `GET /api/auth/me` should return the Admin user.
4. `POST /api/users` (as Admin) to create a pending Student/Teacher/Coordinator
   — check your terminal output for the stubbed "email" (activation link with
   token), since real email sending isn't wired up yet (see below).

## 10. Wiring real email

`app/utils/email.py` currently just prints to the console. Once Account C's
Google Workspace transactional email service is set up, replace the body of
`send_email()` with the real API/SMTP call — every place that needs to send
mail already calls this single function.

## 11. What's implemented vs. what's still a stub

**Implemented:** cookie-based JWT auth, bcrypt hashing, RBAC on every route,
the license kill-switch (`check_license` dependency on all core routers),
onboarding/activation + correction-request flow, password reset, soft
deletes throughout, the weightage=100 hard block before publishing an
assessment, auto teacher-present attendance logic + Coordinator gap-filling
and override, derived fee voucher status, subject-scoped/batch-independent
content access, audit logging on key mutations (user changes, grade
overrides, fee proof reviews, subject request reviews).

Also implemented: **scheduled jobs** via APScheduler (`app/core/jobs.py`,
`app/core/scheduler.py`) — `generate_fee_vouchers` runs monthly and creates
one voucher per student with an active enrollment in the current batch;
`cleanup_expired_tokens` runs daily and hard-deletes `verification_tokens`
older than the 30-day grace period (used tokens by `used_at`, unused expired
tokens by `expires_at` — never touches `audit_logs`). Both start
automatically with the app (`ENABLE_SCHEDULER=true` in `.env`) and are also
triggerable on demand as Admin-only endpoints: `POST
/api/system/jobs/generate-fee-vouchers` and `POST
/api/system/jobs/cleanup-expired-tokens`. Schedule and amounts are
configurable via the `FEE_VOUCHER_*` / `TOKEN_CLEANUP_*` env vars in
`.env.example`.

**Left as stubs/next steps you'll want to wire up:**
- Real transactional email (Account C) instead of the print-based stub
- SonarQube/Snyk static analysis in CI (not a runtime backend concern)
- Google Classroom / YouTube API integration for pulling `gcr_resource_id`
  and `youtube_video_id` (this backend stores/serves the references; the
  actual OAuth + API calls to Google are a separate integration layer)
- Alembic migrations (you're currently managing schema via the raw SQL
  script directly, which is fine — add Alembic later if you want versioned
  migrations instead of hand-run SQL)

## 12. Deploying to Render

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Set the same env vars from `.env` in Render's dashboard (never commit `.env`)
- Set `COOKIE_SECURE=true` and `FRONTEND_ORIGIN` to your deployed Vercel/Netlify URL
