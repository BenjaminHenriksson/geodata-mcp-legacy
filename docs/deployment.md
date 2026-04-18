# Deployment — `geo.benjaminhenriksson.com`

How the Phase 1 MCP server runs in production. Captured 2026-04-18.

- **VPS:** Hetzner CX23 in Helsinki (`ubuntu-4gb-hel1-1`, 2 vCPU, 4 GB RAM, 40 GB SSD).
- **Edge:** Cloudflare proxy (orange cloud), TLS terminated at CF, re-established to origin.
- **Reverse proxy:** Caddy already installed as a system service, serving `benjaminhenriksson.com` (root domain portfolio site).
- **App:** `geodata_mcp` Python process, owned by `ben`, managed by systemd.
- **SSH access:** Tailscale only. No port 22 on the public internet.

---

## Request flow

```
MCP client / browser
  │  HTTPS (Cloudflare-issued cert)
  ▼
Cloudflare proxy  (geo.benjaminhenriksson.com  A → CF anycast IP, proxied)
  │  HTTPS (Let's Encrypt cert Caddy issued via HTTP-01)
  ▼
Caddy on VPS :443
  │   ├── /mcp/*   → bearer-token check → reverse_proxy 127.0.0.1:8765
  │   └── /*       → reverse_proxy 127.0.0.1:8765   (viewer, /api/…, /static/…)
  ▼
geodata-mcp.service  (uvicorn / FastMCP on 127.0.0.1:8765, bound to loopback only)
```

The Python app binds **only** to `127.0.0.1`, so Caddy is the only thing that can reach it. Public exposure is entirely through Caddy.

---

## Files on the VPS

| Path | Owner | Purpose |
|---|---|---|
| `/home/ben/geodata-mcp/` | `ben` | Source + data + venv |
| `/etc/systemd/system/geodata-mcp.service` | `root` | systemd unit for the app |
| `/etc/geodata-mcp.env` | `root:caddy` (0640) | Bearer token env var |
| `/etc/systemd/system/caddy.service.d/geodata-token.conf` | `root` | Caddy drop-in loading the env file |
| `/etc/caddy/Caddyfile` → `/home/ben/personal-homepage/Caddyfile` | `ben` | Caddy site configs (symlinked) |
| `/var/lib/caddy/.local/share/caddy/certificates/acme-v02…/geo.benjaminhenriksson.com/` | `caddy` | Auto-provisioned LE cert |

### `geodata-mcp.service`

The unit runs as a dedicated `geodata-mcp` system user (not `ben`) inside a
heavily sandboxed namespace: filesystem blocked except the project dir,
cgroup-BPF egress filter restricting outbound to loopback only, seccomp
denylist blocking kernel-exploit primitives. Core shape:

```ini
[Service]
Type=simple
User=geodata-mcp
Group=geodata-mcp
SupplementaryGroups=
UMask=0002
WorkingDirectory=/home/ben/geodata-mcp
Environment=HOME=/home/ben/geodata-mcp
Environment=PATH=/home/ben/geodata-mcp/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/ben/geodata-mcp/.venv/bin/python -m geodata_mcp --http --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=3

# Filesystem sandbox
ProtectHome=tmpfs
BindPaths=/home/ben/geodata-mcp
ProtectSystem=strict
ReadWritePaths=/home/ben/geodata-mcp/data/exports /home/ben/geodata-mcp/.duckdb
InaccessiblePaths=/etc/ssh /etc/ssl/private /etc/shadow /etc/gshadow /etc/sudoers /etc/sudoers.d /etc/caddy /etc/geodata-mcp.env /etc/systemd /root /var/log
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true

# Egress (cgroup BPF)
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=localhost

# Process + seccomp
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
SystemCallArchitectures=native
SystemCallFilter=~@mount @swap @reboot @debug @module @raw-io @cpu-emulation @obsolete bpf ptrace perf_event_open process_vm_readv process_vm_writev userfaultfd keyctl add_key request_key kexec_load kexec_file_load

[Install]
WantedBy=multi-user.target
```

