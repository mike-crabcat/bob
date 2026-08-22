"""Default-deny token gate for state-changing HTTP requests.

Any POST/PUT/PATCH/DELETE must present the API token (see
``Settings.resolved_api_secret``) or it is rejected with 401. Websocket
scopes pass through untouched — WS endpoints keep their own auth
(``dashboard_ws`` checks ``?secret=``, the voice realtime bridge validates
session tokens). Exempt POST paths (Twilio callbacks, public voice-page
logging) are listed in ``PUBLIC_UNAUTHENTICATED``.
"""

from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from bob_server.config import Settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# (method, path) pairs that must stay reachable by external callers without a
# token: Twilio voice/status callbacks and the public voice pages' log sink.
PUBLIC_UNAUTHENTICATED: dict[str, frozenset[str]] = {
    "POST": frozenset({"/phone/twiml", "/phone/status", "/voice/log"}),
}


def extract_api_token(request: Request) -> str:
    """Pull the API token from any of the accepted transports."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if header := request.headers.get("x-dashboard-secret", ""):
        return header.strip()
    if cookie := request.cookies.get("bob_dashboard_secret", ""):
        return cookie
    return request.query_params.get("secret", "")


def api_token_valid(settings: Settings, request: Request) -> bool:
    """Single comparison path for every auth site (middleware, dashboard,
    dashboard WS, dependencies)."""
    if not settings.api_auth_enabled:
        return True
    token = extract_api_token(request)
    return bool(token) and secrets.compare_digest(token, settings.resolved_api_secret)


class ApiAuthMiddleware:
    """Deny POST/PUT/PATCH/DELETE unless a valid API token is presented."""

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope["method"].upper()
        if method in SAFE_METHODS or scope["path"] in PUBLIC_UNAUTHENTICATED.get(method, frozenset()):
            await self.app(scope, receive, send)
            return
        if api_token_valid(self.settings, Request(scope, receive)):
            await self.app(scope, receive, send)
            return
        await JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized: valid API token required"},
            headers={"WWW-Authenticate": 'Bearer realm="bob-api"'},
        )(scope, receive, send)
