# Public release — todo

Preparing a sanitised public copy of this repo while keeping the commit
history. New remote, not a GitHub fork (forks carry the full
unredacted history and a "forked from" relationship).

Effort: ~1 day if no design rework, ~2 days with a neutral default
theme + documentation pass. Work in a clone in a sandbox directory, not
in-place on the private repo.

---

## Open questions to answer before starting

These determine how much work is needed and can't be decided alone:

1. **Design.** Three options for the editorial palette + Cormorant/DM Sans
   fonts + terra/ivory colours:
   - (a) Replace with a neutral default (system font + generic grays).
   - (b) Keep as CSS variables in a themes file; ship a "default" theme
     plus the current "editorial" variant; document how to switch.
   - (c) Strip only the Stockholm-specific visual elements (map
     backdrop SVG, coordinate marginalia) and keep the palette as-is.
2. **Copy voice.** `about.html` (primer), `landing.html`, `404.html`
   are in a specific literary voice. Keep the structure and rewrite
   in plainer wording, or wholesale replace with a neutral voice?
3. **Geographic scope.** Is the public copy still Stockholm-focused
   (no changes to `catalog.json` / `normalize.py` / `geocoder.py` /
   hardcoded EPSG:3011), or does it target generalising the data
   layer? The latter is a multi-week project and outside this plan.
4. **Footer attribution.** Keep `© <year> Benjamin Henriksson` verbatim,
   or soften to something like "originally authored by Benjamin
   Henriksson, <link>" and put the forker's footer alongside?

---

## What to strip

### Files to remove entirely from history

Delete from every commit via `git filter-repo --path ... --invert-paths`:

- `docs/_deployment.md` **and** the pre-rename `docs/deployment.md`
  (VPS specifics: provider, hostname, IP ranges, tailscale setup).
- `docs/_security.md` **and** the pre-rename `docs/security.md`
  (internal threat model + audit findings; content is embargoed).
- `todo.md` and this `public-release-todo.md` — private work queue.
- Anything under `.claude/` that ever slipped into history (should be
  gitignored, but verify).

### Text replacements across every commit

Via `git filter-repo --replace-text <rules.txt>`, rules file:

```
/home/ben/geodata-mcp==>$REPO_ROOT
/home/ben/==>$HOME/
geo.benjaminhenriksson.com==><your-mcp-host>
benjaminhenriksson.com==><your-site>
ubuntu-4gb-hel1-1==>[redacted]
hetzner==>[redacted]
hel1==>[redacted]
hel1-1==>[redacted]
```

Also run with `--replace-message` to scrub the same strings from commit
messages (several old messages reference `LoadCredential`,
`IPAddressDeny`, specific paths).

### Commit messages worth rewriting manually

A handful of messages mention systemd directives or paths by name.
`git filter-repo` can rewrite these individually via a Python callback
if text-replace isn't enough.

### License consistency across history

Early commits have a proprietary "all rights reserved" `LICENSE` and a
matching README line. Rewrite via `--blob-callback` so every commit
carries the AGPL text. Otherwise a reader scrolling back sees the
project as retroactively proprietary, which muddles the license story.
Alternative: accept the inconsistency and note it in the README; less
clean.

### Author identity

Keep `author = Benjamin Henriksson` on all commits (matches the
footer-attribution choice). Nothing to rewrite unless that decision
changes.

---

## Sanitisation workflow (sandboxed)