The authoritative version is the one on the host at
`/etc/systemd/system/geodata-mcp.service` — always prefer reading that over
trusting this snippet.

Enabled at boot. The process exec's the project venv directly
(`/home/ben/geodata-mcp/.venv/bin/python`); nothing is rebuilt at service
start. Source code is owned `ben:ben` read-only to the service user, so RCE
inside the process cannot patch Python files. `data/exports` and `.duckdb`
are group `geodata-mcp` with setgid + default ACL `g::rwX` so both `ben`
(via group membership) and the service can read/write them.

### Caddy site block (in `~/personal-homepage/Caddyfile`)

```caddy
geo.benjaminhenriksson.com {
    encode zstd gzip

    @mcp path /mcp /mcp/*
    handle @mcp {
        @unauthed not header Authorization "Bearer {$GEODATA_MCP_TOKEN}"
        handle @unauthed {
            respond "Unauthorized" 401
        }
        reverse_proxy 127.0.0.1:8765 {
            # Streamable HTTP is long-poll; don't let Caddy buffer responses.
            flush_interval -1
        }
    }

    handle {
        reverse_proxy 127.0.0.1:8765
    }
}
```

### Caddy env drop-in (`/etc/systemd/system/caddy.service.d/geodata-token.conf`)

```ini
[Service]
EnvironmentFile=/etc/geodata-mcp.env
```

The env file exposes `GEODATA_MCP_TOKEN` to the Caddy process so the `{$GEODATA_MCP_TOKEN}` placeholder in the Caddyfile resolves. When the token is rotated, **restart** Caddy (not reload — reload doesn't re-read `EnvironmentFile`).

---

## DNS + TLS

- Cloudflare DNS record: `geo  A  <VPS IP>`, **proxy ON** (orange cloud). IP is the same as the root-domain A record; Cloudflare anycasts everything.
- Cloudflare SSL/TLS mode: same as root (`Full` or `Full (strict)`) — Caddy terminates TLS on the origin with a real LE cert.
- Caddy's automatic HTTPS tries `tls-alpn-01` first → fails (Cloudflare proxy can't forward the `acme-tls/1` ALPN) → falls back to `http-01` → Cloudflare transparently proxies `/.well-known/acme-challenge/*` on port 80 → cert issued. Renewals happen automatically.

A single failed-then-succeeded pair of lines per 60-day renewal cycle is normal; the fallback does the job. If you want a tidy log, pin `http-01` in the site block:

```caddy
tls {
    issuer acme {
        disable_tlsalpn_challenge
    }
}
```

---

## Authentication

- **MCP endpoint `/mcp/*`** — `Authorization: Bearer <token>` required, enforced by Caddy reading `GEODATA_MCP_TOKEN` from `/etc/geodata-mcp.env`. Wrong or missing token returns `401` before hitting the app.
- **Viewer + API (`/view/*`, `/api/*`, `/static/*`)** — **open**. Knowing the session id is the only access control. Phase 1 uses a hardcoded `"default"` session, so anyone who finds the hostname can read whatever you've `show()`ed. Tighten before sharing broadly (see [Known limitations](#known-limitations)).

### Retrieve the token

```bash
sudo grep GEODATA_MCP_TOKEN /etc/geodata-mcp.env
```

### Rotate the token

```bash
NEW=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
echo "GEODATA_MCP_TOKEN=$NEW" | sudo tee /etc/geodata-mcp.env >/dev/null
sudo systemctl restart caddy     # reload does NOT re-read EnvironmentFile
```

Then update every MCP-client config with the new token.

---

## MCP client configuration

```json
{
  "mcpServers": {
    "geodata": {
      "url": "https://geo.benjaminhenriksson.com/mcp",
      "transport": {
        "type": "http",
        "headers": {
          "Authorization": "Bearer <TOKEN>"
        }
      }
    }
  }
}
```

