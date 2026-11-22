# Development

[Back to the documentation index](../README.md)

This page is a short overview. The rules for contributions are in [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Stack

- Backend: Python, FastAPI and uvicorn, a single process with one worker.
- Live sync: Yjs documents on the server through `pycrdt`, over WebSockets.
- Storage: SQLite plus a `files/` directory in `DATA_DIR`.
- Agent access: an MCP server built with FastMCP, mounted at `/mcp`.
- Frontend: one generated `index.html` with CodeMirror and the Typst compiler as WebAssembly. No Node at runtime.

## Local setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
make dev      # http://127.0.0.1:8978, data in ./data, registration open
```

`make dev` rebuilds the frontend and runs uvicorn with auto-reload. It sets `REGISTRATION=open` so you can create accounts without an invite code. `make help` lists all targets.

## Make targets

| Target | What it does |
|---|---|
| `make dev` | Build the frontend and run the app with reload. |
| `make test` | Backend tests (`pytest`). |
| `make lint` | `ruff check`. |
| `make typecheck` | `mypy`. |
| `make web` | Build `index.html` from `web/`. |
| `make web-check` | Fail if `index.html` is out of date. |
| `make i18n-check` | Fail on missing translation keys or hardcoded strings. |
| `make vendor` | Download pinned frontend assets into `vendor/` (needs network). |
| `make e2e` | Browser smoke tests with Playwright. |
| `make check` | Everything CI runs: lint, typecheck, web-check, i18n-check, test. |

## Repository layout

| Path | Content |
|---|---|
| `backend/main.py` | App setup: lifespan, middleware, router registration. |
| `backend/routers/` | HTTP and WebSocket endpoints, one module per area. |
| `backend/services/` | Shared logic: access checks, quotas, checkpoints, proposals, OIDC, and more. |
| `backend/mcp_*.py` | MCP server and its tools. |
| `backend/config.py` | All settings, defaults and parsing. |
| `backend/db.py`, `backend/schema.sql` | Database setup and append-only migrations. |
| `web/` | Frontend sources, see [`web/README.md`](../../web/README.md). |
| `index.html` | Generated frontend. Do not edit by hand. |
| `cm-build/`, `vendor-cm.js` | CodeMirror bundle and its build. |
| `vendor/` | Pinned third-party assets and fonts served by the app. |
| `scripts/` | Backup, vendoring and maintenance scripts. |
| `skills/typst-editor/` | Skill for MCP agents. |
| `tests/`, `e2e/` | Backend tests and browser smoke tests. |

## Frontend changes

Edit files under `web/`, then rebuild and commit the regenerated `index.html` together with your change:

```bash
python web/build.py           # regenerate index.html
python web/build.py --check   # what CI runs
```

## Tests

```bash
pytest -q                                   # backend tests
pip install -r requirements-e2e.txt && playwright install chromium
pytest e2e -q -n 4                          # browser tests, need internet for the Typst compiler CDN
```

## Building the image

```bash
docker build -t typst-editor:dev .
```

The Dockerfile installs the pinned Typst CLI for `amd64` and `arm64`.

## Maintenance scripts

- [`scripts/backup.py`](../../scripts/backup.py): database and files backup, see [Backups and upgrades](../self-hosting/backup-and-upgrades.md).
- [`scripts/login_token.py`](../../scripts/login_token.py): create, list and revoke persistent `#token=` login links for a user. These links are not exposed in the web UI. Run it with the same `DATA_DIR` as the app.
- [`scripts/vendor.py`](../../scripts/vendor.py): refresh pinned frontend assets in `vendor/`.