```bash
# 1. Sandbox clone
git clone /home/ben/geodata-mcp /tmp/public-prep
cd /tmp/public-prep
git remote remove origin

# 2. Remove files from every commit
git filter-repo \
  --path docs/_deployment.md --path docs/deployment.md \
  --path docs/_security.md  --path docs/security.md \
  --path todo.md            --path public-release-todo.md \
  --invert-paths

# 3. Text content replacements
cat > /tmp/replacements.txt <<'EOF'
/home/ben/geodata-mcp==>$REPO_ROOT
/home/ben/==>$HOME/
geo.benjaminhenriksson.com==><your-mcp-host>
ubuntu-4gb-hel1-1==>[redacted]
hetzner==>[redacted]
hel1-1==>[redacted]
EOF
git filter-repo --replace-text /tmp/replacements.txt --force

# 4. Commit message replacements (same rules)
git filter-repo --replace-message /tmp/replacements.txt --force

# 5. Verify
git log --all -p | grep -iE '/home/ben|benjaminhenriksson|hetzner|credstore|hel1|LoadCredential|IPAddressDeny' | head -40

# Iterate 2-3 until empty.

# 6. Optional: rewrite LICENSE blob across history via Python callback.
# (Script to be drafted; not a one-liner.)

# 7. Push to new remote once clean
git remote add origin <new-public-remote>
git push -u origin main
```

---

## What to change in the working tree (post-history-scrub)

Even after history is clean, the current `main` still has:

### Remove

- `docs/_deployment.md`, `docs/_security.md`, `todo.md`, this file.
- `viewer/stockholm-map.svg` — if geographic scope changes. Keep if
  Stockholm-focused.
- `.claude/` contents (should already be gitignored).

### Replace

- All `geo.benjaminhenriksson.com` URLs in user-facing copy →
  placeholder (`<your-mcp-host>`) or the chosen public domain.
- `© 2026 Benjamin Henriksson` footers → per the attribution question
  above.
- OAuth consent form (`geodata_mcp/oauth.py` `_AUTH_FORM_HTML`) has
  "Stockholm open geodata" text and a GitHub source link; update.
- Landing `about.html`, `docs/*.md` references to specific coordinates
  (59.33°N, Tekniska Nämndhuset, etc.) if de-Stockholming.

### Add

- `SETUP.md` covering deployment the public forker will do themselves:
  systemd unit template, Caddy reverse-proxy stanza, credstore
  directory setup, invite-code generation, `scripts/fetch_basemap.py`
  one-shot. Much of this content exists in the private `_deployment.md`
  — lift it, sanitise, publish.
- A sanitised screenshot or two for the README (the current repo has
  none).
- Maybe a `CONTRIBUTING.md` with "this is a personal project, PRs
  welcome but no guarantees" language.

### Design choice outputs (depending on answer to Q1)

- If (a) neutral default: rewrite CSS in `viewer/landing.html`,
  `about.html`, `docs.html`, `404.html`, `index.html`, OAuth consent
  form (~600 lines of CSS total). Replace Cormorant + DM Sans + terra
  palette with system font + neutral grays.
- If (b) themeable: extract palette + font choices into a shared CSS
  variables file; ship default.css + editorial.css; document.
- If (c) minimal strip: remove `stockholm-map.svg` backdrop,
  coordinate marginalia, eyebrow labels referring to Stockholm. Keep
  palette + fonts.

---

## Verification before publishing

- `git log --all -p | grep -iE 'bearer|token|invite|secret|password|key='`
  — should be empty (no accidentally committed credentials).
- `git log --all -p | grep -iE '/home/ben|benjaminhenriksson|hetzner|credstore|hel1'`
  — should be empty after filter-repo.
- Fresh `git clone <new-remote>` in a new directory, run
  `uv sync` + `uv run python -m geodata_mcp.server --http --port 9000`
  (without credstore + without data/) and confirm: doesn't crash,
  serves landing, returns a helpful error when invite code is wrong.
- Visit every page in a browser: landing, about, docs index, one
  doc page, 404 (via a bad URL), viewer at `/view/invalid`.
- Run `scripts/fetch_basemap.py` on a clean checkout, verify tiles
  land under `data/basemap/`.

---

## Risks

- **Missed pattern → leak in history forever.** Verification grep must
  be broad and run multiple times.
- **SHA change breaks any existing clones** of the sanitised tree.
  Fine here because the private repo stays private and the public one
  is fresh.
- **`git filter-repo` refuses to run on a repo with a remote** without
  `--force`. Always work in a sandbox clone with the remote removed.
- **License history inconsistency** (pre-AGPL proprietary commits)
  may confuse a reader; resolve via Q on license rewrite.
