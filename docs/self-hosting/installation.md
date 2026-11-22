# Installation

[Back to the documentation index](../README.md)

Typst Editor ships as a single container image: `ghcr.io/fl-grtg/typst-editor:main`. It contains the Python backend, the prebuilt frontend and the Typst CLI used for server-side rendering and export. All state (SQLite database, uploaded files, invite code) lives in one data directory, `/app/data` inside the container.

## Requirements

- Docker with the Compose plugin (or plain Docker).
- An `amd64` or `arm64` host.
- Outbound HTTPS from the browsers of your users to `packages.typst.org` if documents use `@preview` packages. The Typst compiler itself is served by your server.

## Option 1: Docker Compose (recommended)

Download the shipped [`compose.yml`](../../compose.yml) and start it:

```bash
curl -fsSLO https://raw.githubusercontent.com/fl-grtg/typst-editor/main/compose.yml
docker compose up -d
```

What the Compose file does:

- Publishes port `8978` on loopback only (`127.0.0.1:8978:8978`).
- Stores data in the named volume `typst-data`, mounted at `/app/data`.
- Reads an optional `.env` file next to `compose.yml`. Use it for settings, see [Configuration](configuration.md).
- Pins `DATA_DIR=/app/data`, `HOST=0.0.0.0` and `PORT=8978` inside the container.
- Rotates container logs (3 files of 10 MB).

### Hardened variant

[`compose.hardened.yml`](../../compose.hardened.yml) is an overlay that adds a read-only root filesystem, a `tmpfs` for `/tmp`, `cap_drop: ALL`, `no-new-privileges` and a PID limit. Download it next to `compose.yml` and start both:

```bash
curl -fsSLO https://raw.githubusercontent.com/fl-grtg/typst-editor/main/compose.hardened.yml
docker compose -f compose.yml -f compose.hardened.yml up -d
```

## Option 2: docker run

```bash
docker run -d --name typst-editor \
  --restart unless-stopped \
  -p 127.0.0.1:8978:8978 \
  -v typst-data:/app/data \
  ghcr.io/fl-grtg/typst-editor:main
```

Pass settings with `-e NAME=value` or `--env-file .env`.

## First sign-in

Open `http://127.0.0.1:8978`. Registration is invite-only by default. On the first start with an empty data directory, the container generates an invite code, prints it to the log and saves it to `DATA_DIR/.invite_token`:

```bash
docker compose logs app | grep "invite code"          # Compose
docker logs typst-editor 2>&1 | grep "invite code"    # docker run
```

Sign up with that code.

The generated code is only applied on the start that creates the database. After the container restarts, sign-up with a server-wide invite code is disabled until you set `REGISTRATION_INVITE_TOKEN` yourself. To keep a permanent code, and for any server reachable from the internet, choose it before the first start:

```bash
echo "REGISTRATION_INVITE_TOKEN=$(openssl rand -hex 32)" >> .env
```

The token must have at least 16 characters, otherwise the server refuses to start. Restart with `docker compose up -d` after changing it.

## Health check

The image defines a Docker health check against `GET /healthz`. Check the state with:

```bash
docker compose ps
curl -fsS http://127.0.0.1:8978/healthz
```

## Using a bind mount instead of a volume

The container runs as user `app` (UID and GID `999`). If you mount a host directory instead of the named volume, make it writable for that user:

```bash
mkdir -p data && sudo chown -R 999:999 data
```

The entrypoint exits with a clear message if the data directory is not writable.

## Running without Docker

You can run the backend directly with Python 3.11 or newer:

```bash
git clone https://github.com/fl-grtg/typst-editor.git
cd typst-editor
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml   # optional, edit as needed
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8978 --workers 1
```

Notes:

- Keep `--workers 1`. Live sync keeps its rooms in memory, so the server refuses to start with more than one worker.
- Install the Typst CLI (the version pinned as `TYPST_VERSION` in the [`Dockerfile`](../../Dockerfile)) and put it on `PATH`. Without it, server-side export and rendering for agents are unavailable. Editing and the in-browser preview still work.
- Without Docker, the invite code is not generated for you. Set `REGISTRATION_INVITE_TOKEN` in `config.toml` or the environment before creating accounts.
- For development with auto-reload, see [Development](../development/development.md).

## Next steps

- [Configuration](configuration.md): quotas, rate limits, registration mode.
- [Reverse proxy and TLS](reverse-proxy.md): make the app reachable under your domain.
- [Single Sign-On (OIDC)](single-sign-on.md): use your identity provider for logins.
- [Backups and upgrades](backup-and-upgrades.md): keep your data safe.
