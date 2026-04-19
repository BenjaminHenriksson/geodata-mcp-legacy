"""Minimal OAuth 2.1 + PKCE authorization server — invite-code login.

Implements the protocol endpoints that claude.ai's custom-connector OAuth
client expects (per the MCP Nov 2025 auth spec), with a drastically
simplified "identity provider": users log in by typing a shared invite
code. The code is stored in the server's environment (GEODATA_INVITE_CODE)
and rotated out-of-band — you share it with coworkers in Slack/DM the
same way you would a Netflix password.

Endpoints (all public — must not be behind the /mcp bearer check):

    GET  /.well-known/oauth-protected-resource     RFC 9728 pointer
    GET  /.well-known/oauth-authorization-server   RFC 8414 metadata
    POST /oauth/register                            RFC 7591 dynamic client reg
    GET  /oauth/authorize                           render invite-code form
    POST /oauth/authorize                           validate code → auth code
    POST /oauth/token                               exchange code → access token

Storage is in-memory. Access tokens live 7 days; if the server restarts
users re-auth. Good enough for a small-team MCP demo; swap for sqlite if
you ever want persistence.

The access tokens this module issues are opaque random strings. Validate
them via `validate_token(token)`; returns the issuing client_id on success
or None on failure/expiry. The MCP app's auth middleware calls that, and
also accepts the legacy shared-bearer (GEODATA_MCP_TOKEN) for Claude Code
CLI back-compat.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


# ---- configuration -------------------------------------------------------

INVITE_CODE = os.environ.get("GEODATA_INVITE_CODE", "").strip()
LEGACY_BEARER = os.environ.get("GEODATA_MCP_TOKEN", "").strip()

# Public URL for issuer / endpoint advertisement. Required for claude.ai —
# it will call back to what we advertise here.
PUBLIC_URL = os.environ.get("GEODATA_PUBLIC_URL", "").rstrip("/")

ACCESS_TOKEN_TTL_S = 7 * 24 * 60 * 60     # 7 days
REFRESH_TOKEN_TTL_S = 30 * 24 * 60 * 60   # 30 days
AUTH_CODE_TTL_S = 5 * 60                   # 5 minutes (spec max 10)


# ---- stores (in-memory, lock-protected) ----------------------------------

_lock = threading.Lock()
_clients: dict[str, dict] = {}             # client_id → {redirect_uris, client_name}
_auth_codes: dict[str, dict] = {}          # code → {client_id, redirect_uri, code_challenge, method, exp, scope}
_access_tokens: dict[str, dict] = {}       # token → {client_id, exp}
_refresh_tokens: dict[str, dict] = {}      # token → {client_id, exp}


def _now() -> float:
    return time.time()


def _random_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def _reap(store: dict) -> None:
    """Drop expired entries. Cheap; called inline at each op."""
    now = _now()
    for k in [k for k, v in store.items() if v.get("exp", 0) < now]:
        store.pop(k, None)


# ---- endpoint handlers ---------------------------------------------------

async def protected_resource_metadata(request: Request) -> JSONResponse:
    """RFC 9728 — lets claude.ai discover which auth server to talk to."""
    issuer = PUBLIC_URL or str(request.base_url).rstrip("/")
    return JSONResponse({
        "resource": f"{issuer}/mcp",
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
    })


async def authorization_server_metadata(request: Request) -> JSONResponse:
    """RFC 8414 — advertises OAuth capabilities."""
    issuer = PUBLIC_URL or str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        # Public clients only (PKCE makes this safe). claude.ai uses this.
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    })


async def register(request: Request) -> JSONResponse:
    """RFC 7591 dynamic client registration.
    claude.ai calls this to announce itself. We accept any reasonable
    client and issue an unauthenticated `client_id`. PKCE handles the
    security; no client_secret."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_client_metadata",
                             "error_description": "body must be JSON"},
                            status_code=400)
    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": "redirect_uris is required"},
                            status_code=400)

    client_id = "c_" + _random_token(16)
    client_name = body.get("client_name", "unknown")
    with _lock:
        _clients[client_id] = {
            "redirect_uris": [str(u) for u in redirect_uris],
            "client_name": str(client_name),
            "created_at": _now(),
        }
    return JSONResponse({
        "client_id": client_id,
        "client_id_issued_at": int(_now()),
        "redirect_uris": redirect_uris,
        "client_name": client_name,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }, status_code=201)


