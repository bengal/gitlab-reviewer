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
                                               - library checkouts mounted read-only
                                                - opencode run --thinking -m provider/model "<prompt>"
                                                - emit structured JSON + stream logs live
                                                    |  /v1 (local models)
                                                    v
                                        shared llama-server (OpenAI-compatible)
```

- **Web app** — FastAPI + Jinja2 + HTMX (vendored, no CDN). In-process
  APScheduler: an interval *queue pump* claims the lowest-position queued job
  (Postgres: `FOR UPDATE SKIP LOCKED`; SQLite: `BEGIN IMMEDIATE`) under a
  concurrency semaphore, and a nightly cron promotes the enrolled jobs.
  At startup, runs/jobs left in `running`/`claimed` by a previous process
  (restart, crash) are finalized — their container is stopped, the run is
  marked `error`, the job `failed` — so a restart never leaves a phantom
  running review in the UI (the queue page also offers a Cancel button for
  in-flight reviews).
  Shared-password login → itsdangerous-signed session cookie.
- **Orchestrator** — `podman run --rm` of a fixed, allowlisted review-runner
  image through the host's **rootless user podman socket** (never
  `--privileged`; `--memory=2g --cpus=2 --pids-limit=256`
  `--security-opt no-new-privileges`), one sibling container per review.
  `ORCHESTRATOR=fake` swaps in a deterministic in-process backend for
  dev/tests.
- **Review-runner** (`containers/review-runner/`) — stdlib-only entrypoint:
  clones the target MR branch + base (token via git credential helper); the
  extra library repos arrive read-only at `/work/lib/<path>` — the app keeps
  a persistent checkout per library on the host (`LIBRARY_CHECKOUT_DIR`,
  re-pulled at most every `LIBRARY_PULL_MAX_AGE_HOURS`) and bind-mounts them
  (the entrypoint clones as a fallback when a checkout is not mounted);
  then it renders `opencode.json` (provider block;
  `permission.bash/edit/webfetch = allow`), runs
   `opencode run --thinking -m <provider>/<model> "<prompt>"` under a hard
   timeout, streaming the model's output (including its `Thinking:` blocks)
   line by line to `review.log` — which the app persists as it arrives and
   the run detail page shows live — then extracting the final fenced JSON
   block to `result.json` (falls back to raw markdown). It also exports
   opencode's own session (the model's reasoning/"thinking" blocks, tool
   calls and full transcript) via `opencode session list` + `opencode export`
   to `session.json`, stored on the run and downloadable from its detail
   page (*Model session → Download session*); both the session export and the
   live log are best-effort — capture failures are logged and never fail the
   run. `--thinking` needs opencode ≥ 1.18.30 (the pinned version); an older
   opencode that rejects the flag is retried without it.
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
cp .env.example .env
```

