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

Storage is persistent: registered clients and access + refresh tokens
are written to `.duckdb/oauth.json` (mode 0600) so a graceful service
restart doesn't force every user back through the invite-code form.
Access tokens live 7 days, refresh tokens 30; expired entries are
dropped during load. Auth codes stay in-memory only (5-min TTL, single
use). Good enough for a small-team MCP demo; swap for SQLite + hashed
tokens if you ever need audit trails or at-rest-leak resistance.

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
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


# ---- configuration -------------------------------------------------------

def _load_secret(cred_name: str, env_fallback: str) -> str:
    """Prefer systemd's $CREDENTIALS_DIRECTORY/<cred_name> over env vars.
    Under the service unit we use LoadCredential= (file-based, not in
    /proc/<pid>/environ) so a DuckDB file-read exploit can't exfiltrate
    the token via read_text('/proc/self/environ'). The env fallback is
    for dev / stdio mode where systemd isn't involved."""
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if cred_dir:
        path = os.path.join(cred_dir, cred_name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError):
            pass
    return os.environ.get(env_fallback, "").strip()


INVITE_CODE = _load_secret("invite-code", "GEODATA_INVITE_CODE")
LEGACY_BEARER = _load_secret("mcp-token", "GEODATA_MCP_TOKEN")

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

# Persistence so a graceful restart doesn't force every user back through
# the invite-code form. Clients and both token kinds survive; auth_codes
# stay in-memory (5-min TTL, single-use, rarely mid-flight).
#
# Threat model for this file:
# - Path is inside the service's ReadWritePaths (same zone as the DuckDB
#   sessions); not web-served.
# - Mode 0600 at create-time so even a relaxed parent directory wouldn't
#   expose tokens.
# - Tokens stored verbatim (not hashed). A file leak equals a token leak
#   for the 7/30-day TTL window; equivalent surface to /etc/credstore.
#   If that ever needs tightening, switch to HMAC-hashed lookups.
_OAUTH_STORE_PATH = (Path(__file__).resolve().parents[1]
                     / ".duckdb" / "oauth.json")


def _now() -> float:
    return time.time()


def _random_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n)


def _reap(store: dict) -> None:
    """Drop expired entries. Cheap; called inline at each op."""
    now = _now()
    for k in [k for k, v in store.items() if v.get("exp", 0) < now]:
        store.pop(k, None)


def _load_store() -> None:
    """Load persisted clients + tokens from disk. Expired tokens are
    dropped during the load. Safe to call at import.

    Load failures (missing file, corrupt JSON) are non-fatal: the server
    comes up with an empty store and users re-authorise on next use. We
    log the condition so an operator can tell the difference between
    "clean first boot" and "the file went missing".
    """
    import sys as _sys
    if not _OAUTH_STORE_PATH.exists():
        # Silent on first boot; loud if a restart is supposed to
        # preserve state but can't find the file.
        if _clients or _access_tokens or _refresh_tokens:
            print(f"[oauth] store {_OAUTH_STORE_PATH} is missing; "
                  f"starting with empty state (users will re-auth).",
                  file=_sys.stderr)
        return
    try:
        raw = _OAUTH_STORE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        print(f"[oauth] failed to parse {_OAUTH_STORE_PATH}: "
              f"{type(e).__name__}: {e}. Starting with empty state; "
              f"users will re-auth.",
              file=_sys.stderr)
        return
    now = _now()
    dropped_expired = 0
    with _lock:
        for cid, cmeta in (data.get("clients") or {}).items():
            _clients[cid] = cmeta
        for t, entry in (data.get("access_tokens") or {}).items():
            if entry.get("exp", 0) > now:
                _access_tokens[t] = entry
            else:
                dropped_expired += 1
        for t, entry in (data.get("refresh_tokens") or {}).items():
            if entry.get("exp", 0) > now:
                _refresh_tokens[t] = entry
            else:
                dropped_expired += 1
    print(f"[oauth] loaded {len(_clients)} clients, "
          f"{len(_access_tokens)} access tokens, "
          f"{len(_refresh_tokens)} refresh tokens "
          f"(dropped {dropped_expired} expired) from {_OAUTH_STORE_PATH}",
          file=_sys.stderr)


