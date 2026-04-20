# Docker deployment plan

Design notes for a containerised deployment that preserves the current
systemd hardening posture as the default. For discussion before
implementing. When approved, this becomes the implementation brief and
can be deleted after the work ships.

---

## Goals

- **One-command deploy on any modern Linux VPS** with Docker installed:
  `cp secrets.template secrets/` → fill in two files → `docker compose up -d`.
- **Maximum hardening out of the box.** No-egress network on the app,
  read-only root FS, non-root user, cap-drop-all, custom seccomp, no
  new privileges. Technical users relax where they need to.
- **TLS handled by a sidecar Caddy container** (auto-ACME, transparent
  to the operator). The app itself never faces the public internet.
- **Preserve the native-systemd path** as an alternative for operators
  who prefer it, via `deploy/systemd/geodata-mcp.service.example`. No
  deprecation.

---

## Proposed layout

```
deploy/
├── docker/
│   ├── Dockerfile              multi-stage build, see spec below
│   ├── compose.yml             default hardened compose
│   ├── Caddyfile               TLS + reverse proxy to app
│   ├── seccomp.json            custom seccomp profile (denylist)
│   ├── .env.example            documented env vars with defaults
│   ├── secrets/
│   │   ├── .gitignore          ignore *.txt (real secret files)
│   │   └── README.md           how to populate invite-code + mcp-token
│   └── README.md               deploy walk-through
└── systemd/
    ├── geodata-mcp.service.example   sanitised current unit
    └── README.md               native-install walk-through
```

`deploy/docker/` becomes the documented default; `deploy/systemd/` is
left for operators who prefer native.

---

## Container design

### Two services, hardened differently

#### `app` — the MCP server

Runs as non-root (UID 65532), read-only root filesystem, no capabilities,
custom seccomp, tmpfs for `/tmp`. **No outbound network route.** Bound
to the internal docker bridge only; cannot reach the public internet at
runtime. Talks to Caddy over the internal bridge.

Writable volumes (mounted at specific paths, nothing else):

- `geodata-sessions` → `/app/.duckdb/` — session DuckDB files, sidecars,
  OAuth state store.
- `geodata-exports`  → `/app/data/exports/` — per-request export files,
  24-hour TTL.
