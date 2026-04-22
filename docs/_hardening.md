# Security posture and deployment-portable hardening

Private doc. Captures the defense-in-depth posture of the geodata-mcp
service so a public-release operator can see the whole stack at once
and understand which layers are load-bearing vs nice-to-have. Written
after the 2026-04-20 security audit.

---

## Threat model

Callers are semi-trusted:

- An authenticated claude.ai user has traversed an invite-code gate
  and is talking to the server through OAuth 2.1 + PKCE.
- Inside a session, the caller (effectively an LLM emitting tool
  calls) has broad read access to 66 pre-normalised Swedish open
  datasets and can issue DuckDB SQL via `execute_sql`, `derive`, and
  `edit_field`.
- The LLM is considered prone to mistakes but not actively malicious.
  The MCP client is treated the same way.
- The data the server hosts is *open* (CC0 / ODbL / CC BY). Secrets
  on the host (credstore contents, OAuth access tokens, `.env` files,
  `/etc/shadow`) are emphatically *not* open.
- The network environment is:
  - Cloudflare → Caddy (TLS) → 127.0.0.1:8765 Python app.
  - Caddy is the only fronting proxy; the app binds to loopback only.

The adversary we design against is: **an attacker who finds a bypass in
our SQL validator or tool-argument parsing and tries to pivot into
host-level access, credential theft, or lateral movement.** Everything
else is secondary.

---

## Defense layers (outside → in)

Each layer is described with: what it protects against, what would
happen if it fell, and whether a public-fork operator can remove it.

### 1. Cloudflare edge (WAF + DDoS)

- **Protects**: common web scanners, volumetric DDoS, bot traffic.
- **If removed**: server is exposed to raw internet scan/abuse; the
  remaining layers still hold but UX degrades.
- **Public fork**: optional. Caddy + the in-app rate limiter are
  adequate for small-scale use. Document as "put this behind a CDN
  if you expect public traffic."

### 2. TLS termination + rate limiter (Caddy + app)

- **Protects**: eavesdropping, MITM, per-IP request-rate abuse.
- The app-level `RateLimitMiddleware` is a per-IP token bucket
  (120 req/min burst 40), with IP derivation preferring
  `CF-Connecting-IP`, falling back to `X-Forwarded-For`, then the
  ASGI socket peer.
- **If removed**: crypto-in-the-clear + trivial amplification. CSRF
  would still be blocked by OAuth + PKCE, but token interception
  during OAuth becomes viable.
- **Public fork**: mandatory. Caddy's auto-TLS is the default path;
  the Docker deployment plan keeps it as a sidecar container with
  outbound limited to the ACME endpoint.

### 3. HTTP security headers (CSP + defensive headers)

Attached by `SecurityHeadersMiddleware` on HTML responses only:

- **Content-Security-Policy.** Blocks inline script injection even
  if an XSS somehow slipped through the viewer's `escapeHtml` /
  `textContent` paths. Script sources limited to `self` + pinned
  `unpkg` CDN for MapLibre (SRI-integrity-checked). Style sources
  include Google Fonts.
- **X-Content-Type-Options: nosniff.** Stops browsers from
  MIME-sniffing a response.
- **X-Frame-Options: DENY.** No embedding.
- **Referrer-Policy: strict-origin-when-cross-origin.** Viewer URLs
  (48-bit session ids, now widened to 128-bit) don't leak to
  third-party origins on click.

Skipped on `/mcp`, `/api/*`, `/exports/*`, `/static/*`, `/.well-known/*`,
`/oauth/token`, `/oauth/register` (non-HTML).

- **If removed**: one viewer-side XSS bug becomes exploitable. Viewer
  URL leaks to third parties via the Referer header.
- **Public fork**: keep on by default. The CSP policy tolerates user-
  pasted basemap URLs (`img-src`, `connect-src` allow `https:` broadly);
  adjust if the fork tightens that surface.

### 4. Subresource Integrity on CDN scripts

MapLibre JS + CSS from `unpkg.com` pinned at 4.7.1, loaded with
`integrity="sha384-..."` `crossorigin="anonymous"`. If unpkg ever
serves a mutated asset the browser refuses to execute it.

- **Google Fonts CSS intentionally has no SRI.** The CSS body varies
  per-User-Agent (serves woff vs woff2), so SRI would break across
  browsers. CSP scopes what the fonts CSS can load (`font-src` is
  gstatic-only), which closes the same compromise path.
- **Public fork**: rehash if you bump MapLibre; trivial.

### 5. OAuth 2.1 + PKCE + invite code

- **Protects**: unauthenticated MCP access.
- `/oauth/authorize` renders a consent form that requires an
  invite code (shared secret, delivered via credstore). The
  code is compared with `hmac.compare_digest`, constant-time.