def _save_store_locked() -> None:
    """Atomic write of in-memory OAuth state. Caller must hold `_lock`.

    Writes to a sibling `.tmp` first, then `os.replace()` so a partial
    write can't corrupt a live file.
    """
    _OAUTH_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Reap expired entries before serialising so the file stays small.
    _reap(_access_tokens)
    _reap(_refresh_tokens)
    payload = json.dumps({
        "version": 1,
        "clients": _clients,
        "access_tokens": _access_tokens,
        "refresh_tokens": _refresh_tokens,
    }, ensure_ascii=False, separators=(",", ":"))
    tmp = _OAUTH_STORE_PATH.with_suffix(".tmp")
    # Mode 0600 at open so the fresh file is never world-readable even
    # if the parent directory's permissions loosen.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise
    os.replace(str(tmp), str(_OAUTH_STORE_PATH))


_load_store()


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
        _save_store_locked()
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
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Connect to Geodata MCP</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <meta name="theme-color" content="#F5F0E8">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;1,400&family=DM+Sans:wght@300;400;500&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
  <style>
    :root {
      --ivory:     #F5F0E8;
      --paper:     #EDE7D9;
      --ink:       #1C1A16;
      --ink-mid:   #4A4540;
      --ink-faint: #9A9188;
      --terra:     #B05B3B;
      --amber-bg:  #F5E6C8;
      --amber-bd:  #C99A5B;
      --amber-ink: #6E4A1E;
      --rule:      rgba(28,26,22,0.14);
      --serif:     'Cormorant Garamond', Georgia, serif;
      --sans:      'DM Sans', system-ui, sans-serif;
      --mono:      'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    *, *::before, *::after { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--ivory);
      color: var(--ink);
      font-family: var(--sans);
      font-weight: 300;
      font-size: 15px;
      line-height: 1.6;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 2rem 1rem;
      -webkit-font-smoothing: antialiased;
    }
    body::before {
      content: '';
      position: fixed;
      inset: 0;
      background-image:
        repeating-linear-gradient(0deg, transparent, transparent 59px, rgba(28,26,22,0.04) 60px),
        repeating-linear-gradient(90deg, transparent, transparent 59px, rgba(28,26,22,0.04) 60px);
      pointer-events: none;
      z-index: 0;
    }
    .card {
      position: relative;
      z-index: 1;
      background: var(--ivory);
      border: 1px solid var(--rule);
      padding: 2.2rem 2.4rem 2rem;
      width: 100%;
      max-width: 440px;
      box-shadow: 0 10px 32px -8px rgba(28,26,22,0.14);
    }
    .eyebrow {
      font-size: 10px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--terra);
      margin-bottom: 0.4rem;
      font-weight: 400;
    }
    h1 {
      font-family: var(--serif);
      font-size: 32px;
      font-weight: 400;
      letter-spacing: -0.01em;
      line-height: 1.1;
      margin: 0 0 1rem;
    }
    h1 em { font-style: italic; color: var(--terra); }
    .lede {
      color: var(--ink-mid);
      font-size: 14px;
      line-height: 1.65;
      margin: 0 0 1.2rem;
    }
    .lede .client { color: var(--ink); font-weight: 400; }
    .label {
      display: block;
      font-size: 10px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--terra);
      margin: 1.4rem 0 0.35rem;
      font-weight: 400;
    }
    .callback {
      display: block;
      background: var(--paper);
      border: 1px solid var(--rule);
      padding: 0.55rem 0.7rem;
      font-family: var(--mono);
      font-size: 13px;
      color: var(--ink);
      word-break: break-all;
      user-select: all;
    }
    .warn {
      margin-top: 0.9rem;
      padding: 0.75rem 0.9rem;
      background: var(--amber-bg);
      border-left: 2px solid var(--amber-bd);
      color: var(--amber-ink);
      font-size: 12px;
      line-height: 1.55;
    }
    .warn strong { color: var(--amber-ink); font-weight: 500; }
    .warn code {
      font-family: var(--mono);
      font-size: 11px;
      background: rgba(255,255,255,0.55);
      padding: 0 3px;
      border: 1px solid rgba(28,26,22,0.08);
    }
    input[type=password] {
      width: 100%;
      padding: 0.55rem 0.7rem;
      background: var(--ivory);
      color: var(--ink);
      border: 1px solid var(--rule);
      border-radius: 0;
      font-family: var(--mono);
      font-size: 13px;
      letter-spacing: 0.06em;
    }
    input[type=password]:focus {
      outline: none;
      border-color: var(--terra);
    }
    button[type=submit] {
      margin-top: 1.2rem;
      width: 100%;
      padding: 0.7rem 1rem;
      background: var(--ink);
      color: var(--ivory);
      border: none;
      border-radius: 0;
      font-family: var(--sans);
      font-size: 11px;
      font-weight: 400;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      cursor: pointer;
      transition: background 0.15s;
    }
    button[type=submit]:hover { background: var(--terra); }
    .err {
      margin-top: 0.9rem;
      padding: 0.55rem 0.75rem;
      background: rgba(176,91,59,0.10);
      border-left: 2px solid var(--terra);
      color: var(--terra);
      font-size: 12px;
      line-height: 1.5;
    }
    .meta {
      margin-top: 1.8rem;
      padding-top: 1rem;
      border-top: 1px solid var(--rule);
      color: var(--ink-faint);
      font-size: 11px;
      letter-spacing: 0.04em;
      line-height: 1.5;
    }
    .meta a {
      color: var(--ink-mid);
      text-decoration: underline;
      text-decoration-color: var(--rule);
      text-underline-offset: 3px;
    }
    .meta a:hover { color: var(--terra); text-decoration-color: var(--terra); }
  </style>
