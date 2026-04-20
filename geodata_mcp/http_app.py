"""HTTP/Starlette composition + middleware. Built only when run with --http.

Composes FastMCP's streamable-HTTP routes with the viewer/API/OAuth/docs
routes and wraps the result in three defensive middleware layers (rate
limit → security headers → OAuth bearer). Everything in this module is
inert under stdio transport; server.py imports it lazily inside main().

Coupling to server.py is intentionally narrow: build_http_app() takes the
FastMCP instance, project root path, and a couple of helpers as keyword
arguments rather than importing them — keeps this module side-effect-free
on import and avoids a circular dependency with server.py.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import time as _time
from pathlib import Path
from typing import Callable

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    FileResponse, HTMLResponse, JSONResponse, StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import oauth as oauth_mod
from .operations import EXPORT_ROOT, EXPORT_TTL_S
from .session import REGISTRY


# ---------------------------------------------------------------------------
# Middleware — order from outside in: rate limit → security headers → OAuth.
# ---------------------------------------------------------------------------


class OAuthAuthMiddleware:
    """Guards the MCP protocol path (`/mcp/*`) with bearer auth. Accepts:
      - OAuth access tokens issued by our /oauth/token endpoint
      - The legacy GEODATA_MCP_TOKEN env var (for Claude Code CLI back-compat)
    On missing/invalid token returns 401 with WWW-Authenticate pointing at
    the RFC 9728 resource metadata so claude.ai discovers the OAuth flow.

    Public paths (everything else — /oauth/*, /.well-known/*, /view/*,
    /api/*, /static/*, /exports/*) pass through unauthenticated. Caddy no
    longer does a bearer check on /mcp; we handle it here so OAuth tokens
    and the shared bearer can coexist.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if not (path == "/mcp" or path.startswith("/mcp/")):
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        client = oauth_mod.validate_bearer(headers.get("authorization"))
        if client is None:
            req = Request(scope)
            challenge = oauth_mod.www_authenticate_header(req)
            resp = JSONResponse(
                {"error": "invalid_token",
                 "error_description": "missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": challenge},
            )
            return await resp(scope, receive, send)
        # Pass the authenticated caller downstream (for logging / auditing).
        scope = dict(scope)
        scope.setdefault("state", {})
        if isinstance(scope.get("state"), dict):
            scope["state"]["mcp_client"] = client
        return await self.app(scope, receive, send)


_CSP_HTML = (
    # script-src: self (inline app.js is cache-busted, loaded from
    # /static), plus unpkg for the pinned MapLibre (SRI-protected).
    # 'unsafe-inline' is load-bearing for the landing-page copy button,
    # the docs code-highlighting script, and the OAuth consent form;
    # without it those pages break.
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com 'unsafe-inline'; "
    "style-src 'self' https://fonts.googleapis.com https://unpkg.com 'unsafe-inline'; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    # img-src is intentionally broad (https:) — the viewer loads tiles
    # from user-pasted basemap URLs in addition to the default Carto CDN.
    "img-src 'self' data: https:; "
    # connect-src broad for the same reason: MapLibre's style/sprite
    # fetches target arbitrary origins when a custom basemap is pasted.
    "connect-src 'self' https:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware:
    """Attach CSP and a handful of defensive headers to HTML responses.

    MCP (JSON-RPC), API JSON endpoints, file downloads, and well-known
    metadata bypass this — CSP on those would be inert noise. The
    middleware only decorates responses whose Content-Type starts with
    text/html.
    """

    _SKIP_PREFIXES = ("/mcp", "/api/", "/exports/", "/static/",
                      "/.well-known/")
    _SKIP_PATHS = {"/oauth/token", "/oauth/register"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in self._SKIP_PATHS or any(
            path == p or path.startswith(p) for p in self._SKIP_PREFIXES
        ):
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                is_html = any(
                    k.lower() == b"content-type" and b"text/html" in v
                    for k, v in headers
                )
                if is_html:
                    headers.append(
                        (b"content-security-policy", _CSP_HTML.encode())
                    )
                    headers.append((b"x-content-type-options", b"nosniff"))
                    headers.append((b"x-frame-options", b"DENY"))
                    headers.append(
                        (b"referrer-policy",
                         b"strict-origin-when-cross-origin")
                    )
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RateLimitMiddleware:
    """Token-bucket rate limiter per client IP. Applies to all routes except
    /static/*. Generous defaults — this is belt-and-braces, not a production
    DDoS guard."""

    def __init__(self, app, rate_per_min: int = 120, burst: int = 40) -> None:
        self.app = app
        self.rate = rate_per_min / 60.0
        self.burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}  # ip → (tokens, last_ts)
        self._lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith("/static"):
            return await self.app(scope, receive, send)
        # Client IP — Cloudflare fronts us, so prefer CF-Connecting-IP.
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        ip = headers.get("cf-connecting-ip") or headers.get("x-forwarded-for", "").split(",")[0].strip()
        if not ip:
            ip = scope.get("client", ["unknown"])[0] or "unknown"
        now = _time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(ip, (self.burst, now))
            tokens = min(self.burst, tokens + self.rate * (now - last))
            if tokens < 1:
                self._buckets[ip] = (tokens, now)
                retry_after = int((1 - tokens) / self.rate) + 1
                response = JSONResponse(
                    {"error": "rate_limited", "retry_after_s": retry_after},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
                return await response(scope, receive, send)
            tokens -= 1
            self._buckets[ip] = (tokens, now)
        return await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Docs — allow-list of slug → (title, eyebrow). Underscore-prefixed files
# in docs/ (deployment, security) are private and never exposed. Order
# matters: it determines the sidebar ordering.
# ---------------------------------------------------------------------------


DOCS_PAGES = [
    ("index",        "Overview",                "Documentation"),
    ("design",       "Design philosophy",       "Why this exists"),
    ("architecture", "Architecture",            "How the pieces fit"),
    ("data",         "Data model",              "What is loaded and how it's shaped"),
    ("tools",        "Tool reference",          "The MCP surface"),
    ("sessions",     "Sessions and persistence", "The workspace model"),
    ("viewer",       "Viewer",                  "Interactive map surface"),
    ("provenance",   "Provenance",              "Layer- and column-level lineage"),
    ("rendering",    "Map rendering",           "Server-side PNG export"),
    ("roadmap",      "Roadmap and limitations", "What's missing, what's deferred"),
]
DOCS_BY_SLUG = {slug: (title, eyebrow) for slug, title, eyebrow in DOCS_PAGES}


# ---------------------------------------------------------------------------
# Composition.
# ---------------------------------------------------------------------------


def build_http_app(
    *,
    mcp,
    root: Path,
    layer_summary: Callable,
    qi: Callable[[str], str],
) -> object:
    """Compose FastMCP's streamable-HTTP routes with the viewer/API routes.

    Args:
        mcp: the FastMCP instance (server.py owns this).
        root: project root (for locating viewer/ and docs/).
        layer_summary: server.py's `_layer_summary(sess, name)` helper.
        qi: server.py's `_qi(ident)` quoter for safe SQL identifiers.

    Returns the fully-composed Starlette ASGI app (rate-limit > security
    headers > OAuth > Starlette).
    """
    viewer_dir = root / "viewer"
    docs_dir = root / "docs"
    # Content-hash the JS at startup so the viewer HTML references
    # /static/app.js?v=<hash>. Changes auto-bust Cloudflare's
    # cache-control: max-age=14400.
    app_js_hash = hashlib.sha256(
        (viewer_dir / "app.js").read_bytes()
    ).hexdigest()[:10]
    index_template = (viewer_dir / "index.html").read_text(encoding="utf-8")
    index_rendered = index_template.replace("{APP_JS_HASH}", app_js_hash)
    landing_html = (viewer_dir / "landing.html").read_text(encoding="utf-8")
    docs_template = (viewer_dir / "docs.html").read_text(encoding="utf-8")
    notfound_html = (viewer_dir / "404.html").read_text(encoding="utf-8")
    about_html = (viewer_dir / "about.html").read_text(encoding="utf-8")

    def _render_docs_page(slug: str) -> HTMLResponse | None:
        if slug not in DOCS_BY_SLUG:
            return None
        import markdown as _md
        try:
            md_text = (docs_dir / f"{slug}.md").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        title, eyebrow = DOCS_BY_SLUG[slug]
        # Rewrite bare slug links in markdown so "[tools](tools)" becomes
        # an absolute `/docs/tools` link (markdown's default would produce
        # a relative link that breaks under the viewer path layout).
        for other_slug in DOCS_BY_SLUG:
            md_text = md_text.replace(f"]({other_slug})",
                                      f"](/docs/{other_slug})")
        html = _md.markdown(
            md_text,
            extensions=["fenced_code", "tables", "toc", "sane_lists"],
        )
        # Sidebar TOC — links to every allow-listed page, marking current.
        toc_items = []
        for s, t, _ in DOCS_PAGES:
            cls = ' class="current"' if s == slug else ''
            toc_items.append(
                f'      <li><a href="/docs/{s}"{cls}>{t}</a></li>'
            )
        page = (docs_template
                .replace("@@TITLE@@", title)
                .replace("@@EYEBROW@@", eyebrow)
                .replace("@@TOC@@", "\n".join(toc_items))
                .replace("@@CONTENT@@", html))
        return HTMLResponse(page)

    async def root_index(request):
        return HTMLResponse(landing_html)

    async def about_page(request):
        return HTMLResponse(about_html)

    async def docs_index(request):
        resp = _render_docs_page("index")
        if resp is None:
            return HTMLResponse("<h1>Docs unavailable</h1>", status_code=500)
        return resp

    async def docs_page(request):
        slug = request.path_params["slug"]
        if not slug.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(404)
        resp = _render_docs_page(slug)
        if resp is None:
            raise HTTPException(404)
        return resp

    async def not_found(request, exc):
        return HTMLResponse(notfound_html, status_code=404)

    async def view_index(request):
        return HTMLResponse(index_rendered)

    async def api_visible(request):
        sid = request.path_params["session_id"]
        s = REGISTRY.get(sid)
        if s is None:
            return JSONResponse(
                {"error": "unknown_or_expired_session", "session_id": sid},
                status_code=404,
            )
        return JSONResponse({
            "session_id": s.id,
            "version": s.version,
            "visible_layers": s.visible_layers,
            "layers": {n: layer_summary(s, n) for n in s.visible_layers},
            "styles": s.visible_styles,
            "title": s.visible_title,
        })

    async def api_version(request):
        """Tiny endpoint for viewer to poll — just the session's version
        counter. The viewer diffs on it to decide whether to re-fetch."""
        sid = request.path_params["session_id"]
        s = REGISTRY.get(sid)
        if s is None:
            return JSONResponse({"error": "unknown_or_expired_session"},
                                status_code=404)
        return JSONResponse({
            "session_id": s.id,
            "version": s.version,
            "visible_layers": s.visible_layers,
        })

    async def api_audit_log(request):
        """Per-session audit timeline for the viewer's history panel.

        Returns each Operation in chronological order, with its captured SQL
        statements grouped by correlation_id. Internal probes (DESCRIBE,
        bbox queries, etc.) are filtered out unless ?include_internal=1.
        """
        sid = request.path_params["session_id"]
        s = REGISTRY.get(sid)
        if s is None:
            return JSONResponse({"error": "unknown_or_expired_session"},
                                status_code=404)
        include_internal = request.query_params.get("include_internal") in (
            "1", "true", "yes",
        )
        from .session import _audit_to_dict, _op_to_dict
        sql_by_cid: dict[str, list[dict]] = {}
        unattached: list[dict] = []
        for rec in s.audit:
            if rec.internal and not include_internal:
                continue
            d = _audit_to_dict(rec)
            if rec.correlation_id:
                sql_by_cid.setdefault(rec.correlation_id, []).append(d)
            else:
                unattached.append(d)
        operations = []
        for op in s.history:
            d = _op_to_dict(op)
            d["sql_statements"] = sql_by_cid.get(op.correlation_id, []) \
                if op.correlation_id else []
            operations.append(d)
        return JSONResponse({
            "session_id": s.id,
            "version": s.version,
            "operations": operations,
            "unattached_sql": unattached,
            "include_internal": include_internal,
        })

    async def serve_export(request):
        """Serve /exports/{token}/{filename} from data/exports/.
        Path-traversal-safe: token must be a pure identifier, filename must
        live inside <EXPORT_ROOT>/<token>/."""
        token = request.path_params["token"]
        filename = request.path_params["filename"]
        if not token.replace("-", "").replace("_", "").isalnum():
            return JSONResponse({"error": "bad_token"}, status_code=400)
        if "/" in filename or ".." in filename:
            return JSONResponse({"error": "bad_filename"}, status_code=400)
        path = (EXPORT_ROOT / token / filename).resolve()
        if not str(path).startswith(str(EXPORT_ROOT.resolve())):
            return JSONResponse({"error": "bad_path"}, status_code=400)
        if not path.exists():
            return JSONResponse({"error": "not_found"}, status_code=404)
        # Check expiry — anything older than EXPORT_TTL is refused and cleaned up.
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            # Raced with a concurrent expiry cleanup.
            return JSONResponse({"error": "expired"}, status_code=410)
        if _time.time() - mtime > EXPORT_TTL_S:
            try:
                path.unlink()
                path.parent.rmdir()
            except OSError:
                pass
            return JSONResponse({"error": "expired"}, status_code=410)
        # FileResponse opens the file lazily; if another request's expiry
        # sweep deletes it between here and the send, Starlette raises.
        # Fall back to 410 if the file is already gone.
        try:
            return FileResponse(path, filename=filename)
        except FileNotFoundError:
            return JSONResponse({"error": "expired"}, status_code=410)

    async def api_layer_geojson(request):
        """Emit a layer as a GeoJSON FeatureCollection.

        Streams features in chunks rather than building one giant JSON value,
        which OOMs the 256 MB per-session DuckDB limit for large layers
        (observed with 79 k buildings). Per-feature JSON is cheap; the
        aggregate is the problem.
        """
        sid = request.path_params["session_id"]
        layer = request.path_params["layer"]
        s = REGISTRY.get(sid)
        if s is None or layer not in s.layers:
            return JSONResponse({"error": "unknown_layer_or_session"},
                                status_code=404)
        meta = s.layers[layer]
        geom_col = meta.attributes.get("__geom_col__") or ""
        if not geom_col:
            return JSONResponse({"type": "FeatureCollection", "features": []})
        cols = [c for c in meta.attributes
                if not c.startswith("__") and c != geom_col]
        props_struct = ", ".join(f"'{c}', {qi(c)}" for c in cols) or "'_', NULL"

        # Build the per-feature JSON on the DuckDB side but keep them as
        # individual rows so the aggregator doesn't hold them all at once.
        per_feature_sql = f"""
            SELECT json_object(
                'type', 'Feature',
                'properties', json_object({props_struct}),
                'geometry', ST_AsGeoJSON(ST_Transform({qi(geom_col)}, 'EPSG:3011', 'EPSG:4326', true))::JSON
            )::VARCHAR AS feature_json
            FROM {qi(layer)}
        """
        CHUNK = 2000  # features per fetch — bounded per-iteration alloc

        async def stream():
            try:
                cur = s.conn.execute(per_feature_sql)
            except Exception as e:
                yield ('{"type":"FeatureCollection","features":[],'
                       '"error":"geojson_query_failed",'
                       f'"detail":{json.dumps(str(e))}' + '}').encode("utf-8")
                return
            yield b'{"type":"FeatureCollection","features":['
            first = True
            try:
                while True:
                    rows = cur.fetchmany(CHUNK)
                    if not rows:
                        break
                    parts = []
                    for (fj,) in rows:
                        if fj is None:
                            continue
                        if first:
                            first = False
                        else:
                            parts.append(",")
                        parts.append(fj)
                    if parts:
                        yield ("".join(parts)).encode("utf-8")
            except Exception as e:
                yield (f'],"error":"geojson_stream_failed",'
                       f'"detail":{json.dumps(str(e))}' + '}').encode("utf-8")
                return
            yield b']}'

        return StreamingResponse(stream(), media_type="application/json")

    # FastMCP's http_app provides a /mcp route AND a lifespan that starts
    # the streamable-http session manager. We must (a) include its routes
    # directly (Mount double-prefixes the path) and (b) propagate its
    # lifespan.
    mcp_app = mcp.http_app(transport="http")

    routes = [
        *mcp_app.routes,
        *oauth_mod.routes(),
        Route("/", root_index),
        Route("/about", about_page),
        Route("/docs", docs_index),
        Route("/docs/", docs_index),
        Route("/docs/{slug}", docs_page),
        Route("/view/{session_id}", view_index),
        Route("/api/{session_id}/visible_layers", api_visible),
        Route("/api/{session_id}/version", api_version),
        Route("/api/{session_id}/audit_log", api_audit_log),
        Route("/api/{session_id}/layer/{layer}/geojson", api_layer_geojson),
        Route("/exports/{token}/{filename}", serve_export),
        Mount("/static", StaticFiles(directory=str(viewer_dir)), name="static"),
    ]
    # Compose a lifespan that runs FastMCP's startup/shutdown AND flushes
    # persistent session state on SIGTERM so graceful restarts don't drop
    # any dirty sidecars.
    _inner_lifespan = mcp_app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app):  # noqa: ANN001
        async with _inner_lifespan(app):
            try:
                yield
            finally:
                REGISTRY.flush_all()

    app = Starlette(
        routes=routes, lifespan=lifespan,
        exception_handlers={404: not_found},
    )
    app = SecurityHeadersMiddleware(app)
    app = OAuthAuthMiddleware(app)
    return RateLimitMiddleware(app)
