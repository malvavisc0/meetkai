# Deployment

How to run kAI locally for development, and how the production stack differs
from it. Three paths:

- **Bare metal** (uv) — fastest iteration; cockpit runs directly on the host.
- **Docker Compose** — closest to production; cockpit + Postgres + mailpit in containers.
- **Production** (step 5) — the base compose file alone, no dev override.

Pick one of the first two for development. Most day-to-day development uses
the bare-metal path; use Docker when you want to verify the containerized
setup or test against real Postgres. Steps 1-2 apply to all three paths.

---

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Docker + Docker Compose v2 (only for the Docker path)
- An OpenAI-compatible LLM endpoint (API base + key + model)

## 1. Clone and install

```bash
git clone https://github.com/malvavisc0/meetkai
cd meetkai
uv sync
```

## 2. Configure

### 2a. Generate the secrets

The cockpit requires three strong random values before it will start (unless
`KAI_COCKPIT_TESTING=1`). Generate each one with `openssl` — run it three
times so every secret is a distinct value:

```bash
# KAI_COCKPIT_SECRET — signs cockpit session/auth tokens
openssl rand -hex 32

# KAI_COCKPIT_ESCALATION_SECRET — signs escalation payloads
openssl rand -hex 32

# KAI_CREDENTIAL_ENCRYPTION_KEY — encrypts stored credentials at rest
openssl rand -hex 32
```

Paste each printed hex string into the matching variable in `.env`. If you
have no `openssl`, any equivalent CSPRNG works as well:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"   # repeat per secret
```

Notes:

- `KAI_CREDENTIAL_ENCRYPTION_KEY` is a deployment-wide root secret that feeds
  key derivation — the cockpit only requires it to be set, it is not
  length-checked. Generate 32 random bytes with the command above and keep
  the value stable once credentials have been
  stored (changing it makes them undecryptable; rotate deliberately with
  `kai cockpit rotate-credential-key` instead).
- Also set `KAI_CREDENTIAL_KEY_VERSION=v1` in `.env` alongside the key. It is
  the version tag embedded in stored ciphertext; without it the cockpit raises
  on the first credential save. Keep it stable too — changing the tag without
  a rotation makes existing rows undecryptable.
- The Docker stack additionally needs a Postgres password:
  `openssl rand -hex 16` for `KAI_POSTGRES_PASSWORD`.
- Treat these values like passwords: keep them in `.env` only (it is git
  ignored), never commit or share them.

### 2b. Write `.env`

kAI reads `.env` from the working directory. For bare-metal development you
need a minimal `.env` — the cockpit **refuses to start** without real values
for its three secrets (unless `KAI_COCKPIT_TESTING=1`):

```bash
# .env — paste the hex values generated in step 2a below
KAI_COCKPIT_SECRET=paste-generated-hex-here
KAI_COCKPIT_ESCALATION_SECRET=paste-generated-hex-here
KAI_CREDENTIAL_ENCRYPTION_KEY=paste-generated-hex-here
KAI_CREDENTIAL_KEY_VERSION=v1                  # matches .env.example; keep stable

KAI_PUBLIC_URL=http://localhost:8080   # base URL baked into magic links