- `geodata-basemap`  → `/app/data/basemap/` — Carto tile cache
  (read-heavy; mounted ro from the app's perspective after warmup).
- `/app/data/normalized/` — mounted read-only from a host path or a
  pre-populated named volume. Contains the SCB/SBK/OSM parquet+gpkg.

Secrets (read-only):

- `invite-code` and `mcp-token` mounted at `/run/secrets/<name>` via
  compose's native `secrets:` block. `CREDENTIALS_DIRECTORY=/run/secrets`
  is set in the app env, so the existing `_load_secret()` helper in
  `oauth.py` picks them up without code changes.

Network:

- Attached only to an internal bridge network named `mcp-internal`.
- No default gateway on that bridge (achieved via a Docker custom
  network with `internal: true`, or by disabling IP masquerade for
  that bridge and dropping egress via iptables in `docker-compose`
  extension hooks — docker's internal networks are the cleaner path).

#### `caddy` — TLS terminator + reverse proxy

Runs on the public internet side. Two networks: `mcp-edge` (exposes
80/443 to the host and thus to the internet) and `mcp-internal` (shared
with the app). Has outbound because it needs to reach Let's Encrypt's
ACME endpoint. Still dropped caps where possible; official `caddy:2`
image already runs as non-root for serving.

Config: a minimal `Caddyfile` that auto-manages TLS from `${PUBLIC_DOMAIN}`
and reverse-proxies to `app:8765`.

### Hardening parity — systemd → Docker

| Systemd directive | Docker equivalent |
|---|---|
| `User=geodata-mcp` | `user: "65532:65532"` |
| `NoNewPrivileges=true` | `security_opt: [no-new-privileges:true]` |
| `ProtectSystem=strict` | `read_only: true` |
| `PrivateTmp=true` | `tmpfs: [/tmp:size=64m,noexec,nosuid,nodev]` |
| `ProtectHome=tmpfs` | no `/home` in image — covered by build |
| `ReadWritePaths=data/exports .duckdb` | explicit volume mounts on those paths, everything else read-only |
| `InaccessiblePaths=/etc/ssh /etc/ssl/private …` | not present in image |
| `IPAddressDeny=any` | `networks: [mcp-internal]` where `internal: true` — no default route |
| `IPAddressAllow=localhost` | loopback in container works unchanged |
| `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` | seccomp profile blocks other AF_* syscalls |
| `CapabilityBoundingSet=` (empty) | `cap_drop: [ALL]` |
| `ProtectKernelTunables=true` etc. | not userspace-reachable in a non-root container; seccomp + no-caps covers the rest |
| `SystemCallFilter=~@mount @swap @reboot @debug @module @raw-io @cpu-emulation @obsolete bpf ptrace perf_event_open …` | `security_opt: [seccomp:./seccomp.json]` with custom profile |
| `ProtectProc=invisible` | Docker's default `/proc` masking covers most; non-root user can't read other processes' `/proc/<pid>/environ` regardless |
| `LoadCredential=*` | compose `secrets:` block, mounted read-only at `/run/secrets/` |

Result: essentially equivalent to the current systemd posture, with
two differences worth acknowledging:

1. **`ProtectProc=invisible` isn't a Docker primitive.** Docker masks
   specific `/proc` paths (`/proc/kcore`, etc.) by default and hides
   inter-PID `/proc/<pid>/environ` from non-root users, which covers
   the spirit of the directive without the exact semantics.
2. **`MemoryDenyWriteExecute=true` is still deliberately off.** Same
   reason as the systemd config — Python ctypes and GDAL JIT need it.

### Dockerfile shape

Multi-stage, roughly 80 lines:

- **Stage 1 (builder):** `python:3.12-slim-bookworm`. Installs `uv`,
  runs `uv sync --frozen --no-dev`, and pre-warms the DuckDB spatial
  extension by `INSTALL spatial; LOAD spatial;` once during build so
  the runtime container doesn't need outbound to fetch it.
- **Stage 2 (runtime):** `python:3.12-slim-bookworm` with `tini` as
  PID 1 for clean SIGTERM propagation. Copies `/opt/venv` + the
  pre-warmed `/opt/duckdb-extensions`. Creates the non-root `app`
  user (UID 65532) and the mount-point directories with owner+group
  set so volume writes work at runtime.

No GDAL system package needed — DuckDB spatial bundles what it uses.
If something surfaces at runtime, `libgdal-*` gets added to the slim
base in a small follow-up.

### Compose shape

Single `compose.yml`. Key structure (illustrative, not final):

```yaml
networks:
  mcp-internal:
    internal: true           # no default gateway → no egress
  mcp-edge: {}               # default bridge, for caddy only

volumes:
  geodata-sessions: {}
  geodata-exports:  {}
  geodata-basemap:  {}
  caddy-data:       {}
  caddy-config:     {}

secrets:
  invite-code:
    file: ./secrets/invite-code
  mcp-token:
    file: ./secrets/mcp-token

services:
  app:
    build:
      context: ../..
      dockerfile: deploy/docker/Dockerfile
    read_only: true
    user: "65532:65532"
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
      - seccomp:./seccomp.json
    tmpfs:
      - /tmp:size=64m,noexec,nosuid,nodev
    networks: [mcp-internal]
    volumes:
      - geodata-sessions:/app/.duckdb
      - geodata-exports:/app/data/exports
      - geodata-basemap:/app/data/basemap
      - ./data/normalized:/app/data/normalized:ro  # or named volume
    secrets: [invite-code, mcp-token]
    environment:
      CREDENTIALS_DIRECTORY: /run/secrets
      GEODATA_PUBLIC_URL: https://${PUBLIC_DOMAIN}
    restart: unless-stopped

  caddy:
    image: caddy:2
    read_only: true
    cap_drop: [ALL]
    cap_add: [NET_BIND_SERVICE]      # needed for :80, :443
    security_opt: [no-new-privileges:true]
    tmpfs: [/tmp]
    networks: [mcp-internal, mcp-edge]
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"                 # HTTP/3
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    environment:
      PUBLIC_DOMAIN: ${PUBLIC_DOMAIN}
    restart: unless-stopped
```

One-shot compose services (`docker compose run --rm …`) for the
outbound-network operations:

- `fetch-basemap` — runs `scripts/fetch_basemap.py`, writes to the
  `geodata-basemap` volume, exits. Gets a one-time egress-enabled
  network just for the run.
- `normalize` — runs `scripts/normalize.py` to build the catalog from
  raw SCB/SBK/OSM sources. Same pattern: one-shot, needs egress.

### Caddyfile

Minimal:

```
{$PUBLIC_DOMAIN} {
    encode zstd gzip
    header X-Robots-Tag "noindex, nofollow, noarchive, nosnippet"

    @mcp path /mcp /mcp/*
    handle @mcp {
        reverse_proxy app:8765 {
            flush_interval -1
        }
    }

    handle /robots.txt {
        header Content-Type "text/plain"
        respond "User-agent: *\nDisallow: /\n" 200
    }

    handle {
        reverse_proxy app:8765
    }
}
```

### seccomp profile

Start from Docker's default whitelist and add extra deny rules to
mirror the systemd filter (`bpf`, `ptrace`, `perf_event_open`,
`process_vm_readv`, `process_vm_writev`, `userfaultfd`, `keyctl`,
`add_key`, `request_key`, `kexec_load`, `kexec_file_load`,
`swap*`, `mount`, `umount`, `reboot`, `delete_module`, `init_module`,
and the rest already on Docker's default deny list). ~40 lines of
JSON.

---

## Code changes needed

### `geodata_mcp/oauth.py`

No change strictly needed — the existing `_load_secret()` already
reads from `$CREDENTIALS_DIRECTORY`. Compose sets
`CREDENTIALS_DIRECTORY=/run/secrets`, docker secrets land with
filenames matching `invite-code` and `mcp-token`. Works as-is.

### `geodata_mcp/server.py`

Currently binds with `--host 127.0.0.1` as the systemd default. In the
container we bind to `0.0.0.0:8765` because Caddy (a different
container) reaches us over the internal bridge. The CLI already
accepts `--host`, so this is just an env-driven argument default or
a compose-level override — no code change.

### Scripts

`scripts/fetch_basemap.py` and `scripts/normalize.py` already run as
scripts. They get exposed via one-shot compose services.

---

## What can the public release reuse?

All of this. The `deploy/docker/` tree is generic except for
`Caddyfile`'s `{$PUBLIC_DOMAIN}` placeholder. A public fork needs to:

1. Populate `deploy/docker/secrets/invite-code` and `mcp-token`.
2. Set `PUBLIC_DOMAIN=yourhost.example` in `.env`.
3. Optionally run `docker compose run --rm normalize` if they want to
   rebuild the catalog from scratch (else ship `data/normalized/` as
   a release artefact or bind-mount it).
4. `docker compose up -d`.

The hardening profile ships active by default. Anyone who needs to
relax (e.g., temporarily enable egress for diagnostics) edits their
override file.

---

## Risks / open trade-offs

1. **Image size.** A `python:3.12-slim-bookworm`-based image with the
   full venv is roughly 600–800 MB. Acceptable. Distroless could bring
   it under 400 MB at the cost of debugging ergonomics; probably not
   worth it.
2. **Catalog data volume.** Shipping 66 normalized datasets inside the
   image would balloon it to ~1 GB+ and create a licensing-attribution
   question at image-distribution time. Keeping data outside the image
   (bind mount or named volume populated via the `normalize` service)
   is cleaner. Public forks get a smaller image and explicit control.
3. **One-shot services need egress.** `fetch-basemap` and `normalize`
   must be on an egress-enabled network for their runtime. Use a
   second, egress-enabled compose profile invoked only by `docker
   compose --profile bootstrap run`. The main `app` never joins it.
4. **`internal: true` network means no DNS.** Docker's embedded DNS
   still resolves service names inside an `internal` bridge, so
   `app` can talk to `caddy` by name. External DNS (like
   `api.cartocdn.com`) fails, which is the point.
5. **Seccomp profile maintenance.** The profile file pins specific
   blocked syscalls. Kernel updates that add new syscalls won't
   automatically be blocked; we'd need to track the Docker default
   upstream. Low-frequency chore.
6. **Rootless Docker.** Users on rootless Docker may hit UID-mapping
   friction (UID 65532 maps oddly). Works fine on rootful. Document
   the rootless caveat; don't design for it in the default.

---

## Effort estimate

Assuming the plan is accepted as drafted:

- Dockerfile + seccomp + compose + Caddyfile: 2 h
- One-shot profile services + env defaults: 1 h
- Sanitised `geodata-mcp.service.example`: 30 min
- Docs (`deploy/docker/README.md` + `deploy/systemd/README.md`): 1.5 h
- Build + smoke test locally on a non-production port: 1.5 h
- End-to-end test: spin up, OAuth flow, session persistence across
  restart, render_map with basemap cache: 1 h

Total: **~7 h of focused work.** No dependencies on external
infrastructure beyond what's already provisioned.

---

## Open questions before I start

1. **`data/normalized/`** — bind-mount from host or named volume
   populated by a one-shot `normalize` service?
2. **Image publishing** — do you want to publish the image to
   GitHub Container Registry (GHCR) under your namespace so public
   forkers can `docker pull` without building, or is `docker compose
   build` on the operator's host acceptable?
3. **Migration of the current production?** Two paths:
   - (a) Leave systemd in place on `geo.benjaminhenriksson.com`;
     Docker is purely for others.
   - (b) Migrate the production deploy to Docker as part of this work,
     retire the systemd unit.
   (a) is less disruptive; (b) unifies the story.
4. **Should `todo.md` and `public-release-todo.md` be gitignored before
   any of this ships?** They're currently tracked. Minor but easy to
   lose track of.
