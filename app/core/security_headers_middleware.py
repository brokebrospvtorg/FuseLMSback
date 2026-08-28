from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

# ---------------------------------------------------------------------------
# Security Response Headers
# ---------------------------------------------------------------------------
# Standard hardening headers attached to EVERY outgoing response (success,
# error, and — importantly — the 403 CSRFMiddleware returns and the
# RateLimitExceeded handler's response too, since this wraps the whole
# ASGI call chain via call_next rather than being applied per-route).
#
# - Strict-Transport-Security: tells browsers to only ever talk to this
#   host over HTTPS for the given max-age (here: 2 years, in seconds),
#   including subdomains, and allows the domain to be preloaded into
#   browsers' built-in HSTS lists. Only meaningful once the app is served
#   over HTTPS in production — harmless (ignored) over plain HTTP in local
#   dev, so it's safe to always send.
# - X-Content-Type-Options: nosniff — stops browsers from MIME-sniffing a
#   response into a different content type than the server declared
#   (e.g. treating an uploaded file as HTML/JS and executing it).
# - X-Frame-Options: DENY — the app may never be embedded in an <iframe>
#   on another site, which blocks classic clickjacking attacks.
# - Referrer-Policy: strict-origin-when-cross-origin — cross-origin
#   requests (e.g. clicking an external link) only leak the origin, never
#   the full path/query, while same-origin navigation still sends the
#   full referrer.
#
# These are static, non-secret values, so they're defined as a plain
# module-level constant here rather than pulled from Settings — nothing
# about them varies per-environment the way FRONTEND_ORIGIN or the
# onboarding passwords do.
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Attaches standard security hardening headers to every response.

    Registration order matters in Starlette: middlewares added later wrap
    those added earlier, and Starlette dispatches through them in reverse
    registration order (last-added runs outermost). To guarantee these
    headers survive on *every* response — including CORS preflight/
    rejections and the CSRFMiddleware's 403 — add this middleware LAST in
    app/main.py, i.e. after CORSMiddleware, CSRFMiddleware, and
    GZipMiddleware, so it sits outermost and always gets to stamp the
    final response on its way out. It never inspects or rejects a
    request; it only ever adds headers to whatever call_next returns.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        return response