FastMCP's `transport="http"` is the **Streamable HTTP** transport (single endpoint, long-poll). Streamable HTTP is request/response-shaped, so Cloudflare's 100 s origin-response cap is not an issue for the tool calls in Phase 1 (all <1 s).

---

## Operating the service

| Task | Command |
|---|---|
| Status | `systemctl status geodata-mcp` |
| Start / stop / restart | `sudo systemctl {start,stop,restart} geodata-mcp` |
| Live logs | `journalctl -u geodata-mcp -f` |
| Recent errors | `journalctl -u geodata-mcp --since '1 hour ago' -p err` |
| Deploy new code | `cd ~/geodata-mcp && git pull && uv sync && sudo systemctl restart geodata-mcp` |
| Deploy new catalog only | edit `catalog.json` → `sudo systemctl restart geodata-mcp` (catalog is loaded at startup) |
| Caddy status | `systemctl status caddy` |
| Caddy logs | `journalctl -u caddy -f` |
| Caddy reload (config only) | `sudo systemctl reload caddy` |
| Caddy restart (env file changed) | `sudo systemctl restart caddy` |

### Regenerate normalized data

When raw `data/scb_deso/` or `data/stockholm_sbk/` change:

```bash
cd ~/geodata-mcp
uv run --with pandas --with pyarrow python scripts/normalize.py
sudo systemctl restart geodata-mcp
```

---

## Health check

From the VPS (bypasses Cloudflare):

```bash
# App reachable on loopback:
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/view/default

# Caddy terminating TLS correctly (Host header forces the right site):
curl -sk --resolve geo.benjaminhenriksson.com:443:127.0.0.1 \
     -o /dev/null -w '%{http_code}\n' https://geo.benjaminhenriksson.com/view/default
```

From anywhere:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://geo.benjaminhenriksson.com/view/default  # 200
curl -s -o /dev/null -w '%{http_code}\n' https://geo.benjaminhenriksson.com/mcp              # 401
TOKEN=...   # from /etc/geodata-mcp.env
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
     https://geo.benjaminhenriksson.com/mcp                                                    # != 401
```

---

## Security posture

The MCP exposes a DuckDB SQL surface to LLM-driven callers, so defense assumes
an adversarial prompt. Three layers of defense:

### Layer 1 — sqlglot allowlist + denylist on `execute_sql`
In `geodata_mcp/operations.py::_validate_sql`:
- **Statement type**: only `SELECT` / `WITH` / `UNION` / `Subquery`. Rejects `INSERT`/`UPDATE`/`DELETE`/`CREATE`/`DROP`/`ALTER`/`MERGE`/`COPY`/`ATTACH`/`DETACH` anywhere in the tree.
- **Function denylist**: rejects calls to any filesystem/network/extension function: `read_csv`, `read_parquet`, `read_json`, `read_blob`, `read_text`, `glob`, `parquet_scan`, `parquet_schema`, `json_scan`, `csv_sniffer`, `load_extension`, `install_extension`, `httpfs_register_secret`, etc. (full list in `_DISALLOWED_FUNCTIONS`).
- **String-literal checks**: rejects any string literal starting with `http://`, `https://`, `ftp://`, `file://`, `s3://`, `gs://`, `azure://`, etc. Also rejects absolute paths into `/etc`, `/home`, `/root`, `/var`, `/proc`, `/sys`, `/boot`, `/usr`, `/opt`, `/srv`, `/run`, `/tmp`.
- **DoS caps**: rejects any numeric literal > 10 million (blocks `repeat('a', 1e10)`, `generate_series(0, 1e10)` before DuckDB allocates).

### Layer 2 — DuckDB per-session resource limits
- `memory_limit='256MB'`, `threads=2` set at every session start.
- 30-second wall-clock timeout per `execute_sql` via `threading.Timer` + `conn.interrupt()`.

