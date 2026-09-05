"""Starlette wiring: portal page + JSON API.

Same ASGI stack the work-dashboards Cloud Run MCP already runs on
(starlette/uvicorn) — no new tech. ``build_app(store)`` is used by tests and
by ``app/main.py`` (which also mounts the MCP under /mcp).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import auth
from .api import HANDLERS
from .config import portal_base_url
from .store import Store

_PORTAL_PATH = Path(__file__).resolve().parent / "portal.html"
_PORTAL_HTML = _PORTAL_PATH.read_text(encoding="utf-8")


def _portal_html() -> str:
    """The portal page, read once at import.

    ``PORTAL_DEV_RELOAD=1`` re-reads from disk per request so a UI edit is a
    browser refresh instead of a server restart. Opt-in by an explicit env var
    rather than inferred from the environment, so production and local run the
    same code path unless someone deliberately says otherwise — production never
    sets it, and a stat+read per page load is not a cost worth paying there.
    """
    if os.environ.get("PORTAL_DEV_RELOAD") == "1":
        return _PORTAL_PATH.read_text(encoding="utf-8")
    return _PORTAL_HTML

_INVALID_LINK_HTML = """<!doctype html><html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>链接无效 / Invalid link</title></head>
<body style="font-family:sans-serif;text-align:center;padding:3rem 1rem">
<h1>链接无效或已过期</h1>
<p>This link is invalid or has expired.</p>
<p>请联系家人获取新链接。/ Please ask for a new link.</p>
</body></html>"""

_NOT_ALLOWED_HTML = """<!doctype html><html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>没有权限 / Not allowed</title></head>
<body style="font-family:sans-serif;text-align:center;padding:3rem 1rem">
<h1>这个账号没有权限</h1>
<p>This account is not on the allowlist.</p>
<p><a href="/logout">换一个账号登录 / Sign in as someone else</a></p>
</body></html>"""


def _safe_next(raw: str) -> str:
    """Only same-site paths. Blocks '//evil.com' and 'https://evil.com' — an
    open redirect on the login route would let a crafted link bounce a
    signed-in family member straight off the site."""
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


def _redirect_uri(request: Request) -> str:
    """Auth0 demands an exact match, so prefer an explicit base URL.

    Cloud Run terminates TLS upstream, so ``request.url_for`` can report
    ``http://`` unless proxy headers are trusted — which would produce a
    redirect_uri Auth0 rejects. PORTAL_BASE_URL removes the guesswork.
    """
    base = portal_base_url()
    if base:
        return f"{base}/callback"
    return str(request.url_for("auth_callback"))


def build_routes(store: Store) -> list[Route]:
    async def healthz(request: Request):
        return JSONResponse({"ok": True})

    async def favicon(request: Request):
        return Response(status_code=204)

    async def portal(request: Request):
        token = request.path_params["token"]
        # Token first: a bad link 404s without sending anyone through a login
        # round trip only to be rejected at the end of it.
        if await run_in_threadpool(store.validate_token, token) is None:
            return HTMLResponse(_INVALID_LINK_HTML, status_code=404)
        if auth.is_enabled() and auth.current_user(request) is None:
            return RedirectResponse(
                f"/login?next={quote(request.url.path, safe='/')}", status_code=302
            )
        return HTMLResponse(_portal_html())

    def make_api_endpoint(handler):
        async def endpoint(request: Request):
            if auth.is_enabled() and auth.current_user(request) is None:
                return JSONResponse(
                    {"ok": False, "error": "not signed in"}, status_code=401
                )
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    {"ok": False, "error": "invalid JSON body"}, status_code=400
                )
            # The handlers are synchronous and every one of them talks to the
            # database. Called directly they block the event loop for the whole
            # round trip to Neon, so one slow query stalls every other request
            # in the process — including /health. Postgres connections are
            # thread-local and production runs against Neon's pooled endpoint,
            # so a threadpool worker getting its own connection is free.
            status, payload = await run_in_threadpool(handler, store, body)
            return JSONResponse(payload, status_code=status)

        return endpoint

    routes = [
        Route("/health", healthz, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
        Route("/t/{token}", portal, methods=["GET"]),
    ]
    routes += [
        Route(f"/api/{name}", make_api_endpoint(handler), methods=["POST"])
        for name, handler in HANDLERS.items()
    ]
    routes += _auth_routes()
    return routes


def _auth_routes() -> list[Route]:
    """Login/callback/logout — only mounted when OAuth is configured.

    Absent by default, so with the env vars unset the route table is byte-for-
    byte what it was before OAuth existed.
    """
    oauth = auth.build_oauth()
    if oauth is None:
        return []

    async def login(request: Request):
        request.session["next"] = _safe_next(request.query_params.get("next", "/"))
        return await oauth.auth0.authorize_redirect(request, _redirect_uri(request))

    async def auth_callback(request: Request):
        try:
            token = await oauth.auth0.authorize_access_token(request)
        except Exception:
            # Expired/replayed state, or the user backed out at Auth0. Start over
            # rather than showing a stack trace.
            return RedirectResponse("/login", status_code=302)
        info = token.get("userinfo") or {}
        email = info.get("email")
        if not auth.is_allowed(email):
            request.session.clear()
            return HTMLResponse(_NOT_ALLOWED_HTML, status_code=403)
        request.session[auth.SESSION_USER_KEY] = {
            "email": email, "name": info.get("name"), "sub": info.get("sub"),
        }
        return RedirectResponse(
            _safe_next(request.session.pop("next", "/")), status_code=302
        )

    async def logout(request: Request):
        request.session.clear()
        base = portal_base_url()
        return RedirectResponse(
            auth.logout_url(base or str(request.base_url).rstrip("/")),
            status_code=302,
        )

    return [
        Route("/login", login, methods=["GET"], name="auth_login"),
        Route("/callback", auth_callback, methods=["GET"], name="auth_callback"),
        Route("/logout", logout, methods=["GET"], name="auth_logout"),
    ]


def session_middleware():
    """Session middleware list — empty when OAuth is off.

    Kept as a helper so app/main.py wraps the combined app with exactly the
    same configuration as build_app, rather than the two drifting apart.
    """
    if not auth.is_enabled():
        return []
    from starlette.middleware import Middleware
    from starlette.middleware.sessions import SessionMiddleware

    return [
        Middleware(
            SessionMiddleware,
            secret_key=auth.session_secret(),
            https_only=True,
            same_site="lax",  # must survive Auth0's cross-site redirect back
        )
    ]


def build_app(store: Store) -> Starlette:
    """Portal-only app (tests / running without the MCP mount)."""
    return Starlette(routes=build_routes(store), middleware=session_middleware())
