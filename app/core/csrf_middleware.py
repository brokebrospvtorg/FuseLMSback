from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.core.config import settings

CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Paths that are ALWAYS exempt from the CSRF check, regardless of whether
# the request happens to be carrying a session cookie. Login and logout
# are the two cases where "no session cookie present" isn't a reliable
# enough signal on its own:
#   - /login: a client re-authenticating while still holding a stale/
#     expired session cookie from a previous login (very common — browser
#     tabs, Swagger UI's "Try it out", a token that expired but the
#     cookie itself hasn't been cleared yet) would otherwise get a false
#     403 here, since the middleware would see that leftover cookie and
#     demand a CSRF pair for an endpoint that doesn't need one — you're
#     not doing anything to an existing authenticated session, you're
#     establishing a new one from a fresh set of credentials.
#   - /logout: ending your own session via your own valid cookie is not
#     something CSRF protection is meant to gate either; if a stale
#     cookie triggers the same false-403 here, the user can't even log
#     out to clear it.
# Everything else that actually mutates data under an active session
# still goes through the normal check below.
CSRF_EXEMPT_PATHS = {"/api/auth/login", "/api/auth/logout"}


class CSRFMiddleware(BaseHTTPMiddleware):
    """
    Double-submit cookie check for every state-changing request.

    Enforced whenever the request carries the session cookie
    (settings.COOKIE_NAME) AND the path isn't in CSRF_EXEMPT_PATHS above.
    Session-establishing/ending POSTs made *before* a session meaningfully
    exists — /submit-activation, /request-password-reset, etc. — are still
    naturally exempt via the "no session cookie yet" check; /login and
    /logout are listed explicitly because a STALE cookie can be present
    even when the session it refers to is no longer meaningfully active,
    which the implicit check alone doesn't handle (see CSRF_EXEMPT_PATHS'
    comment for the concrete failure mode this fixes).
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        if request.method not in CSRF_SAFE_METHODS and request.url.path not in CSRF_EXEMPT_PATHS:
            session_cookie = request.cookies.get(settings.COOKIE_NAME)
            if session_cookie:
                csrf_cookie = request.cookies.get(settings.CSRF_COOKIE_NAME)
                csrf_header = request.headers.get(settings.CSRF_HEADER_NAME)

                if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF token missing or invalid."},
                    )

        return await call_next(request)