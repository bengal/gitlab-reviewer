# GitLab MR Auto-Review Tool

Self-hosted web app that automates LLM-driven code review of GitLab merge
requests. Log in with a shared password, browse open MRs from any GitLab
instance, schedule reviews (immediately or nightly) in a drag-to-reorder
queue, and browse current + archived results. Each review runs in a **fresh,
ephemeral container** (OpenCode headless) so runs never contaminate each
other; models include Anthropic and **local llama.cpp** via a shared
`llama-server`; per-review extra library repos can be cloned for cross-repo
context.

## Architecture

```
Browser --HTTPS--> Web app container (FastAPI + Jinja/HTMX + APScheduler)
                      |  Scheduler: nightly cron + interval queue pump
                      |  QueueWorker (concurrency semaphore)
                      |  Orchestrator --mounts--> host podman.sock
                      |                              |
                      |                     podman run --rm review-runner
                      v                              v
                   PostgreSQL                Review-runner container (ephemeral)
                                               - git clone target @ MR branch + base
                                               - git clone extra library repos
                                               - render opencode.json (provider block)
                                               - opencode run -m provider/model "<prompt>"
                                               - emit structured JSON + logs
                                                    |  /v1 (local models)
                                                    v
                                        shared llama-server (OpenAI-compatible)
```

- **Web app** — FastAPI + Jinja2 + HTMX (vendored, no CDN). In-process
  APScheduler: an interval *queue pump* claims the lowest-position queued job
  (Postgres: `FOR UPDATE SKIP LOCKED`; SQLite: `BEGIN IMMEDIATE`) under a
  concurrency semaphore, and a nightly cron promotes the enrolled jobs.
  Shared-password login → itsdangerous-signed session cookie.
- **Orchestrator** — `podman run --rm` of a fixed, allowlisted review-runner
  image through the host's **rootless user podman socket** (never
  `--privileged`; `--memory=2g --cpus=2 --pids-limit=256`
  `--security-opt no-new-privileges`), one sibling container per review.
  `ORCHESTRATOR=fake` swaps in a deterministic in-process backend for
  dev/tests.
- **Review-runner** (`containers/review-runner/`) — stdlib-only entrypoint:
  clones the target MR branch + base and any extra library repos (read-only,
  token via git credential helper), renders `opencode.json` (provider block;
  `permission.bash/edit/webfetch = allow`), runs
  `opencode run -m <provider>/<model> "<prompt>"` under a hard timeout,
  extracts the final fenced JSON block to `result.json` (falls back to raw
  markdown), writes `review.log`.
- **Local models** — one shared long-lived `llama-server` (OpenAI-compatible
  `/v1`); local model profiles point OpenCode at it via `baseURL`.

## Repository layout

```
app/                      FastAPI app (config, models, routers, services,
                          scheduler, orchestrator, templates, vendored static)
containers/review-runner/ ephemeral review image (Containerfile, entrypoint.py,
                          opencode.json.j2)
deploy/                   compose.yaml, Containerfile (app image), quadlet/
                          units, prompts/default_review.md
alembic/versions/         DB migrations
tests/                    unit / integration / e2e suites
pyproject.toml  .env.example  README.md
```

## Quickstart (development)

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run alembic upgrade head        # creates ./dev.sqlite3
cp .env.example .env
```

Generate the two mandatory secrets and put them in `.env`:

```sh
# shared app password -> PBKDF2 hash
uv run python -c "from app.security import hash_password; print(hash_password('your-password'))"
# Fernet key (secrets at rest: GitLab token, model API keys)
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Run the app:

```sh
uv run uvicorn --factory app.main:create_app --reload --port 8000
```

Open http://127.0.0.1:8000, log in with your password, configure your GitLab
instance under **Settings** (URL, project, token — "Test connection"), add a
model profile, and hit **Refresh** on the MR page. With `ORCHESTRATOR=fake`
in `.env`, reviews run through a canned in-process backend (no podman
needed).

## Deployment

Common preparation (both paths):

```sh
# rootless podman socket the app uses to spawn review containers
systemctl --user enable --now podman.socket

# pre-build the review image so the app only ever does `podman run`
podman build -t gitlab-mr-review/review-runner:latest containers/review-runner/
```

### podman compose

