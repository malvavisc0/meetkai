# docker-compose.yml audit

Date: 2026-10-15

Scope: `docker-compose.yml` plus `docker-compose.override.yml`, `docker-compose.workers.yaml`, `Dockerfile`, `entrypoint.sh`, `.env.example`, and live network state.

## Critical / High

### 1. `KAI_COCKPIT_AUTO_APPROVE_LOGIN` defaults to `true`

`docker-compose.yml:64`

```yaml
KAI_COCKPIT_AUTO_APPROVE_LOGIN: ${KAI_COCKPIT_AUTO_APPROVE_LOGIN:-true}
```

Our own `.env.example` comment says admin approval is the brute-mail gate. Defaulting to `true` in the production compose means any login-link delivery (or email leak in prod) auto-admits with no human check.

Fix: flip the default to `false` and require the variable explicitly.

### 2. Required secrets silently become empty strings

`docker-compose.yml:57-58, 67-68, 76-77`

`${KAI_COCKPIT_SECRET}`, `${KAI_COCKPIT_ESCALATION_SECRET}`, `${KAI_CREDENTIAL_ENCRYPTION_KEY}`, `${KAI_LLM_API_KEY}` have no `:?` guard — a missing `.env` entry passes an empty value and the app boots with a weak/empty secret.

Especially bad for `KAI_COCKPIT_ESCALATION_SECRET`: the code audit found `POST /api/escalations` is unauthenticated when that secret is unset.

Fix: use `${VAR:?set in .env}` so `docker compose up` fails instead of running insecure.

### 3. Cross-stack networking is broken between the two compose projects

Live state: two isolated networks exist, `meetkai-dev_odyssey` and `kai-workers_odyssey`, because both files declare `odyssey:` without `external: true`.

Consequence: the cockpit's `KAI_BRAIN_BASE_URL=http://morphik:8000` and `KAI_BRAIN_CRAWLER_URL=http://crawl4ai:11235` will **not resolve** when both stacks run together — DNS fails across project networks. (Currently only works because the app runs on the host and reaches workers via `localhost:8000`.)

Fix:

```yaml
# docker network create odyssey
networks:
  odyssey:
    external: true
```

in both `docker-compose.yml` and `docker-compose.workers.yaml`.

### 4. `pull_policy: always` + mutable `:latest`

`docker-compose.yml:35-36`

```yaml
image: ghcr.io/malvavisc0/meetkai-cockpit:${KAI_IMAGE_TAG:-latest}
pull_policy: always
```

Reproducibility and supply-chain risk: a redeploy silently pulls whatever `latest` is now.

Fix: pin a release tag (or digest) and drop `always` (keep it in the override where it is the dev image).

## Medium

### 5. No resource limits

No `mem_limit`/`deploy.resources.limits` on cockpit or database. The cockpit spawns LLM-consuming bot subprocesses — an unbounded memory spike OOMs the host instead of being capped.

### 6. No runtime hardening

Add `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]` (the non-root user makes this cheap), and consider `read_only: true` with tmpfs for `/tmp/kai`.

### 7. No log rotation

Default json-file driver grows unbounded; add `logging: {options: {max-size, max-file}}`.

### 8. Secrets via `environment:`

Visible to `docker inspect` and `/proc/<pid>/environ` of any host process. `env_file:` (or compose `secrets`) is the standard fix.

### 9. Project-name footgun

Base file is `name: kai-cockpit`, the override renames to `meetkai-dev` (`docker-compose.override.yml:1`). Volumes/networks are project-prefixed, so running with vs. without the override creates *different* volumes (`kai-cockpit_storage` vs `meetkai-dev_storage`) — data can be orphaned by switching invocation. Pick one name and reuse it, or make the override a separate stack explicitly.

### 10. Doc drift

- `Dockerfile:15` references `docker-compose.dev.yml` (does not exist — it is `.override.yml`)
- `.env.example:16-20` talks about `kai-postgres`/`kai-postgres-data`, but the actual service/volume are `database`/`postgres`

## Low

- `user: appuser` (`docker-compose.yml:37`) duplicates the Dockerfile `USER appuser` — harmless, but keep one source of truth.
- No `stop_grace_period` — bot subprocesses can be killed mid-turn on `down`.
- `container_name: database` + `hostname: database` are globally generic names; collisions on shared hosts are a matter of time.
- `mailpit` in the override exposes only `8025` — fine for dev; the cockpit also depends on SMTP at `mailpit:1025`, which is internal-only (correct).

## Already good