</head>
<body>
  <form class="card" method="post" action="/oauth/authorize">
    <div class="eyebrow">Authorize · Stockholm</div>
    <h1>Connect to Geodata <em>MCP</em></h1>
    <p class="lede">
      <span class="client">@@CLIENT_NAME@@</span> is requesting access.
      Verify the callback below before continuing. That is where your
      access token will be sent.
    </p>
    <span class="label">Callback host</span>
    <span class="callback">@@REDIRECT_HOST@@</span>
    <div class="warn">
      If this is not a host you recognize (e.g. <code>claude.ai</code>
      for claude.ai, <code>localhost</code> for local testing),
      <strong>do not continue</strong>. It could be a phishing attempt
      to hijack your session.
    </div>
    <label class="label" for="code">Invite code</label>
    <input type="password" id="code" name="invite_code" autocomplete="off"
           autofocus required>
    <button type="submit">Connect</button>
    @@ERROR@@
    @@HIDDEN@@
    <div class="meta">
      Stockholm open geodata, read-only MCP server.
      <a href="https://github.com/BenjaminHenriksson/geodata-mcp">Source</a>.
    </div>
  </form>
</body>
</html>
"""


def _render_form(client_name: str, params: dict, error: str | None = None) -> HTMLResponse:
    from urllib.parse import urlparse
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in params.items()
    )
    err_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
    # Show the callback's host + scheme prominently so a phished user has a
    # chance to notice when the redirect_uri is attacker-controlled. Full
    # URL is hidden in the HTML source; the host+scheme preview is what
    # the user actually eyeballs. We fall back to the whole URL if parsing
    # fails.
    redirect_uri = params.get("redirect_uri", "")
    try:
        parsed = urlparse(redirect_uri)
        if parsed.scheme and parsed.netloc:
            preview = f"{parsed.scheme}://{parsed.netloc}"
        else:
            preview = redirect_uri or "(missing)"
    except Exception:
        preview = redirect_uri or "(missing)"
    page = (_AUTH_FORM_HTML
            .replace("@@CLIENT_NAME@@", html.escape(client_name or "a client"))
            .replace("@@REDIRECT_HOST@@", html.escape(preview))
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
            _save_store_locked()
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
            entry = _refresh_tokens.pop(rt, None)   # single-use: consume it.
            if entry is None or entry["client_id"] != client_id:
                # If the RT existed and we just popped it, it's now
                # invalid — a well-behaved client won't retry with the
                # same RT. If a legit request races with an attacker
                # replaying the same RT, the loser gets invalid_grant;
                # neither can keep using the stolen token past its
                # one-use moment.
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            # OAuth 2.1 BCP: rotate the refresh token on each use so a
            # leaked RT is good for exactly one exchange, not a 30-day
            # window. Access token lifetime is unchanged.
            access = "at_" + _random_token(32)
            new_rt = "rt_" + _random_token(32)
            _access_tokens[access] = {"client_id": client_id,
                                       "exp": _now() + ACCESS_TOKEN_TTL_S}
            _refresh_tokens[new_rt] = {"client_id": client_id,
                                        "exp": _now() + REFRESH_TOKEN_TTL_S}
            _save_store_locked()
        return JSONResponse({
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_S,
            "refresh_token": new_rt,
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