### Layer 3 — systemd sandbox (identity, filesystem, egress, syscalls)
The service runs as a **dedicated `geodata-mcp` system user** (no shell, no
home) in a restricted mount namespace, with cgroup-BPF egress filtering and
a seccomp syscall denylist:

- **Identity.** `User/Group=geodata-mcp`, `SupplementaryGroups=` (no
  inherited groups). Source code owned `ben:ben` read-only to the service —
  RCE inside the process cannot patch its own code. `ben` is in the
  `geodata-mcp` group so dev workflow on the host still works.
- **Filesystem.** `ProtectHome=tmpfs` + `BindPaths=/home/ben/geodata-mcp` —
  only the project dir is visible under `/home`. `ProtectSystem=strict`
  with minimal `ReadWritePaths`. `InaccessiblePaths` for `/etc/ssh`,
  `/etc/ssl/private`, `/etc/shadow`, `/etc/caddy`, `/etc/geodata-mcp.env`,
  `/etc/systemd`, `/root`, `/var/log`.
- **Egress.** `IPAddressDeny=any` + `IPAddressAllow=localhost` — kernel
  cgroup-BPF filter restricts outbound to `127.0.0.0/8` / `::1`. Blocks
  SSRF against `169.254.169.254`, HTTPS exfiltration, DuckDB extension
  registry fetches.
- **Syscalls.** `SystemCallArchitectures=native` blocks the x86-32 compat
  ABI. `SystemCallFilter=~@mount @swap @reboot @debug @module @raw-io
  @cpu-emulation @obsolete bpf ptrace perf_event_open process_vm_readv
  process_vm_writev userfaultfd keyctl add_key request_key kexec_load
  kexec_file_load` — denylist blocking container-escape, cross-process
  memory snooping, keyring access, live kernel replacement. Verified not
  to break DuckDB spatial (see security.md smoke test).
- **Process.** `PrivateTmp`, `PrivateDevices`, `ProtectKernel{Tunables,Modules,Logs,ControlGroups}`,
  `ProtectClock`, `ProtectHostname`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`,
  `RestrictNamespaces`, `RestrictSUIDSGID`, `LockPersonality`,
  `NoNewPrivileges`, `CapabilityBoundingSet=` (all dropped).

`systemd-analyze security geodata-mcp` reports **1.8 OK** (lower is tighter).

### Verification

From inside the service's mount namespace, the audited secrets are not reachable:

```
sudo nsenter -m -t $(systemctl show geodata-mcp --property=MainPID --value) \
  cat /home/ben/.claude.json