KAI_LLM_API_BASE=https://api.openai.com/v1
KAI_LLM_API_KEY=sk-your-key-here
KAI_LLM_MODEL=gpt-4o-mini
```

Everything else falls back to built-in defaults: SQLite at `data/cockpit.db`,
escalations at `data/cockpit.escalations.json`. The cockpit applies Alembic
migrations automatically on startup (`kai cockpit migrate` runs them manually).

See `README.md` → *Configure* for the full list of `KAI_*` settings, and
`.env.example` for the Docker stack's template.

## 3a. Run bare metal (recommended for development)

```bash
uv run kai cockpit serve
```

The cockpit binds to `127.0.0.1:8080` by default (`--host`/`--port` to change).

### First login (bare metal)

Login is by magic link, and links are only sent to **existing users** — so on
a fresh install, create your operator account first:

```bash
uv run kai cockpit user create you@example.com --language English --timezone Europe/Berlin
```

Operators get **no feature flags by default** — image interpretation and SSO
stay off until granted, so tools silently lose capabilities for that user:

```bash
uv run kai cockpit user flags you@example.com --image    # --sso likewise; --show to inspect
```

Then get the magic link to your inbox. The default SMTP target is a host
named `mailpit` (the Docker dev stack provides it); on bare metal you have
two options:

- **Run a local SMTP catch** — e.g. `docker run -p 1025:1025 -p 8025:8025 axllent/mailpit`
  and set `KAI_SMTP_HOST=127.0.0.1` in `.env`. Links appear in mailpit's UI
  on <http://localhost:8025>.
- **Mint the link from the CLI** — with `KAI_COCKPIT_TESTING=1` and
  `KAI_SMTP_HOST=` (empty) in `.env`, the link is printed to stdout instead
  of emailed, so no SMTP server is needed:

  ```bash
  uv run kai cockpit request create you@example.com
  uv run kai cockpit request approve you@example.com   # prints the magic link
  ```

Setting `KAI_COCKPIT_AUTO_APPROVE_LOGIN=true` skips the approval step but
still sends the link over SMTP — it does not print it anywhere.

Run a bot directly (without the cockpit) instead:

```bash
uv run kai list
uv run kai start email \
  --goal "Answer support questions grounded in the Brain. Be helpful and concise." \
  --language English
uv run kai status email
```

### Tests and lint

```bash
uv run pytest            # parallel via pytest-xdist
uv run pytest -n 0       # single process
uv run ruff format .
uv run ruff check --fix .
```

## 3b. Run with Docker Compose

The dev override (`docker-compose.override.yml`) is auto-merged by
`docker compose`, so a plain `up` gives you the development setup: the
cockpit is **built from the working tree** (not the published image),
[mailpit](https://mailpit.axllent.com/) catches all outbound SMTP, and the
cockpit is published on host port 8080.

```bash
cp .env.example .env   # then edit .env — see below
docker compose up -d --build
```

Minimum edits in `.env`:

| Variable | What to put |
|----------|-------------|
| `KAI_COCKPIT_SECRET` | `openssl rand -hex 32` |
| `KAI_COCKPIT_ESCALATION_SECRET` | `openssl rand -hex 32` |
| `KAI_POSTGRES_PASSWORD` | `openssl rand -hex 16` |
| `KAI_CREDENTIAL_ENCRYPTION_KEY` | `openssl rand -hex 32` |
| `KAI_CREDENTIAL_KEY_VERSION` | `v1` (keep stable; see step 2a) |
| `KAI_LLM_API_KEY` | your LLM provider key |

The defaults already wire the cockpit to the in-stack Postgres
(`database:5432`) and to mailpit (`KAI_SMTP_HOST=mailpit`,
`KAI_SMTP_PORT=1025`), so login links land in mailpit's web UI.

Then:

- Cockpit: <http://localhost:8080>
- Mailpit (SMTP catch + login links): <http://localhost:8025>

The container entrypoint runs `alembic upgrade head` on start (the cockpit
app also self-migrates), so the database schema is always current.

### First login (Docker)

Magic links are only sent to existing users, so create your operator account
once, inside the running container:

```bash
docker compose exec cockpit kai cockpit user create you@example.com \
  --language English --timezone Europe/Berlin
docker compose exec cockpit kai cockpit user flags you@example.com --image
```

(`user flags` grants the capabilities that are off by default for every new
operator — see the bare-metal section above.)

Then request a login at <http://localhost:8080/login> — the magic link lands
in mailpit at <http://localhost:8025>.

Useful commands:

```bash
docker compose logs -f cockpit     # follow cockpit logs
docker compose down                # stop (keeps volumes)
docker compose down -v             # stop and wipe Postgres/storage volumes
docker compose up -d --build       # rebuild after code changes
```

> Changing `KAI_POSTGRES_*` credentials after the first boot requires
> resetting the volume: `docker compose down -v` (the Postgres password is
> fixed at first container start).

## 4. Optional: Brain workers (morphik + crawl4ai)

The Brain (knowledge ingestion) runs as a **separate compose stack**
(`docker-compose.workers.yaml`) with its own env template. It is not required
for cockpit development — without it, the brain tool stays disabled and the
cockpit logs a warning instead of failing.

If you need it locally, note that the Brain is a **second Compose project**
and the two stacks are network-isolated by default: each file is its own
project, and the `odyssey` network each one declares becomes
`<project>_odyssey` — distinct bridges even on one box. That
is why the stock `KAI_BRAIN_BASE_URL=http://morphik:8000` /
`KAI_BRAIN_CRAWLER_URL=http://crawl4ai:11235` values in `.env.example` only
work once the containers share a network (pick option (a) or (b) below).