- Authorization codes are single-use (`_auth_codes.pop()`), 5-minute
  TTL.
- PKCE S256 verified with constant-time compare.
- Access tokens: 7-day TTL. Refresh tokens: 30-day TTL, **rotated on
  each use** (OAuth 2.1 BCP; a leaked refresh token is good for one
  exchange, not the full window).
- Tokens persist to `.duckdb/oauth.json` (mode 0600, atomic write)
  so a service restart doesn't force re-authorisation.
- **If the invite code is removed** (anyone can register): the MCP
  endpoint is effectively public, which for the current data is
  tolerable but not the intended posture.
- **Public fork**: the invite code is the hard gate. Per-user codes
  with independent revocation is a noted improvement for scale.

### 6. Session IDs

- 128 bits of entropy via `secrets.token_urlsafe(16)`. 22 URL-safe
  characters. Earlier versions used 48-bit `uuid4.hex[:12]`, which
  was brute-forceable over days with distributed IPs; raised to 128
  bits after the 2026-04-20 audit.
- The viewer and API endpoints (`/view/<sid>`, `/api/<sid>/...`) are
  gated **only** by knowledge of the session id. The `X-Robots-Tag`
  noindex header keeps them out of search.
- **If removed**: session enumeration is trivial and equals auth
  bypass for viewer data.

### 7. Per-request validators and escapers

- `_validate_sql` in `operations.py` parses via sqlglot, rejects
  non-SELECT/WITH/UNION, walks the AST to reject DDL/DML node types,
  denylists filesystem / extension / process intrinsics
  (`read_csv`, `read_parquet`, `read_text`, `read_blob`, `glob`,
  `attach`, `load_extension`, `install_extension`,
  `s3_register_secret`, `httpfs_register_secret`, …), rejects
  suspicious string literals (absolute paths, non-allowed URL
  schemes), caps numeric literals at 10M to block
  memory-exhaustion payloads.
- `_assert_predicate` wraps user predicates in a known-good SELECT
  context and parses, catching multi-statement payloads.
- `_quote_ident` on every identifier interpolation (layer/column
  names).
- `_EPSG_RE = r"EPSG:\d{4,6}"`: strict whitelist on CRS strings;
  remediates the 2026-04-18 HIGH finding against inline-load CRS
  input (`load(op="inline")`).
- `decode_unicode_escapes` on free-text user fields (title, notes,
  annotation values) to decode the over-escaped `\uXXXX` some MCP
  clients emit; handles surrogate pairs, rejects lone surrogates
  (otherwise downstream UTF-8 encoding breaks).
- Every display surface uses `textContent` or `escapeHtml`;
  verified no template injection into `docs.html` placeholders.
- **Public fork**: these are load-bearing. Any removal weakens the
  SQL sandbox directly.

### 8. Sandbox (systemd or Docker-equivalent)

This is the biggest layer and the one most likely to vary by
deployment. Both paths provide broadly the same posture; see the
parity table below.

#### What the sandbox protects against

- A validator bypass in layer 7 that lets an attacker run
  *something* in the Python process.
- The sandbox limits what that "something" can reach: which files,
  which syscalls, which network destinations, which credentials.

#### Systemd directives (current production)

See the unit at `/etc/systemd/system/geodata-mcp.service`:

| Directive | Purpose |
|---|---|
| `User=geodata-mcp` + dedicated group | Non-root, no shell, no sudoers |
| `NoNewPrivileges=true` | Block setuid/setgid + fscaps |
| `PrivateTmp=true` | Per-service tmpfs for `/tmp` |
| `PrivateDevices=true` | No `/dev/*` except `/dev/null` `/dev/zero` `/dev/urandom` |
| `ProtectHome=tmpfs` + `BindPaths=<project>` | Service's `/home` is a fresh tmpfs; only the project is bind-mounted in. `~/.ssh`, `~/.claude.json`, `~/.bash_history` invisible |
| `ProtectSystem=strict` | `/`, `/etc`, `/var`, `/usr` read-only |
| `InaccessiblePaths=/etc/ssh /etc/ssl/private /etc/shadow …` | Extra blackhole list for sensitive files |
| `ReadWritePaths=data/exports .duckdb` | Only these two writable |
| `ProtectKernelTunables/Modules/Logs=true` | No `/proc/sys` writes; no `init_module` |
| `ProtectControlGroups=true` | No cgroup manipulation |
| `ProtectClock=true` | No `adjtimex`, `clock_adjtime` |
| `ProtectHostname=true` | `sethostname` blocked |
| `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` | No raw socket families |
| `IPAddressDeny=any` + `IPAddressAllow=localhost` | No outbound egress |
| `RestrictNamespaces=true` | No new user/mount/net namespaces |
| `RestrictRealtime=true` | No realtime scheduling (CVE vector) |
| `RestrictSUIDSGID=true` | Block setuid binaries |
| `LockPersonality=true` | Block `personality()` syscall |
| `ProtectProc=invisible` | Hide other PIDs in `/proc` |
| `SystemCallArchitectures=native` | Block compat syscalls (x32) |
| `SystemCallFilter=~@mount @swap @reboot @debug @module @raw-io @cpu-emulation @obsolete bpf ptrace perf_event_open process_vm_readv process_vm_writev userfaultfd keyctl add_key request_key kexec_load kexec_file_load` | Seccomp denylist |
| `CapabilityBoundingSet=` + `AmbientCapabilities=` | Drop every capability |
| `LoadCredential=mcp-token:/etc/credstore/…` `LoadCredential=invite-code:…` | Secrets delivered via tmpfs under `/run/credentials/<unit>`, never in `/proc/<pid>/environ` |

