# Kai Docker Deployment Plan

## 1. CI/CD Pipeline — Docker Image on Push to Master

### 1.1 GitHub Actions Workflow

A new workflow triggers on every push to `master`. It builds the Docker image for the `cockpit` service (the only service in `docker-compose.yml` that is built from the project's own code) and **pushes** it to GitHub Container Registry (GHCR). Coolify then pulls the tagged image and deploys — no SSH, no build on the deploy server, no custom deploy commands. On PRs, the same build runs as a smoke test without pushing.

**File:** `.github/workflows/docker.yml`

**Triggers:**
- `push` on `master` branch — builds and pushes.
- `pull_request` on `master` branch — builds only (no push).

**Permissions:** Explicitly grant the minimum the workflow needs (the default `GITHUB_TOKEN` only has `contents: read`; without `packages: write` the GHCR push silently 403s):
```yaml
permissions:
  contents: read
  packages: write
```

**Concurrency:** Group by branch so overlapping master pushes don't push twice:
```yaml
concurrency:
  group: docker-${{ github.ref }}
  cancel-in-progress: true
```

**Steps:**
1. `actions/checkout@v7` — check out the repo.
2. `docker/setup-buildx-action@v4` — enables buildx with caching backend.
3. `docker/login-action@v4` — logs into GHCR using `GITHUB_TOKEN` (only on push events).
4. `docker/metadata-action@v6` — generates image tags and labels.
5. `docker/build-push-action@v7` — builds and pushes with GHA cache (`type=gha`). `push` is gated on `github.event_name == 'push'` so PRs build but don't publish.

**Tags (via `docker/metadata-action`):**
- Git commit SHA (always on push, e.g. `ghcr.io/malvavisc0/meetkai-cockpit:sha-a1b2c3d`)
- `latest` on master pushes (e.g. `ghcr.io/malvavisc0/meetkai-cockpit:latest`)

The SHA tag is what Coolify should pin for reproducible deploys and rollbacks (§6); `latest` is a convenience alias for the most recent master build.

### 1.2 Image Registry — GHCR

GitHub Container Registry is the registry Coolify pulls from:
- No additional secrets needed in the workflow — `GITHUB_TOKEN` with `packages: write` (declared in §1.1) is enough to push.
- No rate limits on authenticated pulls from within the same GitHub org.
- Tags, manifests, and image metadata are managed in the same place as the source code.

**Coolify-side configuration (one-time):**
- Create a Coolify "Docker Registry-based" resource pointing at `ghcr.io/malvavisc0/meetkai-cockpit`.
- Authenticate with a GitHub PAT that has `read:packages` scope (the workflow's `GITHUB_TOKEN` can't be reused by Coolify). Store the PAT + org in Coolify's registry credentials.
- Set the image tag Coolify tracks (e.g. `latest` for auto-deploy, or a specific SHA for pinned deploys).
- Set the container port (`8080`) and a `/health` healthcheck.
- Configure the same environment variables and persistent volumes the `cockpit` service uses in `docker-compose.yml` (see §4.2).

### 1.3 Docker Build Optimizations

The existing `Dockerfile` is already well-structured. Recommended additions to the workflow and Dockerfile:
- The `.dockerignore` already excludes `.git/`, `tests/`, `node_modules/`, caches, `data/`, and `.env` — context is already lean; no change needed.
- Cache the `uv sync --frozen` layer via the GHA cache backend (`type=gha`). Vendor install is no longer a build step (it runs at container startup on named volumes — see §2), so there's no vendor layer to cache in CI.
- Do **not** enable multi-arch builds. `kai vendors install all` builds `whisper.cpp` from source for the build host's arch; cross-arch buildx would produce broken binaries. Restrict to `linux/amd64` (the GitHub runner's native arch and the Coolify host arch). If other arches are needed later, switch to per-arch build stages or pre-built vendor binaries first.

### 1.4 Non-root runtime (container hardening)

The container runs as a non-root user (`appuser`, uid 1000) regardless of who runs `docker compose up` on the host. **Coolify SSHing into the VPS as root does NOT make the container run as root** — the container's runtime user is enforced by the Dockerfile's `USER` directive and is independent of the host SSH session.

How it's set up in the `Dockerfile`:
- A `appuser` user (uid 1000) is created.
- The writable runtime dirs (`/app/data`, `/app/vendor`, `/app/models`, `/tmp/kai`, `/home/appuser`) are pre-created and `chown`ed to `appuser` **before** `USER appuser`. This matters for named volumes: when a fresh named volume mounts on `/app/vendor` etc., Docker copies the image dir's ownership into the volume, so `appuser` can write without a runtime chown.
- `ENV HOME=/home/appuser` and `ENV UV_CACHE_DIR=/app/vendor/.uv-cache` so the kokoro venv creation (`uv pip install` at runtime) has a writable cache/home.
- Build deps (`build-essential`, `cmake`, `git`) are world-executable, so `appuser` can still build whisper.cpp from source at startup.

