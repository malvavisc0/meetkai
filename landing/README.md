# kAI landing page

Standalone static marketing site for `meetk.ai`, fully decoupled from the
cockpit. No Python, no server, no build step — just static files served at the
site root.

## Contents

- `index.html` — the page. Self-contained: the `<head>`, icons, and copy are
  inlined (no templating). The login button links to the cockpit at
  `https://cockpit.meetk.ai/login`.
- `static/` — assets served under `/static/…` (the paths `index.html` expects):
  - `cockpit.css` — landing components using the cockpit's color, typography,
    and spacing tokens.
  - `landing.js` — keyboard-accessible example switching. The first example
    and native template accordions also work without JavaScript.
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
python3 -m http.server 8090
# open http://localhost:8090
```