Deliberately omitted:

- `SystemCallFilter=@system-service`: allowlist too strict; DuckDB
  spatial breaks under it. Denylist is the pragmatic choice.
- `ProcSubset=pid`: GDAL's `/proc/cpuinfo` SIMD probe breaks.
- `MemoryDenyWriteExecute=true`: Python ctypes / GDAL JIT needs it.

#### Docker parity (planned, see `docker-deployment-plan.md`)

| systemd | Docker equivalent |
|---|---|
| `User=` non-root | `user: "65532:65532"` |
| `NoNewPrivileges=true` | `security_opt: [no-new-privileges:true]` |
| `ProtectSystem=strict` | `read_only: true` |
| `PrivateTmp` + `ProtectHome=tmpfs` | `tmpfs: [/tmp]`, no `/home` in image |
| `ReadWritePaths=` | explicit volume mounts at specific paths, nothing else |
| `IPAddressDeny=any` + `IPAddressAllow=localhost` | custom network with `internal: true` (no default route); DNS still works inside the bridge |
| `SystemCallFilter=~@mount @swap …` | `security_opt: [seccomp:./seccomp.json]`; custom JSON mirrors the denylist |
| `CapabilityBoundingSet=` | `cap_drop: [ALL]` |
| `LoadCredential=` | compose `secrets:` block, `CREDENTIALS_DIRECTORY=/run/secrets` (existing `_load_secret()` picks them up without code change) |
| `RestrictAddressFamilies=` | seccomp profile blocks other address families |
| `ProtectKernelTunables` etc. | covered by non-root + cap-drop + seccomp |
| `ProtectProc=invisible` | Docker masks `/proc/kcore` etc. by default; non-root user can't read inter-PID `environ` |

Two honest differences:

1. **`MemoryDenyWriteExecute`.** Still off in Docker for the same
   Python / GDAL reason.
2. **`ProtectProc=invisible`** has no 1:1 Docker directive; the
   container's `/proc` is masked by Docker defaults which are close
   but not identical.

#### Which directives are load-bearing, ordered

A public-fork operator asking "which of these can I drop safely?"
has a partial ordering. Drop in reverse order of security impact:

1. `ProtectClock`, `ProtectHostname`, `LockPersonality`,
   `RestrictRealtime`: niche kernel surfaces; minimal loss.
2. `PrivateDevices`, `RestrictNamespaces`: hardening against a
   compromised-root exploit; the non-root user makes these
   mostly redundant.
3. `InaccessiblePaths`: helpful defense-in-depth; if
   `ProtectSystem=strict` + `ReadWritePaths=` is honoured, the
   extra blackhole list is duplicative.
4. `ProtectKernelTunables/Modules/Logs`, `ProtectControlGroups`:
   only meaningful if the attacker gains some privileged capability.
   With `CapabilityBoundingSet=` empty, these rarely fire.