Verified by building the image and running the full `kai vendors install all` as `appuser` (uid 1000): whisper.cpp compiled, ffmpeg/kokoro models downloaded, `kai` resolved, `/root` correctly denied.

**Dev caveat:** `docker-compose.dev.yml` bind-mounts `./data/kai`, `./data/vendor`, `./data/models` from the host. Bind mounts do NOT inherit image ownership — they use the host path's uid:gid. Since the container runs as uid 1000, the host dirs must be owned by uid 1000 (the primary user on most Linux dev machines). If they're root-owned (e.g. created by a prior `sudo`), fix once with `sudo chown -R 1000:1000 data/`.

## 2. Vendor Artifacts — Install at Startup, Persist via Named Volumes

### 2.1 Behavior

`entrypoint.sh` runs `kai vendors install all` at container startup, before `exec "$@"`. This:
- Builds `whisper.cpp` from source (ffmpeg + whisper-cli/whisper-server).
- Downloads whisper and kokoro model files.
- Is **idempotent**: each vendor's `is_installed()` (a local file check, no network) skips the build/download when artifacts are already present, so it's a fast no-op on every boot after the first.

The vendor binaries land in `/app/vendor/<name>/` and the models in `/app/models/<name>/` (resolved by `VendorManager._resolve_project_root()` → `/app` in the container).

### 2.2 Why not bake into the Docker image

Baking `kai vendors install all` into a Dockerfile `RUN` layer was considered and rejected:
- It bloats the image by ~200-500 MB (whisper/kokoro models) and makes every CI build several minutes slower.
- It reintroduces the PATH-ordering footgun: `ENV PATH` is set after the `uv sync` RUN, so the install must call `/app/.venv/bin/kai` explicitly.
- It doesn't survive a volume mount anyway — if `/app/vendor` or `/app/models` is on a volume, the empty volume shadows the baked-in artifacts on first boot and the install re-runs regardless.

Instead, keep the install at startup and make it cheap by persisting its output on named volumes.

### 2.3 Named volumes in prod (the fix)

The slow first-boot cost only bites when the install output is lost between deploys. `docker-compose.yml` mounts `/app/vendor` and `/app/models` on named volumes so the build/download happens **once** and survives container recreation (which Coolify triggers on every new image):

```yaml
  cockpit:
    volumes:
      - cockpit-data:/app/data        # SQLite DB, per-bot state, configs
      - cockpit-vendor:/app/vendor    # ffmpeg, whisper.cpp, kokoro venv
      - cockpit-models:/app/models    # whisper ggml + kokoro onnx models
volumes:
  cockpit-vendor:
  cockpit-models:
```

**Outcome:**
- First deploy on fresh volumes: `kai vendors install all` builds whisper.cpp + downloads models (several minutes, needs network to GitHub + Hugging Face). The container won't accept traffic until `entrypoint.sh` finishes (`set -e`).
- Every subsequent deploy (Coolify pulls new image, recreates container): the volumes already hold the artifacts, `is_installed()` returns true, the install is a ~1s no-op, and the cockpit starts immediately.
- New/recreated volume: install re-runs to repopulate it.

