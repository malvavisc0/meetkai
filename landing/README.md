# kAI landing page

Standalone static marketing site for `meetk.ai`, fully decoupled from the
cockpit. No Python, no server, no build step — just static files served at the
site root.

## Contents

- `index.html` — the page. Self-contained: the `<head>`, icons, and copy are
  inlined (no templating). The login button links to the cockpit at
  `https://cockpit.meetk.ai/login`.
- `static/` — assets served under `/static/…` (the paths `index.html` expects):
  - `cockpit.css` — a copy of the cockpit stylesheet, kept intact so the
    landing renders identically. Only the `.landing-*` rules and fonts are
    actually used here; it can be trimmed later if desired.
  - `fonts/`, favicons, `apple-touch-icon.png`, `icon-192.png`, `icon-512.png`,
    `og.png`, `site.webmanifest`.
- `CNAME` — binds the GitHub Pages custom domain (`meetk.ai`).

## Deploy

Published to GitHub Pages by `.github/workflows/pages.yml` on pushes to
`master` that touch `landing/**`. In the repo's Pages settings, set the source
to "GitHub Actions" and confirm the custom domain; the `CNAME` file persists
it. Point DNS for `meetk.ai` at GitHub Pages (A/AAAA records) and enable
"Enforce HTTPS".

## Preview locally

```bash
python3 -m http.server -d landing 8090
# open http://localhost:8090
```