5. `ProtectProc=invisible`: helpful but not load-bearing.
6. **Keep** `ProtectSystem=strict`, `ProtectHome=tmpfs`,
   `ReadWritePaths=`, non-root user, `NoNewPrivileges=true`,
   `CapabilityBoundingSet=` empty, the seccomp filter,
   `IPAddressDeny=any`, and `LoadCredential=` (or equivalent
   secret delivery that doesn't put tokens in `environ`). These
   are the core.

---

## Bypass exposure: what a validator-bypass attacker could still do

Assume the worst: the attacker escapes `_validate_sql` and can run
arbitrary Python in the service process. What happens next depends
on the sandbox.

- **File-system reads**: limited to the bind-mounted `data/`,
  `.duckdb/`, the Python install, and whatever the sandbox leaves
  readable. `/etc/credstore`, `/etc/shadow`, `/root`, and
  `~/.ssh` are invisible.
- **File-system writes**: only `data/exports/` and `.duckdb/`. No
  write to `/tmp` beyond the per-service tmpfs.
- **Process/credential harvest**: credentials are delivered via
  `LoadCredential=`, so they live only under
  `$CREDENTIALS_DIRECTORY` (tmpfs) and **not** in
  `/proc/self/environ`. An attacker in the process can still read
  them, so the outbound-egress deny is the second layer that
  prevents exfiltration.
- **Lateral network**: egress denied. The attacker cannot reach a
  C2. They can bounce off localhost, but nothing local-to-the-box
  is useful.
- **Kernel escalation**: seccomp blocks `bpf`, `ptrace`,
  `perf_event_open`, the `@module` family, `kexec`, and the
  mount/swap/reboot sets. Most local-priv-esc CVEs need one of
  these; the ones that don't are rare.

The net shape: an attacker with a validator bypass can read/write
the session's DuckDB files and that's roughly it. Not nothing, but
well short of host compromise.

---

## Known accepted risks

- **Session-as-bearer for the viewer API.** `/api/<sid>/*` is gated
  only by knowledge of the session id. 128-bit entropy makes
  enumeration infeasible, but referer-header leaks, shoulder-surf,
  or deliberate sharing expose the session. Acceptable because the
  data is public; would need rethinking if non-open data is ever
  hosted.
- **Tokens stored verbatim in `oauth.json`.** A file leak (mode 0600
  but same zone as other state) equals a token leak within the
  TTL window. HMAC-hashed lookups would tighten; deferred.
- **Markdown rendered with default-escape-HTML.** Raw HTML in doc
  files is escaped. A malicious operator-authored `.md` can't
  inject via inline HTML. If extensions change, re-audit.
- **Google Fonts without SRI.** UA-varied CSS makes SRI impractical;
  CSP `font-src https://fonts.gstatic.com` is the mitigation.
- **Custom basemap URL from `localStorage`.** User-pasted; MapLibre
  treats it as JSON/image; the CSP allows broad `https:` for
  `img-src` and `connect-src` so this doesn't break. The trade-off
  is a looser CSP than we'd otherwise pick.

---

## What changed in the 2026-04-20 audit

All findings addressed in commits leading up to this doc:

- **MEDIUM: session id entropy 48 → 128 bits** (session.py).
- **MEDIUM: refresh-token rotation.** OAuth 2.1 BCP. Each refresh
  grants a new RT and invalidates the old one.
- **MEDIUM: CSP + security headers middleware** on HTML responses.
- **MEDIUM: SRI on MapLibre CDN assets**.
- **LOW: TOCTOU on export serve.** File-race returns 410 instead of
  500.
- **LOW: OAuth store load failures are now logged** to stderr.

Previous HIGH fixed earlier this cycle:

- SQL injection in the inline-load `crs` parameter (then `create_layer`,
  now folded into `load(op="inline")`): regex whitelist (commit 36bf3cb).

No HIGH or MEDIUM findings remain.

---

## Checklist for a public-fork operator

Minimum viable hardened deployment on a fresh Linux VPS:

- [ ] Reverse proxy (Caddy, nginx, or Traefik) terminates TLS.
      **Do not** expose the app port (8765) directly.
- [ ] The app binds to `127.0.0.1` or loopback-only inside a
      container.
- [ ] The service runs as a non-root user with no shell and no
      sudoers entry.
- [ ] File-system write access limited to `data/exports/` and
      `.duckdb/`; everything else read-only.
- [ ] Outbound network egress is denied by default. Bootstrap
      scripts (`scripts/fetch_basemap.py`, `scripts/normalize.py`)
      run in a separate one-shot context with temporary egress.
- [ ] Invite code and legacy bearer delivered through a credential
      manager (systemd `LoadCredential=`, Docker secrets), **not**
      via environment variables.
- [ ] `.duckdb/oauth.json` and `.duckdb/sessions/` are on persistent
      storage that survives container / service restarts.
- [ ] CSP + other security headers are served on HTML responses (the
      in-app middleware does this by default; if you front with a
      different proxy, don't disable).
- [ ] Session data dir is backed up if the analyses matter; the
      soft-30-min / hard-14-day TTL is documented.
- [ ] Cloudflare or another CDN in front is recommended but
      optional.

The native systemd path (`deploy/systemd/geodata-mcp.service.example`,
planned) and the Docker compose path (`deploy/docker/compose.yml`,
planned per `docker-deployment-plan.md`) both provide this posture
out of the box.