**Trade-off vs bake-in:** first-ever deploy is slower (install at boot, not in CI), and the deploy target needs network + build deps (`build-essential`, `cmake`, `git` are already in the Dockerfile's `apt-get install`, so this is satisfied). The payoff is a much smaller image and no PATH-ordering hacks in the Dockerfile.

## 3. Production Deployment Strategy — Coolify (Pull from GHCR)

### 3.1 How it works

Coolify is configured as a "Docker Registry-based" deployment: it watches the `ghcr.io/malvavisc0/meetkai-cockpit` image for new tags and, when a new tag appears (or on a manual trigger), pulls it and recreates the container. There is no SSH, no build on the Coolify host, and no custom deploy commands — the entire deploy is Coolify pulling a ready-made image.

**Flow:**
1. Push to `master` → §1.1 workflow builds and pushes `ghcr.io/malvavisc0/meetkai-cockpit:sha-<…>` + `:latest` to GHCR.
2. Coolify detects the new image (polling, webhook, or manual redeploy) and pulls it.
3. Coolify recreates the `cockpit` container with the configured env vars, volumes, and port.
4. Coolify runs the `/health` healthcheck and rolls back the container if it fails (Coolify's built-in behavior when a healthcheck is configured).

No `.github/workflows/deploy.yml` is needed — the deploy is fully driven by Coolify watching the registry. The only GitHub Actions workflow is the build+push in §1.1.

### 3.2 Coolify configuration (one-time)

Create a new Coolify resource of type **Docker Registry-based**:

| Field | Value |
|---|---|
| Registry | `ghcr.io` |
| Image | `malvavisc0/meetkai-cockpit` (Coolify prepends `ghcr.io/`) |
| Tag to track | `latest` for auto-deploy, or a pinned SHA for a fixed deploy |
| Registry credentials | GitHub PAT with `read:packages` scope |
| Port | `8080` |
| Healthcheck | `GET /health` on `8080` (Coolify restarts the container on failure) |
| Persistent volumes | the `cockpit` service's mounts — see §3.3 |
| Environment variables | the `cockpit` service's env — see §4.2 |

### 3.3 Volumes and multi-service considerations

There are now two compose files:

| File | Purpose | cockpit | mailpit | upstream tags |
|---|---|---|---|---|
| `docker-compose.yml` | **Production** — pull-only, used by Coolify | pulls `ghcr.io/malvavisc0/meetkai-cockpit:<tag>` | absent | pinned (see below) |
| `docker-compose.dev.yml` | **Development** — builds locally | `build: .`, bind-mounts `./data/vendor` + `./data/models` | included (dev SMTP sink :8025) | floating (`redis`, `devlikeapro/waha`, `unclecode/crawl4ai:latest`) |

Five services run in prod (`redis`, `waha`, `lightrag`, `crawl4ai`, `cockpit`); `mailpit` is dev-only and excluded from `docker-compose.yml` so prod doesn't swallow real outbound magic-link email. `lightrag` is pinned to `ghcr.io/hkuds/lightrag:v1.5.4` in both files.

Prod upstream image pins (in `docker-compose.yml`):
- `redis` → `redis/redis-stack-server:7.4.0-v8` (Redis Stack with modules)
- `waha` → `devlikeapro/waha:latest`
- `crawl4ai` → `unclecode/crawl4ai:0`

**Deploying in Coolify:** Coolify supports deploying a `docker-compose.yml` directly — point the Coolify resource at this repo and it uses `docker-compose.yml` as the source of truth. Every image is pulled; nothing is built on the Coolify host. Set `KAI_IMAGE_TAG` in the Coolify resource env to pin a specific SHA for deploys/rollbacks (defaults to `latest`).

Persistent volumes the `cockpit` container needs (from `docker-compose.yml`):
- `./data/kai` → `/app/data` — cockpit SQLite DB, per-bot state, configs (the only cockpit mount in prod)
- `./data/vendor` and `./data/models` are **bind-mounted in dev** (`docker-compose.dev.yml`) so you can rebuild/inspect vendor artifacts on the host. In **prod** (`docker-compose.yml`) they're named volumes (`cockpit-vendor`, `cockpit-models`) so the install persists across deploys without a host path — see §2.3.

### 3.4 `docker-compose.yml` split (done)

The single-file approach (one `docker-compose.yml` with `build: .` + an `image:` key, toggled by env) was replaced by a clean two-file split so dev and prod don't share contradictory settings:

- **`docker-compose.yml` (prod)** — `cockpit` has `image:` + `pull_policy: always` and **no `build:`**; the other services are pinned. This is the file Coolify deploys.
- **`docker-compose.dev.yml` (dev)** — `cockpit` has `build: .` and **no `image:`**; upstream tags float; `mailpit` is included. Run with `docker compose -f docker-compose.dev.yml up -d --build`.

This avoids the footgun where `compose up` (no `--build`) in a dev file with both `build:` and `image:` silently pulls a stale registry image instead of rebuilding locally.

### 3.5 Alternatives (not chosen)

- **Docker Compose on a bare VPS with an SSH deploy job** — viable but reintroduces SSH secrets, a second workflow, and manual healthcheck handling. Coolify already provides all of this out of the box.
- **Kubernetes** — overkill for a single-service app with a SQLite-backed cockpit. Only worth revisiting if horizontal scaling or multi-node HA becomes a requirement.
- **Managed PaaS (Railway, Fly.io, Render)** — equivalent to Coolify but hosted; Coolify is preferred here because it's already the chosen platform.

## 4. Secrets and Environment Variables

### 4.1 Secrets Required

| Secret / Credential | Where | Purpose |
|---|---|---|
| `GITHUB_TOKEN` (built-in) | GitHub Actions | GHCR push from §1.1 — `permissions: packages: write` is declared in the workflow, no manual config needed |
| GitHub PAT with `read:packages` | Coolify registry credentials | Lets Coolify pull `ghcr.io/malvavisc0/meetkai-cockpit` — the workflow's `GITHUB_TOKEN` cannot be reused by Coolify |

No SSH secrets are needed — Coolify pulls the image itself; there is no SSH deploy job.

If Docker Hub is ever preferred over GHCR: add `DOCKER_USERNAME` + `DOCKER_PASSWORD` as repo secrets and swap the registry in §1.1, but Coolify-side auth is the same pattern (registry credentials in Coolify).

### 4.2 Production Environment Variables

The `.env` file referenced in `docker-compose.yml` must be maintained separately on the deployment target. Key variables:

| Variable | Required | Notes |
|---|---|---|
| `TZ` | Yes | Timezone for WAHA and other services |
| `REDIS_URL` | Yes | `redis://redis:6379` (same network) |
| `WAHA_API_KEY` | Yes | WhatsApp API authentication |
| `OPENROUTER_API_KEY` | Yes | LLM API key for LightRAG |
| `KAI_BRAIN_LIGHTRAG_API_KEY` | Yes | LightRAG API authentication |
| `KAI_BRAIN_CRAWL4AI_TOKEN` | Yes | Crawl4ai API authentication |
| `KAI_SMTP_HOST` | Yes (prod) | Cockpit magic-link relay host. Dev defaults to `mailpit` (the dev compose ships mailpit); prod has no mailpit, so this MUST be a real SMTP relay or the cockpit's login emails silently fail. |
| `KAI_SMTP_PORT` | Yes (prod) | SMTP port (e.g. 587 for STARTTLS) |
| `KAI_SMTP_USER` | Yes (prod) | SMTP auth username (env var is `KAI_SMTP_USER`, not `_USERNAME` — see `cockpit/settings.py`) |
| `KAI_SMTP_PASSWORD` | Yes (prod) | SMTP auth password |
| `KAI_SMTP_FROM` | Yes (prod) | From address for magic-link emails |
| Various `WAHA_*` settings | Varies | Depends on WAHA configuration needs |

## 5. Implementation Checklist

- [x] Create `.github/workflows/docker.yml` with the build+push workflow (§1.1), using latest action versions (`checkout@v7`, `setup-buildx-action@v4`, `login-action@v4`, `metadata-action@v6`, `build-push-action@v7`).
- [x] Confirm `entrypoint.sh` runs `kai vendors install all` before `exec "$@"` (§2.1) and that `/app/vendor` + `/app/models` are on named volumes in `docker-compose.yml` (§2.3) so the install persists across Coolify deploys.
- [x] Add an `image:` key to the `cockpit` service in `docker-compose.yml` (§3.4) so `docker compose pull` and Coolify's registry-based deploy work.
- [x] Test the image locally (`docker build . -t meetkai-cockpit:test && docker run --rm meetkai-cockpit:test`) to verify vendor install completes and the server starts. Verified during the §1.4 non-root build: the full `kai vendors install all` ran as `appuser` (uid 1000), whisper.cpp compiled, models downloaded, `kai` resolved.
- [ ] Create a GitHub PAT with `read:packages` scope and add it to Coolify's registry credentials.
- [ ] Create a Coolify "Docker Registry-based" resource for `ghcr.io/malvavisc0/meetkai-cockpit`, set port `8080`, healthcheck `/health`, volumes, and env vars (§3.2, §4.2).
- [x] Resolve the vendor persistence in prod: `/app/vendor` + `/app/models` must be named volumes (§2.3), not empty bind-mounts, so the first-boot install survives container recreation.
- [ ] Set up the Coolify environment variables (§4.2) — use `.env.example` as the source of truth for the full list.
- [ ] Test the full pipeline: push to `master` → CI builds & pushes → Coolify pulls new tag → cockpit responds on `/health`.

## 6. Rollback Strategy

Because production runs a pulled image (not a locally-built one), rollback is a registry tag switch, not a rebuild.

- **Coolify UI (preferred):** In the Coolify resource, switch the tracked tag from `latest` to the previous good `sha-<…>` tag and redeploy. Coolify pulls that exact image and recreates the container. Each `sha-<…>` tag is immutable and retained in GHCR.
- **Via `docker-compose.yml` (manual):** Set `TAG=sha-<previous-good>` in the Coolify host's env (or the `.env` next to the compose file), then `docker compose pull cockpit && docker compose up -d cockpit`. This is the same mechanism §3.4 enables.
- **Database-level:** The SQLite database (`cockpit.db`) lives in the persistent volume (`/app/data`, mapped from `./data/kai` in compose or the Coolify volume). Back up the volume before deploying, and restore from backup on rollback.
- **Coolify automatic rollback:** If a `/health` check fails after a deploy, Coolify can be configured to roll back to the previous container automatically — enable this in the resource's healthcheck settings.