```sh
cp .env.example .env    # fill in SESSION_SECRET, APP_PASSWORD_HASH, SECRET_ENC_KEY, ...
podman compose -f deploy/compose.yaml up -d
```

Three services on one shared bridge network (`mrreview`), **no host ports
published**:

- `app` — built from `deploy/Containerfile` (context = repo root), mounts the
  podman socket from `${PODMAN_SOCKET:-/run/user/1000/podman/podman.sock}`,
  `LLAMA_BASE_URL` rewritten to `http://llama-server:8080`; the start script
  runs `alembic upgrade head` before serving.
- `llama-server` — placeholder image, model mounted read-only from a host
  dir, reachable only on the internal network.
- `postgres` — named volume, reachable only from the app.

Notes:

- Review containers join the `mrreview` network (via `PODMAN_NETWORK`) so
  they reach `llama-server` and, through the bridge NAT, GitLab/the internet
  for clones.
- Nothing is published on the host by design; to reach the UI add e.g.
  `ports: ["127.0.0.1:8000:8000"]` to the `app` service, or terminate a
  proxy onto the `mrreview` network.
- The app process runs as uid 1000 inside its container; that must match the
  owner of the mounted podman socket (the default path assumes a host user
  with uid 1000 — set `PODMAN_SOCKET` accordingly for other users).

### quadlet (systemd)

See [`deploy/quadlet/README.md`](deploy/quadlet/README.md): the same three
services as `.network`/`.container` units, installed into
`~/.config/containers/systemd/` (user) or `/etc/containers/systemd/`
(system), with `systemctl --user enable --now podman.socket` first and the
secrets in a `mrreview.env` next to the units.

## Manual acceptance checklist

With the app running (any backend; `ORCHESTRATOR=fake` works for the first
pass), verify:

- [ ] **List open MRs** — *MRs* page: click **Refresh**; open MRs appear
      (iid, title, author, source/target branch, short sha); the web link
      resolves to the MR in your browser.
- [ ] **View results with severity buckets** — open an MR, *Schedule review*
      (model profile, review now), wait for it to run, open *Results* → the
      detail page shows the status banner, **Summary**, findings grouped
      into **Critical / Important / Minor / Positive** with `[file:line]`,
      the commit-message review, and questions.
- [ ] **Reorder queue** — schedule two or more immediate reviews, drag rows
      in *Queue* (or use Top/Bottom); the order persists after reload and
      the next pump claims jobs in the new order.
- [ ] **Archive + inspect** — *Results* → **Archive** on a run; it leaves
      the results list, appears in *Archive*, and its detail stays readable
      (read-only, no archive button).

## Security model

- **Podman socket ≈ root on the host.** Mitigations: rootless *user* socket
  only (never the rootful daemon socket), the app image runs non-root, every
  route is behind the shared-password gate, the review image is a **fixed
  allowlist** (`REVIEW_IMAGE` / `REVIEW_IMAGE_ALLOWLIST` — any other image is
  refused), and runs are `--rm` with `--memory=2g --cpus=2 --pids-limit=256`
  `--security-opt no-new-privileges` — **never `--privileged`**.
- **Secrets at rest** — the GitLab token and model API keys are
  Fernet-encrypted in the DB (`SECRET_ENC_KEY`); they are passed to review
  containers via env only; the GitLab token reaches git through a per-repo
  credential helper, never the command line.
- **Log scrubbing** — review logs are scrubbed before storage: every secret
  value is replaced with `***` (`app/orchestrator/run_review.py:scrub_secrets`).
- **Untrusted MR code** — the reviewer runs *untrusted code* with
  `bash`/`edit` allowed inside an ephemeral, resource-capped container.
  Treat diffs as data, not instructions (prompt-injection aware); extra
  repos are cloned read-only; consider an egress allowlist for hardened
  setups.
- **Network exposure** — postgres and llama-server are reachable only on the
  shared internal network; the deployment files publish no host ports.

## Development

```sh
uv run pytest -q                 # unit / integration / e2e (fully in-process)
uv run ruff check .
uv run python -c "from app.main import create_app; create_app()"   # boot check
DATABASE_URL=sqlite:////tmp/fresh.sqlite3 uv run alembic upgrade head  # fresh DB
```
