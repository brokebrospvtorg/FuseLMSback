from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.core.config import settings

CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class CSRFMiddleware(BaseHTTPMiddleware):
    """
    Double-submit cookie check for every state-changing request.

    Only enforced when the request already carries the session cookie
    (settings.COOKIE_NAME) — this naturally exempts /login, /logout,
    /submit-activation, /request-password-reset, etc., which are POSTs
    made *before* a session exists. Everything that mutates data under an
    active session must present a matching csrf_token cookie + X-CSRF-Token
    header, or it's rejected before it reaches the route.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        if request.method not in CSRF_SAFE_METHODS:
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