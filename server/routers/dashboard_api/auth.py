"""Dashboard API: one-time browser authentication.

The SPA reads the API token from the ``bob_dashboard_secret`` cookie (see
``ui_app/src/lib/api.ts``) — but nothing on the server ever SET that cookie,
so after the API token gate landed (2026-08-22) the dashboard's websocket and
every state-changing SPA call failed auth until the cookie was set by hand in
DevTools. Workable on a desktop, impossible on a phone.

``GET /dashboard/api/auth?secret=<token>`` validates the token and sets the
cookie, so a device authenticates by visiting one URL once. GET passes the
token gate by design; this handler is itself the validator.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()

_COOKIE_MAX_AGE = 365 * 24 * 3600  # one year; re-visit the URL to refresh


@router.get("/api/auth")
async def dashboard_auth(request: Request):
    settings = request.app.state.settings
    if not settings.api_auth_enabled:
        return {"ok": True, "auth_disabled": True}

    # The token comes from the URL, deliberately NOT via extract_api_token:
    # that helper checks the cookie BEFORE the query param, so a browser
    # still holding a stale/broken cookie would have it shadow the fresh
    # token in the URL — and this endpoint is the RECOVERY path for exactly
    # that situation (observed live 2026-08-22: correct token in the URL,
    # 401 because the old mangled cookie won the priority race). A valid
    # cookie never needs this endpoint anyway.
    token = request.query_params.get("secret", "")
    import secrets as _secrets

    # A raw-pasted URL (the phone flow) is not percent-encoded: a base64
    # secret's '+' arrives decoded as a space. Base64/urlsafe tokens never
    # contain literal spaces, so restoring '+' is unambiguous.
    def _valid(candidate: str) -> bool:
        return bool(candidate) and (
            _secrets.compare_digest(candidate, settings.resolved_api_secret)
            or _secrets.compare_digest(candidate.replace(" ", "+"), settings.resolved_api_secret)
        )

    if not _valid(token):
        return JSONResponse(status_code=401, content={"ok": False, "detail": "invalid token"})

    response = JSONResponse({"ok": True})
    # NOT HttpOnly: the SPA reads the cookie via document.cookie to build its
    # ?secret= query and WS URLs. SameSite=Lax keeps it out of cross-site
    # requests; the tailnet/LAN dashboard is served over plain http, so
    # Secure would prevent the cookie from being stored at all.
    #
    # The cookie value is the CANONICAL secret, never the token as received:
    # a raw-pasted URL leaves the received form with a space where '+' was,
    # and a space makes the cookie layer quote the value ('"abc def="') —
    # which then fails every later comparison (observed live 2026-08-22).
    response.set_cookie(
        "bob_dashboard_secret",
        settings.resolved_api_secret,
        max_age=_COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
        httponly=False,
    )
    return response