**1. Prepare the env file.** On its own host copy the template to `.env` (each
host serves exactly one stack's `.env`); on a box that hosts *both* stacks use
distinct names, because each `docker compose` invocation interpolates `${VAR}`
from a single file:

```bash
cp .env.workers.example .env.workers   # fill in every secret, incl. ROUTER_API_KEY
                                       # (one key for morphik's completion, embedding and
                                       # vision calls — the cockpit's own KAI_LLM_* values
                                       # from step 2 are unrelated)
```

**2. Start and verify:**

```bash
docker compose -f docker-compose.workers.yaml --env-file .env.workers up -d
curl -s http://localhost:8000/health | head -c 200    # morphik publishes 8000
docker exec crawl4ai sh -c 'wget -qO- http://localhost:11235/health'   # crawl4ai publishes
                                                           # no host port — healthcheck runs in-container
docker compose -f docker-compose.workers.yaml --env-file .env.workers ps   # all four: healthy
```

**3. Make the two stacks see each other.**

- **(a) Co-located, separate `up` commands** (each stack keeps its own env
  file) — the only cross-referenced container is the cockpit (its bots read
  `KAI_BRAIN_BASE_URL`/`KAI_BRAIN_CRAWLER_URL`; morphik and crawl4ai each use
  their own Postgres), so one attach is enough:

  ```bash
  docker network connect kai-workers_odyssey cockpit   # cockpit reaches morphik/crawl4ai/redis
  ```

  Compose **recreates** containers from their file settings (observed: an
  attach created via `connect`, then `down` + `up`, vanished with the old
  container) — so re-run these `connect` lines after every recreate of either
  stack, not just `up`. A fast `docker compose restart cockpit` keeps the
  attach, but it also cannot refresh the baked `environment:` values, so
  env-file edits need `up -d` (+ `connect` after) anyway.

  The same bridge in one idempotent line — run it after **every** stack
  (re)creation instead of remembering the attaches at each of the three `up`
  places in this doc (swap the two `_odyssey` names for the production project
  names if you run the base file too):

  ```bash
  for pair in "kai-workers_odyssey cockpit" \
              "meetkai-dev_odyssey morphik" "meetkai-dev_odyssey crawl4ai"; do
    set -- $pair; docker network connect "$1" "$2" 2>/dev/null && echo "attached $2" || true
  done
  ```

- **(b) Co-located, one invocation** — pass both files to a single
  `docker compose`; Compose merges them into one project and one bridge, so
  every hostname resolves without manual attaches. Costs: one combined env
  file (both templates merged — `KAI_BRAIN_CRAWL4AI_TOKEN` trivially equal),
  and the project name comes from the **last** file listed (`kai-workers`
  here).

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.workers.yaml \
    --env-file .env.combined up -d
  ```

- **(c) Separate hosts** — set `KAI_BRAIN_BASE_URL=http://<worker-host>:8000`
  (morphik *does* publish `8000`) and `KAI_BRAIN_CRAWLER_URL=http://<worker-host>:<port>`
  — crawl4ai publishes nothing, so add a `ports:` mapping in your own override
  file or keep them on one host via (a)/(b).

**4. Mint the brain token (once per environment).** With morphik healthy:

```bash
# No `source .env.workers` — dotenv files are not shell scripts (fish rejects
# them outright). This routes through bash on purpose, so it runs identically
# from fish, bash, or zsh; the secret is read out of the live container, which
# is its canonical holder:
env bash -c 'admin=$(docker compose -f docker-compose.workers.yaml --env-file .env.workers exec morphik env | grep "^ADMIN_SERVICE_SECRET=" | cut -d= -f2-); curl -s -X POST http://localhost:8000/cloud/generate_uri -H "Content-Type: application/json" -H "X-Morphik-Admin-Secret: $admin" -d "{\"name\":\"kai-cockpit\",\"user_id\":\"kai\",\"expiry_days\":5475}"'
# -> {"uri":"morphik://kai-cockpit:<jwt>@host"} — paste the <jwt> into the
# cockpit's .env as KAI_BRAIN_MORPHIK_TOKEN, then restart the cockpit.
```

Tokens are validated against morphik's own DB: don't keep a JWT from another
environment or from before a `KAI_BRAIN_MORPHIK_JWT_SECRET` rotation — the
cockpit's brain calls will be rejected. The cockpit's brain tool registers
itself only when `KAI_BRAIN_BASE_URL` **and** `KAI_BRAIN_MORPHIK_TOKEN` are
both set (`src/kai/brain/config.py:118`), and website ingest needs
`KAI_BRAIN_CRAWLER_URL` **and** `KAI_BRAIN_CRAWL4AI_TOKEN` — a missing URL
disables ingestion with a startup warning only, never an error at `up`.

Notes:

- Morphik's completion/embedding models call the OpenAI-compatible model
  router at `https://router.requesty.ai/v1` — set `ROUTER_API_KEY` in
  `.env.workers`. This is morphik's key only; the cockpit and its bots keep
  using the `KAI_LLM_*` values from step 2.
- The workers stack defines a container named `redis` — if another local
  stack already uses that name, rename one before starting both on the same
  Docker host.
- Teardown: `docker compose -f docker-compose.workers.yaml --env-file
  .env.workers down` stops the stack but **keeps** `kai-workers_*` volumes —
  indexed brain documents survive. Add `-v` only to wipe the Brain for real;
  after wiping (or after rotating `KAI_BRAIN_MORPHIK_JWT_SECRET`), re-mint the
  token in step 4.

## 5. Production deployment

Production reuses the **same files** as the Docker path — the difference is
that the dev override is not merged, so nothing from the development pages
above leaks in:

| | Development | Production |
|---|---|---|
| Compose file | `docker-compose.yml` + auto-merged `docker-compose.override.yml` | `docker-compose.yml` **only** |
| Cockpit image | built from the working tree (`meetkai-dev-cockpit`, `pull_policy: never`) | published `ghcr.io/malvavisc0/meetkai-cockpit:${KAI_IMAGE_TAG:-latest}`, `pull_policy: always` |
| SMTP | mailpit catch, links read from its UI | a real relay (`KAI_SMTP_USER`/`KAI_SMTP_PASSWORD`) — links are actually emailed |
| Host port | published `8080:8080` by the override | not published: the service only `expose`s `8080` on the internal network |
| Compose project | `meetkai-dev` | `kai-cockpit` (its own volumes) |

Start it on the target host:

```bash
cp .env.example .env.production   # add KAI_IMAGE_TAG=<fixed release>; `latest` +
                                  # `pull_policy: always` can move prod to a new
                                  # build on any redeploy
# Generate every secret with step 2a — do NOT reuse values from a dev machine.
docker compose --env-file .env.production -f docker-compose.yml up -d
```

`docker compose` also reads the file named after the compose file
(`docker-compose.yml.env`) if you prefer not to pass `--env-file`.

### Public ingress and TLS

Because the base file only `expose`s port 8080, the cockpit is unreachable
from outside the compose network until you put a proxy in front of it:

- **Coolify (the reference platform):** the proxy routes the public domain to
  container port `8080` — nothing to change (see `README.md` → *Running With Docker*).
- **Any other host:** add an explicit `ports:` (e.g. `"8080:8080"`) via your
  own override/extension file and terminate TLS at your reverse proxy.

Set `KAI_PUBLIC_URL` to the **public https URL** — it is baked into magic
links (an unset value yields a host-less `/login/auth?token=...`) and it
auto-enables the Secure session cookie, since `cookie_secure` follows the URL
scheme (`src/kai/cockpit/settings.py:34-36`). Force it with
`KAI_COOKIE_SECURE=1` when TLS ends at a proxy and the app sees plain http.

### Secrets and rotation

- Use freshly generated values for all four secrets (step 2a) plus a fresh
  `KAI_POSTGRES_PASSWORD`. Dev values in production = the dev box's credentials
  are production credentials.
- Back up `KAI_CREDENTIAL_ENCRYPTION_KEY`/`KAI_CREDENTIAL_KEY_VERSION`: losing
  them makes every stored connection secret undecryptable. Change them only via
  `kai cockpit rotate-credential-key` (`src/kai/cli/cockpit.py:65`), which
  re-encrypts rows in place; hand-editing the key or the version strands
  existing rows.
- `KAI_POSTGRES_*` credentials are fixed at the volume's first boot — changing
  them requires recreating the `postgres` volume, so set them before the first
  `up`.
- Optional error tracking: any Sentry-compatible backend via `SENTRY_DSN`,
  `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE` (empty DSN = disabled).

### Verify

```bash
docker compose --env-file .env.production -f docker-compose.yml ps          # both services healthy
docker compose --env-file .env.production -f docker-compose.yml logs -f cockpit
curl -fsS https://your-domain/health                                        # 200
```

The initial operator is created inside the container, exactly as in the
Docker development path — and needs the same flag grants:

```bash
docker compose --env-file .env.production -f docker-compose.yml \
  exec cockpit kai cockpit user create you@your-domain \
  --language English --timezone Europe/Berlin
docker compose --env-file .env.production -f docker-compose.yml \
  exec cockpit kai cockpit user flags you@your-domain --image
```

The Brain (step 5) is a second stack, usually on its own host: there the
`KAI_BRAIN_BASE_URL`/`KAI_BRAIN_CRAWLER_URL` values must be host-published
URLs (morphik publishes `8000`; crawl4ai publishes nothing until you add a
`ports:` mapping — see step 5 option (c)), `KAI_BRAIN_CRAWL4AI_TOKEN` must be
identical in both env files, and the morphik JWT is minted once and pasted
into the cockpit env — same workflow as locally, just against the real hosts.

## 6. Optional: landing page

The marketing site in `landing/` is a standalone static site, not part of any
compose stack. Preview it with:

```bash
python3 -m http.server -d landing 8090   # http://localhost:8090
```

## Troubleshooting

- **`migrations failed` on container start** — check `docker compose logs
  cockpit`; usually the database container wasn't healthy yet or the volume
  has an incompatible schema. `docker compose down -v && docker compose up -d
  --build` resets local state.
- **Login link never arrives** — with Docker, open mailpit at
  <http://localhost:8025>. Bare metal: the default SMTP host is `mailpit`
  (unresolvable outside Docker), so either point `KAI_SMTP_HOST` at a real
  relay, or run with `KAI_COCKPIT_TESTING=1` + empty `KAI_SMTP_HOST` and mint
  the link via `kai cockpit request create` / `request approve` (printed to
  stdout). Also confirm the user exists — links are only sent to registered,
  enabled users (`kai cockpit user list`).
- **Cockpit exits immediately complaining about secrets** — bare-metal runs
  require real `KAI_COCKPIT_SECRET`, `KAI_COCKPIT_ESCALATION_SECRET`, and
  `KAI_CREDENTIAL_ENCRYPTION_KEY` values in `.env` (see step 2a), unless
  `KAI_COCKPIT_TESTING=1`.
- **Magic link URL is malformed (missing host)** — `KAI_PUBLIC_URL` is unset;
  the link is built as `/login/auth?token=...` with no base. Set
  `KAI_PUBLIC_URL=http://localhost:8080` and request a new link.
- **Brain tool disabled warning** — expected until you run the workers stack
  and set `KAI_BRAIN_MORPHIK_TOKEN`; harmless for cockpit-only development.
- **Port 8080 already in use** — change the host side of the port mapping in
  `docker-compose.override.yml` (`"8081:8080"`) and update `KAI_PUBLIC_URL`.