_AUTH_FORM_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Connect to geodata-mcp</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { margin: 0; background: #0b0f14; color: #d7dde4;
           font: 15px -apple-system, BlinkMacSystemFont, sans-serif;
           min-height: 100vh; display: flex; align-items: center;
           justify-content: center; }
    .card { background: #12181f; padding: 28px 32px; border-radius: 10px;
            width: 380px; box-shadow: 0 10px 30px rgba(0,0,0,0.4); }
    h1 { font-size: 16px; margin: 0 0 4px; color: #fff; }
    p  { color: #8c95a1; margin: 0 0 18px; font-size: 13px; line-height: 1.5; }
    label { display: block; font-size: 12px; color: #8c95a1;
            margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.05em; }
    input[type=password] { width: 100%; padding: 10px 12px;
        border: 1px solid #2a323d; background: #0b0f14; color: #fff;
        border-radius: 6px; font: inherit; box-sizing: border-box; }
    input[type=password]:focus { outline: none; border-color: #4ecdc4; }
    button { margin-top: 14px; width: 100%; padding: 10px 14px;
        background: #4ecdc4; color: #0b0f14; border: 0;
        border-radius: 6px; font: inherit; font-weight: 600;
        cursor: pointer; }
    button:hover { background: #5ee0d7; }
    .err { color: #ff8a8a; font-size: 13px; margin-top: 12px; }
    .meta { color: #5c6672; font-size: 11px; margin-top: 18px;
            line-height: 1.5; }
    .client { color: #d7dde4; }
  </style>
</head>
<body>
  <form class="card" method="post" action="/oauth/authorize">
    <h1>Connect to geodata-mcp</h1>
    <p><span class="client">@@CLIENT_NAME@@</span> is asking for access.
       Enter the invite code to continue.</p>
    <label for="code">Invite code</label>
    <input type="password" id="code" name="invite_code" autocomplete="off"
           autofocus required>
    <button type="submit">Connect</button>
    @@ERROR@@
    @@HIDDEN@@
    <div class="meta">Stockholm open geodata · read-only MCP server ·
        <a href="https://github.com/BenjaminHenriksson/geodata-mcp"
           style="color:#4ecdc4">source</a></div>
  </form>
</body>
</html>
"""


def _render_form(client_name: str, params: dict, error: str | None = None) -> HTMLResponse:
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in params.items()
    )
    err_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
    # Use explicit tokens instead of str.format so CSS braces pass through.
    page = (_AUTH_FORM_HTML
            .replace("@@CLIENT_NAME@@", html.escape(client_name or "a client"))
            .replace("@@HIDDEN@@", hidden)
            .replace("@@ERROR@@", err_html))
    return HTMLResponse(page)


async def authorize_get(request: Request) -> HTMLResponse | JSONResponse:
    """Render the invite-code form."""
    params = dict(request.query_params)
    required = ["response_type", "client_id", "redirect_uri", "code_challenge"]
    missing = [k for k in required if not params.get(k)]
    if missing:
        return JSONResponse({"error": "invalid_request",
                             "error_description": f"missing: {missing}"},
                            status_code=400)
    if params.get("response_type") != "code":
        return JSONResponse({"error": "unsupported_response_type"},
                            status_code=400)
    if params.get("code_challenge_method", "S256") != "S256":
        return JSONResponse({"error": "invalid_request",
                             "error_description": "only S256 is supported"},
                            status_code=400)
    with _lock:
        client = _clients.get(params["client_id"])
    if client is None:
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    if params["redirect_uri"] not in client["redirect_uris"]:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    return _render_form(client["client_name"], params)


def _redirect_with_code(redirect_uri: str, code: str, state: str | None) -> RedirectResponse:
    sep = "&" if "?" in redirect_uri else "?"
    q = urlencode({"code": code, **({"state": state} if state else {})})
    return RedirectResponse(f"{redirect_uri}{sep}{q}", status_code=302)


async def authorize_post(request: Request) -> HTMLResponse | JSONResponse | RedirectResponse:
    """Validate the invite code; on success issue an authorization code
    and redirect the user back to the client."""
    form = await request.form()
    params = {k: (v if isinstance(v, str) else v.filename) for k, v in form.items()}

    if not INVITE_CODE:
        return JSONResponse(
            {"error": "server_error",
             "error_description": "GEODATA_INVITE_CODE not configured on server"},
            status_code=500)

    submitted = params.get("invite_code", "")
    ok = hmac.compare_digest(submitted.strip(), INVITE_CODE)
    client_id = params.get("client_id")
    with _lock:
        client = _clients.get(client_id) if client_id else None
    if client is None:
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    if params.get("redirect_uri") not in client["redirect_uris"]:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    if not ok:
        # Re-render with error, keeping the OAuth params in hidden fields.
        keep = {k: v for k, v in params.items() if k != "invite_code"}
        return _render_form(client["client_name"], keep, error="Invalid invite code.")

    code = "ac_" + _random_token(24)
    with _lock:
        _reap(_auth_codes)
        _auth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": params["redirect_uri"],
            "code_challenge": params["code_challenge"],
            "code_challenge_method": params.get("code_challenge_method", "S256"),
            "scope": params.get("scope", "mcp"),
            "exp": _now() + AUTH_CODE_TTL_S,
        }
    return _redirect_with_code(params["redirect_uri"], code, params.get("state"))


def _verify_pkce(code_verifier: str, challenge: str) -> bool:
    """S256: base64url(SHA256(code_verifier)) == code_challenge"""
    if not code_verifier or not challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, challenge)


async def token(request: Request) -> JSONResponse:
    """Exchange an authorization code (or refresh token) for an access token."""
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code = form.get("code") or ""
        verifier = form.get("code_verifier") or ""
        redirect_uri = form.get("redirect_uri") or ""
        client_id = form.get("client_id") or ""
        with _lock:
            _reap(_auth_codes)
            entry = _auth_codes.pop(code, None)  # one-time-use
        if entry is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if entry["client_id"] != client_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if entry["redirect_uri"] != redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not _verify_pkce(verifier, entry["code_challenge"]):
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "PKCE verification failed"},
                                status_code=400)
        access = "at_" + _random_token(32)
        refresh = "rt_" + _random_token(32)
        with _lock:
            _access_tokens[access] = {"client_id": client_id,
                                       "exp": _now() + ACCESS_TOKEN_TTL_S}
            _refresh_tokens[refresh] = {"client_id": client_id,
                                         "exp": _now() + REFRESH_TOKEN_TTL_S}
        return JSONResponse({
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_S,
            "refresh_token": refresh,
            "scope": entry.get("scope") or "mcp",
        })

    if grant_type == "refresh_token":
        rt = form.get("refresh_token") or ""
        client_id = form.get("client_id") or ""
        with _lock:
            _reap(_refresh_tokens)
            entry = _refresh_tokens.get(rt)
            if entry is None or entry["client_id"] != client_id:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            access = "at_" + _random_token(32)
            _access_tokens[access] = {"client_id": client_id,
                                       "exp": _now() + ACCESS_TOKEN_TTL_S}
        return JSONResponse({
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_S,
            "scope": "mcp",
        })

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ---- token validation (used by the /mcp middleware) ---------------------

def validate_bearer(header_value: str | None) -> str | None:
    """Return the authenticated client_id (or 'legacy-cli') if the bearer
    token is valid; otherwise None. Accepts:
      - An OAuth access token issued by /oauth/token
      - The legacy shared GEODATA_MCP_TOKEN (for Claude Code CLI compat)
    """
    if not header_value:
        return None
    if not header_value.lower().startswith("bearer "):
        return None
    token_str = header_value[7:].strip()
    if not token_str:
        return None
    # Legacy shared bearer.
    if LEGACY_BEARER and hmac.compare_digest(token_str, LEGACY_BEARER):
        return "legacy-cli"
    # OAuth-issued access token.
    with _lock:
        _reap(_access_tokens)
        entry = _access_tokens.get(token_str)
    if entry is not None:
        return entry["client_id"]
    return None


def www_authenticate_header(request: Request) -> str:
    """401 WWW-Authenticate value — points to the protected-resource metadata."""
    issuer = PUBLIC_URL or str(request.base_url).rstrip("/")
    return (
        f'Bearer realm="geodata-mcp", '
        f'resource_metadata="{issuer}/.well-known/oauth-protected-resource"'
    )


# ---- route registration helper ------------------------------------------

def routes() -> list:
    from starlette.routing import Route
    return [
        Route("/.well-known/oauth-protected-resource",
              protected_resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server",
              authorization_server_metadata, methods=["GET"]),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authorize_get, methods=["GET"]),
        Route("/oauth/authorize", authorize_post, methods=["POST"]),
        Route("/oauth/token", token, methods=["POST"]),
    ]
