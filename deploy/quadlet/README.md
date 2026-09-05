# Quadlet deployment

Equivalent of `deploy/compose.yaml` as systemd/podman quadlet units:

| Unit | Service |
|---|---|
| `mrreview.network` | shared bridge network (name `mrreview`) |
| `postgres.container` | PostgreSQL, named volume `mrreview_pgdata`, no published port |
| `llama-server.container` | llama.cpp `/v1` on `mrreview-llama:8080`, model from a host dir |
| `app.container` | web app + scheduler; mounts the host rootless podman socket |

## Install

User-level (rootless, recommended) or system-level:

```sh
# user-level
install -D -m 0644 mrreview.network postgres.container llama-server.container app.container \
    "$HOME/.config/containers/systemd/"
# or system-level
sudo install -D -m 0644 mrreview.network postgres.container llama-server.container app.container \
    /etc/containers/systemd/
systemctl [--user] daemon-reload
```

Then, once:

```sh
# the app spawns review containers through the host rootless podman socket
systemctl --user enable --now podman.socket

# model directory for llama-server (drop your .gguf into it)
sudo mkdir -p /srv/mrreview/models

# build the app image (context = repository root) and the review-runner image
podman build -f deploy/Containerfile -t mrreview-app:latest .
podman build -t gitlab-mr-review/review-runner:latest containers/review-runner/

# secrets (template ships with the units)
install -m 0600 mrreview.env.example "$HOME/.config/containers/systemd/mrreview.env"
$EDITOR "$HOME/.config/containers/systemd/mrreview.env"
```

## Start / stop / status

```sh
systemctl --user enable --now mrreview.network postgres.container llama-server.container app.container
systemctl --user status app.container
journalctl --user -u app.container -f
systemctl --user stop app.container llama-server.container postgres.container mrreview.network
```

## Notes

- The app container runs as uid 1000 inside the container; the mounted
  podman socket must be owned by the host user whose uid the app process
  maps to (the default assumes a host user with uid 1000 — adjust the
  `Volume=` line in `app.container` for other UIDs).
- `DATABASE_URL` in `mrreview.env` must use the quadlet container name
  `mrreview-postgres` as host (the compose file uses the service name
  `postgres`); likewise `LLAMA_BASE_URL` points at `mrreview-llama`.
- No host ports are published; reach the UI the same way as with compose
  (temporary local port forward, or a proxy on the `mrreview` network).