# → cat: /home/ben/.claude.json: No such file or directory
```

And from outside, via MCP `execute_sql`:

```python
await c.call_tool("execute_sql", {"sql": "SELECT * FROM read_csv('/etc/hostname')"})
# → {"error": "sql_rejected", "detail": "disallowed function: read_csv. …"}
```

### Deliberately omitted hardening (breaks DuckDB-spatial)

- `SystemCallFilter=@system-service` (allowlist form) — DuckDB spatial's
  GDAL/PROJ loader hit a denied syscall and segfaulted. Replaced with the
  **denylist** form (`SystemCallFilter=~...`) above, which targets exploit
  primitives without breaking the spatial extension.
- `ProtectProc=invisible` + `ProcSubset=pid` — broke library initialization
  that reads `/proc/cpuinfo`.
- `MemoryDenyWriteExecute=true` — incompatible with Python's ctypes/JIT
  paths.

Roadmap for further hardening: OAuth 2.1 + PKCE in place of the shared
bearer token, and an audit log of `execute_sql` calls forwarded out of the
sandbox for tamper-proof probing detection.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl geo.benjaminhenriksson.com` → `000` / `SSL_ERROR_RX_RECORD_TOO_LONG` | DNS not yet in CF, or Caddy cert not yet issued | Check `dig geo.benjaminhenriksson.com`. Check `journalctl -u caddy --since '10min ago' \| grep -i acme`. |
| `502 Bad Gateway` from Caddy | App not running | `systemctl status geodata-mcp`; `journalctl -u geodata-mcp` |
| `401` even with the right token | Env var not loaded into Caddy | `sudo systemctl restart caddy` (reload doesn't re-read `EnvironmentFile`) |
| App starts but exits immediately | Python/deps mismatch | `cd ~/geodata-mcp && uv sync`, then restart |
| Viewer loads but has no features | No layers `show()`ed in the session, OR session id in URL doesn't match `SESSION.id` | Call `show([...])` via an MCP client; viewer URL is `/view/<session_id>` |
| Coordinates look off in the viewer (Africa, Arctic) | Axis-order regression in `ST_Transform` | Confirm `server.py` uses `ST_Transform(..., 'EPSG:3011', 'EPSG:4326', true)` (the `true` is `always_xy`) |
| Caddy log has TLS-ALPN failure every cert renewal | Cloudflare proxy can't forward `acme-tls/1`; HTTP-01 fallback still works | Either ignore (benign), or add `disable_tlsalpn_challenge` in the site's `tls` block |

---

## Known limitations (Phase 1)

1. **Session id is hardcoded `"default"`**. All tool calls land in the same session; `show()` is globally visible. Phase 4 moves to per-MCP-connection sessions keyed by UUID.
2. **Viewer is unauthenticated**. Any visitor to the subdomain gets whatever is `show()`ed. Tighten options, in increasing order of friction:
   - Make session ids UUIDs (secret-by-obscurity; works today without login).
   - Bearer-gate `/view/*` and `/api/*` too; client-side JS prompts for token and stores in `localStorage`, adds as `Authorization` header on fetches.
   - Put Cloudflare Zero Trust Access in front of the subdomain (free tier, SSO login, no custom client code).
3. **No rate limiting**. A misbehaving client can spam tool calls. Either add FastMCP middleware or Caddy's `rate_limit` plugin.
4. **No session GC**. Session state accumulates in memory until the process restarts. Phase 4 adds idle GC + operation-log persistence.
5. **Single-instance**. One Python process. Restarts drop all in-memory sessions. Fine for a portfolio demo, not for multi-user production.

---

## Provisioning from scratch

If you ever redo this on a fresh VPS, the order that matters:

```bash
# 0. System prereqs (Caddy is already there in this setup)
sudo apt install -y python3 git curl build-essential libgdal-dev
curl -LsSf https://astral.sh/uv/install.sh | sh    # uv as user `ben`

# 1. Code + deps
git clone <repo> ~/geodata-mcp   # or rsync from dev machine
cd ~/geodata-mcp
uv sync

# 2. Data — if not copied across, regenerate from raw
uv run --with pandas --with pyarrow python scripts/normalize.py

# 3. Bearer token + env file
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
echo "GEODATA_MCP_TOKEN=$TOKEN" | sudo tee /etc/geodata-mcp.env >/dev/null
sudo chown root:caddy /etc/geodata-mcp.env
sudo chmod 0640 /etc/geodata-mcp.env

# 4. systemd unit (contents as above)
sudo tee /etc/systemd/system/geodata-mcp.service <<'EOF'
...
EOF
sudo mkdir -p /etc/systemd/system/caddy.service.d
sudo tee /etc/systemd/system/caddy.service.d/geodata-token.conf <<'EOF'
[Service]
EnvironmentFile=/etc/geodata-mcp.env
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now geodata-mcp
sudo systemctl restart caddy

# 5. Caddy site block (append geo.benjaminhenriksson.com {...} to the Caddyfile)
#    then: sudo systemctl reload caddy

# 6. Cloudflare: add A record `geo` → VPS IP, proxy ON
#    Caddy auto-issues cert via HTTP-01 within ~30 s of first request.
```