Fill in the three required secrets in `.env` (full reference in
[Configuration](#configuration-env) below):

```sh
# 1. Shared app password -> PBKDF2 hash  (APP_PASSWORD_HASH)
uv run python -c "from app.security import hash_password; print(hash_password('your-password'))"
# 2. Session cookie signing key          (SESSION_SECRET)
python -c "import secrets; print(secrets.token_urlsafe(32))"
# 3. Key for encrypting secrets at rest  (SECRET_ENC_KEY)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Create the database (SQLite is the default — zero setup, see
[Database](#database-sqlite-or-postgres) for Postgres):

```sh
uv run alembic upgrade head        # creates ./dev.sqlite3
```

Run the app:

```sh
uv run uvicorn --factory app.main:create_app --reload --port 8000
```

Open http://127.0.0.1:8000 and do the one-time UI setup:

1. **Log in** with the password from step 1.
2. **Settings → GitLab**: instance URL (e.g. `https://gitlab.com`), project
   (`group/name` or numeric id), a project **read/write** token →
   **Test connection**.
3. **Settings → Model profiles**: add one per model you want to offer —
   e.g. `anthropic` provider + `claude-...` model id, or `local` provider +
   your `llama-server` model id with `base_url` = `LLAMA_BASE_URL` from
   `.env`. Mark one as default. Each profile row has **Edit** (re-open the
   form with the current values; the API key stays masked — leave it empty
   to keep it) and **Delete** buttons.
4. **MRs → Refresh** to load open MRs, then *Schedule review* on one.

> Tip: for the very first pass set `ORCHESTRATOR=fake` in `.env` — reviews
> run through a canned in-process backend, so you can try the whole UI
> (queue, reorder, results, archive) without podman or any model. Switch to
> `ORCHESTRATOR=podman` when you are ready for real reviews.

## Database: SQLite or Postgres?

The schema is identical either way (managed by Alembic); `DATABASE_URL` in
`.env` is the only difference.

| | **SQLite** (default) | **Postgres** |
|---|---|---|
| Use when | local development, single machine, single user | real deployment; app running in a container; concurrent users |
| Setup | none — the file is created automatically | a running Postgres (see below) |
| Notes | stored in `./dev.sqlite3` next to the repo; single-writer, which is fine for one app instance | the compose deployment ships a `postgres` service out of the box |

**SQLite.** Nothing to do: the default `DATABASE_URL=sqlite:///./dev.sqlite3`
points at a file relative to where you start the app (run from the repo
root). `uv run alembic upgrade head` creates it — or simply starting the app
does, since a fresh database is provisioned automatically at boot. Use an
absolute path (`sqlite:////home/you/work/gitlab/dev.sqlite3`) if the app is
started from different directories.

**Postgres.** The `psycopg` driver is already a project dependency.

- *Deployment (recommended):* use the compose file — it runs a `postgres`
  service on the internal network and the app's `DATABASE_URL` defaults to
  `postgresql+psycopg://mrreview:mrreview@postgres:5432/mrreview` (override
  via `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` + `DATABASE_URL` in
  `.env` if you want different credentials). Migrations run automatically at
  container start.
- *Local dev against a real Postgres:* run one ad hoc, e.g.

  ```sh
  podman run -d --name mrreview-pg \
    -e POSTGRES_USER=mrreview -e POSTGRES_PASSWORD=mrreview -e POSTGRES_DB=mrreview \
    -p 127.0.0.1:5432:5432 docker.io/library/postgres:16-alpine
  ```

  then set in `.env`:

  ```
  DATABASE_URL=postgresql+psycopg://mrreview:mrreview@127.0.0.1:5432/mrreview
  ```

  and run `uv run alembic upgrade head` once. (If the app is containerized,
  point the host at the container's published port as above; inside the
  compose network use the service name `postgres` instead of
  `127.0.0.1`.)

## Configuration (.env)

Copy `.env.example` to `.env` (git-ignored). The file is read from the
directory you start the app in, and real environment variables always
override it.

### Required

| Variable | What to put |
|---|---|
| `APP_PASSWORD_HASH` | PBKDF2 hash of the shared login password — run the `hash_password('your-password')` one-liner above. Without it, nobody can log in. |
| `SESSION_SECRET` | Any long random string: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Signs the session cookie; change it to log everyone out. |
| `SECRET_ENC_KEY` | Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Encrypts the GitLab token and model API keys stored in the DB. **Keep it safe** — if lost, the stored secrets become unreadable (re-enter them in Settings; the rest of the DB is unaffected). |

### Database

| Variable | What to put |
|---|---|
| `DATABASE_URL` | `sqlite:///./dev.sqlite3` (default, relative) or `postgresql+psycopg://user:pass@host:5432/dbname` — see [Database](#database-sqlite-or-postgres). |

### Models & review engine

| Variable | Default | What to put |
|---|---|---|
| `ORCHESTRATOR` | `podman` | `podman` for real ephemeral review containers, `fake` for the canned in-process backend (first-run/CI). |
| `REVIEW_IMAGE` | `gitlab-mr-review/review-runner:latest` | The review-runner image the app `podman run`s. Pre-build it: `podman build -t gitlab-mr-review/review-runner:latest containers/review-runner/`. |
| `REVIEW_IMAGE_ALLOWLIST` | *(empty)* | Comma-separated image names/prefixes the orchestrator may run (a prefix allows any tag, e.g. `gitlab-mr-review/review-runner`). Empty = only `REVIEW_IMAGE` itself; anything else is refused. |
| `LLAMA_BASE_URL` | *(empty)* | Base URL of your shared llama.cpp server (OpenAI-compatible `/v1`), e.g. `http://127.0.0.1:8080` on the host. Used as the fallback `base_url` for `local` model profiles. In compose this is set to `http://llama-server:8080` automatically. |
| `ANTHROPIC_API_KEY` | *(empty)* | Optional fallback key for `anthropic` profiles; keys can also be stored per profile in the UI (encrypted at rest). |
| `REVIEW_TIMEOUT_SECONDS` | `1800` | Hard per-review timeout (30 min). Exceeded runs are marked `timeout`. |
| `PODMAN_NETWORK` | `host` | Network the review containers join: `host` for dev (reaches GitLab + llama-server directly), `mrreview` in the compose deployment (reaches `llama-server` via the internal network). |
| `DISABLE_SCHEDULER` | `0` | `1` = no background queue pump / nightly cron (tests, or when you want to pump manually). |
| `LIBRARY_CHECKOUT_DIR` | `~/.local/share/mr-review/libraries` | Host directory with the persistent checkouts of the extra (library) repos: one checkout per library, bind-mounted read-only into review containers at `/work/lib/<path>` instead of cloning per review. Must be a **host path** — the compose/quadlet deployments mount it into the app container at the same path. |
| `LIBRARY_PULL_MAX_AGE_HOURS` | `24` | A library checkout older than this is re-pulled (`git pull`/`fetch`) before the next review uses it; `0` = pull before every review. A failed re-pull reuses the stale checkout (the initial clone must still succeed). |

### Deployment-only (consumed by compose/quadlet, not the app)

| Variable | Default | What to put |
|---|---|---|
| `PODMAN_SOCKET` | `/run/user/1000/podman/podman.sock` | Host path of your **rootless user** podman socket (the app container mounts it to spawn review containers). Enable it first: `systemctl --user enable --now podman.socket`. Must be owned by the uid the app runs as (1000 in the image). |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `mrreview` / `mrreview` / `mrreview` | Credentials for the compose `postgres` service; keep them in sync with `DATABASE_URL`. |
| `LLAMA_MODEL_DIR` / `LLAMA_MODEL_FILE` | `./models` / `model.gguf` | Host directory with the model file(s) and the file to load, mounted into `llama-server`. |

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
cp .env.example .env    # fill in SESSION_SECRET, APP_PASSWORD_HASH, SECRET_ENC_KEY (see Configuration)
podman compose -f deploy/compose.yaml up -d
```

Three services on one shared bridge network (`mrreview`), **no host ports
published**:

- `app` — built from `deploy/Containerfile` (context = repo root), mounts the
  podman socket from `${PODMAN_SOCKET:-/run/user/1000/podman/podman.sock}` and
  the library checkouts dir `${LIBRARY_CHECKOUT_DIR:-~/.local/share/mr-review/libraries}`
  (at the same path inside the container, see the configuration table),
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
- [ ] **Live log** — while a review is running, its *Results* detail page's
      **Log** card updates every 2 s (open, "Live" hint) showing the model's
      streamed output as it arrives (including `Thinking:` blocks); the
      *Queue* **Running** row has a **Live log** link to it; the card stops
      polling once the run finishes.
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
   containers via env only; the GitLab token reaches git (in the review
   container, and in the app when it clones/re-pulls the library checkouts)
   through a credential helper, never the command line.
  - **Log scrubbing** — review logs, the raw-markdown result and the
    session export are scrubbed before storage: every secret value is
    replaced with `***`
    (`app/orchestrator/run_review.py:scrub_secrets`).
 - **Untrusted MR code** — the reviewer runs *untrusted code* with
   `bash`/`edit` allowed inside an ephemeral, resource-capped container.
   Treat diffs as data, not instructions (prompt-injection aware); extra
   library repos are bind-mounted read-only from persistent host checkouts
   (never writable by the container); consider an egress allowlist for
   hardened setups.
- **Network exposure** — postgres and llama-server are reachable only on the
  shared internal network; the deployment files publish no host ports.

## Development

```sh
uv run pytest -q                 # unit / integration / e2e (fully in-process)
uv run ruff check .
uv run python -c "from app.main import create_app; create_app()"   # boot check
DATABASE_URL=sqlite:////tmp/fresh.sqlite3 uv run alembic upgrade head  # fresh DB
```