- Non-root `appuser`, `exec`-form entrypoint that runs migrations before serving (`entrypoint.sh:16`), `set -e`
- `depends_on` with `service_healthy`
- Named volumes pre-chowned to uid 1000
- `restart: unless-stopped`
- Healthchecks with sane `start_period`
- `TZ`, and a well-commented env contract — secrets shared across the two stacks are documented in `.env.example:5-7`

## Priority order

1. **#3** — blocks the compose stack from working at all with the workers
2. **#1 + #2** — production auth posture
3. **#4** — image pinning
4. **#5-7** — hardening

## Resolutions (2026-10-15, operator decisions after review)

- **#1 — Accepted risk.** `KAI_COCKPIT_AUTO_APPROVE_LOGIN` stays `true` by default (owner's decision). The brute-mail gate is the built-in login rate limiter; the misleading `.env.example` comment was corrected to say so.
- **#2 — Partially fixed.** `KAI_COCKPIT_ESCALATION_SECRET` freshly generated into `.env` (the cockpit's `POST /api/escalations` ingest is bearer-guarded again). No `:?` boot guards; secrets stay in `environment:` ("fine for now").
- **#3 — Deferred.** Stacks stay split (workers on their own box, per the README design), so the external-network fix was not applied. Cleanup instead: all seven related containers (`cockpit`, `database`, `mailpit`, `morphik`, `crawl4ai`, `redis`, `postgres`) and both networks (`meetkai-dev_odyssey`, `kai-workers_odyssey`) deleted.
- **#4 — Partially fixed.** `pull_policy: always` removed (deploys go through Coolify, which pulls on redeploy). `:latest` on the cockpit image and `crawl4ai:0` in the workers file stay mutable.
- **#5 — Declined.** No resource limits for now.
- **#6 — Partially fixed.** `security_opt: [no-new-privileges:true]` added to every service in all three files; `cap_drop: [ALL]` added on the cockpit only (the postgres containers need setuid/chown capabilities during first init). `appuser` removed per operator decision — containers run as root, which also moots the root-owned `logs` volume found during review.
- **#7 — Fixed.** json-file log rotation (`max-size: 10m`, `max-file: 3`) on every service, defined once per file via a `x-logging` YAML anchor.
- **#8 — Accepted** (secrets via `environment:` for now).
- **#9 — Fixed.** Cockpit project renamed to `kai` (was `kai-cockpit`); `name:` removed from the override, so dev and prod invocations share one project with one volume set. The workers stack keeps `kai-workers` (unchanged from before the audit), so its volumes are unaffected. Hosts running an old cockpit project (`kai-cockpit`, `meetkai-dev`) must migrate volumes across the rename before redeploying — see "Upgrading an existing deployment" in `docs/DEPLOYMENT.md`; a plain redeploy boots an empty database.
- **#10 — Fixed.** `.env.example` now names the `database` service and the `postgres` volume; the Dockerfile was rewritten without the `appuser` block that carried the stale `docker-compose.dev.yml` reference; README stack table and redis-collision note corrected.
- **Low items — mostly fixed.** `user: appuser` gone with appuser; all container names and hostnames are `kai-*`-prefixed across both stacks (no more global `database`/`redis`/`postgres` collisions); `stop_grace_period` left at default; mailpit's web UI (8025) now binds to `127.0.0.1` — it is an unauthenticated inbox holding magic-link emails.

Workers-file follow-ups — closed later the same day: `:?` guards on every workers secret (a missing/empty `.env.workers` entry now fails compose loudly instead of running an empty JWT signing key), morphik's host port bound to `127.0.0.1`, crawl4ai moved to the stock `unclecode/crawl4ai:0.9` image after the 0.9.5 security release (the base ships its own Playwright chromium, so the bake layer and per-start install wrapper were dropped), and morphik now gating on redis health.

Post-review refinements recorded for completeness: the morphik image was bumped to `ghcr.io/morphik-org/morphik-core:2026-10-14` and every session-proven assumption re-validated against it (entrypoint `postgres` probe unchanged, the `openai/`-prefix and `extra_body` litellm workarounds pass live model probes on its litellm 1.74.7, the 2000-dim caps unchanged, all kai-facing routes present); the storage service was renamed `morphik-postgres` → `pgvector` to match its image (references through `depends_on`/`POSTGRES_URI`/`PGHOST` completed to the service-name DNS; container, hostname, and volume names unchanged); and the compose config key `morphik_config` → `morphik`. The dimensional/embedding workarounds expire if morphik stretches past the 2000-dim pgvector index cap or litellm relaxes its provider-prefix routing — the comments in `docker-compose.workers.yaml` mark both spots.