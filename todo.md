# Todo

Small polish items for the public surfaces. None are blocking; ordered
roughly by payoff per unit of work.

## OG image for geo.bh.com

No `og:image` meta tag today, so pasting the URL into Slack, email, or a
message app renders as a bare link. Generate a paper-toned 1200×630 PNG
with the site title in Cormorant, a fragment of the Stockholm map, and
the SWEREF label. Stash as `viewer/og-image.png`, reference from
`landing.html`, `about.html`, and `docs.html`.

## Favicon for geo.bh.com

Browser tab currently has no favicon; the main site does. A terra square
with a small "G" or a simplified map outline would match the aesthetic.
Export at 32×32 and 192×192; link from every page's `<head>`.

## Footer width parity

`/about` and `/docs` shells wrap at 1100 px. The landing footer reuses
the 860-px `.container` class from the hero. Minor visual discontinuity
when someone navigates between pages. Either split the landing footer
into its own wrapper, or standardise everything to 1100 px.

## /docs index rendered as catalogue cards

`/docs` currently shows `index.md` rendered inline. Functional, but the
landing's Read-more cards look more inviting. Promote the ten-doc list
to editorial cards on `/docs` specifically (still reusing the doc shell
for `/docs/<slug>`). Would double as a more scannable entry point.

## Voice consistency across docs

Some docs use "we" (author voice), some use "the server" (third-person).
One sweep to pick one register would make the doc set read as a single
work rather than a folder of independent write-ups.

## Concrete example with output on /about

The design-example prose ("top-5 DeSOs by income") would be stronger if
paired with the actual rendered PNG and a one-line caption. Grounds the
abstract pipeline description for a non-technical reader. Bonus: lets the
`render_map` output serve as its own documentation.

## Viewer state for hard-TTL'd sessions

If a 14-day-old viewer URL is clicked, the viewer loads, polls, and
eventually shows an empty panel. A dedicated "this session has expired
and cannot be restored" state would be clearer. Current behaviour looks
like a bug.

## Deferred from earlier tranches (still open)

- Elevation + two terrain tools (`elevation_at`, `slope_aspect`). One DEM
  unlocks a whole category of questions.
- Detaljplan / ÖP ingestion. Biggest actual-data gap for municipal
  planning work. Needs a schema-tolerant pipeline.
- Routing / isochrones. "15 minutes walking" class of questions.
  Heavier lift (~4 days).
- Audit log for `execute_sql`. Plumbing task; noted in multiple places.
- SVG output for `render_map`. Small format addition.
- Viewer labelling. Label-placement is its own project; defer until
  demand is concrete.
- Per-user invite codes (currently single shared code).
- Persistent OAuth token store (current store is in-memory; graceful
  restart survives the MCP session state but not OAuth tokens, so users
  re-authorise after every restart).
