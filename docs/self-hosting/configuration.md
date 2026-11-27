# Configuration

[Back to the documentation index](../README.md)

## Where settings come from

Every setting can be set in two places:

1. `config.toml` in the application root (next to `backend/`). Copy [`config.example.toml`](../../config.example.toml) as a starting point.
2. Environment variables with the same name.

Environment variables win over `config.toml`. Values that are missing or invalid fall back to the defaults below; out-of-range numbers log a warning and use the default.

With Docker Compose, the usual place is a `.env` file next to `compose.yml`, which Compose passes into the container. [`.env.example`](../../.env.example) lists every variable. The application does not read `.env` by itself when you run it without Compose.

The single source of truth for names, types, defaults and ranges is [`backend/config.py`](../../backend/config.py).

## Server and network

| Setting | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Compose sets `0.0.0.0` inside the container; keep that when you publish ports. |
| `PORT` | `8978` | Listen port. Compose pins `8978`; change `environment.PORT` and `ports` in `compose.yml` together. |
| `DATA_DIR` | `./data` | Directory for the SQLite database and files. Compose forces `/app/data`. |
| `TRUST_PROXY` | `false` | Trust `X-Forwarded-Proto` and `X-Forwarded-For` from a reverse proxy. See [Reverse proxy and TLS](reverse-proxy.md). |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Peers allowed to send `X-Forwarded-*` headers, comma separated, CIDR allowed. Never `*` together with `TRUST_PROXY=true`. |
| `COOKIE_SECURE` | `auto` | `auto`, `true` or `false`. `auto` sets the `Secure` flag when the request arrived over HTTPS. |

## Accounts and registration

| Setting | Default | Description |
|---|---|---|
| `REGISTRATION` | `invite-only` | `closed`, `invite-only` or `open`. Use `open` only for local tests, never on the public internet. Invalid values fail closed to `closed`. |
| `REGISTRATION_INVITE_TOKEN` | empty | Invite code for sign-up. Minimum 16 characters (`openssl rand -hex 32`). With Docker, an empty value on a fresh data directory makes the entrypoint generate one for the first start only. |
| `SESSION_SECONDS` | `1209600` | Session lifetime in seconds (14 days). Range: 1 hour to 90 days. |

Single Sign-On settings (`OIDC_*` and `DISABLE_PASSWORD_LOGIN`) have their own page: [Single Sign-On (OIDC)](single-sign-on.md).

## Quotas

| Setting | Default | Description |
|---|---|---|
| `MAX_DOCS_PER_USER` | `100` | Maximum number of documents per user. |
| `MAX_BYTES_PER_USER` | `524288000` | Maximum storage per user in bytes (500 MB). |
| `MAX_FILES_PER_DOC` | `1000` | Maximum number of files per project. The name is kept for older configs. |

Fixed limits that are not configurable: a document text can be up to 200 KB, a single uploaded file up to 10 MB, and a request body up to 32 MB.

## Rate limits

Rate limits are counted per client and endpoint group. All values are requests per minute, except `RATE_REGISTER_PER_HOUR`. Valid range: 1 to 10000.

| Setting | Default | Applies to |
|---|---|---|
| `RATE_LOGIN_PER_MIN` | `10` | Login attempts |
| `RATE_REGISTER_PER_HOUR` | `20` | Registrations (per hour) |
| `RATE_AUTH_PER_MIN` | `60` | Session checks and logout |
| `RATE_JOIN_PER_MIN` | `30` | Invite redemptions |
| `RATE_READ_PER_MIN` | `30` | Account-less read-link views |
| `RATE_SEARCH_PER_MIN` | `60` | Searches |
| `RATE_FILES_PER_MIN` | `20` | File uploads and deletions |
| `RATE_FILE_READ_PER_MIN` | `600` | File downloads, including preview inputs |
| `RATE_FILES_LIST_PER_MIN` | `120` | File list refreshes |
| `RATE_SIDEBAR_PER_MIN` | `120` | Sidebar refreshes |
| `RATE_SAVE_PER_MIN` | `30` | Document saves |
| `RATE_COMMENTS_PER_MIN` | `30` | Comment writes |
| `RATE_EXPORT_PER_MIN` | `5` | ZIP exports |
| `RATE_EXPORT_DOC_PER_MIN` | `10` | Per-document exports (PDF, SVG, PNG, ZIP) |
| `RATE_PW_PER_MIN` | `10` | Password changes |
| `RATE_INVITE_PER_MIN` | `10` | Invite creations |
| `RATE_SHARE_PER_MIN` | `10` | Share changes |
| `RATE_SNAPSHOTS_PER_MIN` | `60` | Version reads and writes |
| `RATE_CREATE_PER_MIN` | `20` | Document creations |
| `RATE_DUPLICATE_PER_MIN` | `10` | Document duplications |
| `RATE_AVATAR_PER_MIN` | `10` | Avatar uploads |
| `RATE_FOLDERS_PER_MIN` | `20` | Folder operations |
| `RATE_KEYS_PER_MIN` | `30` | API key management |
| `RATE_MCP_PER_MIN` | `60` | MCP tool calls, per API key |
| `RATE_RENAME_PER_MIN` | `10` | Document renames |
| `RATE_DELETE_PER_MIN` | `10` | Document deletions |
| `RATE_RESTORE_PER_MIN` | `10` | Restores from trash |
| `RATE_MOVE_PER_MIN` | `20` | Document moves |
| `RATE_TEMPLATES_PER_MIN` | `20` | Template saves, deletes and moves |
| `RATE_TPLFOLDERS_PER_MIN` | `20` | Template folder operations |

Behind a reverse proxy, rate limits only work per client if proxy headers are trusted. Otherwise all users share the proxy's bucket. See [Reverse proxy and TLS](reverse-proxy.md).

## Startup checks

The server refuses to start in these cases, so a broken config never runs silently:

- `REGISTRATION_INVITE_TOKEN` is set but shorter than 16 characters.
- `TRUST_PROXY=true` with an open `FORWARDED_ALLOW_IPS` (`*`, `0.0.0.0/0` or `::/0`).
- `DISABLE_PASSWORD_LOGIN=true` without a complete OIDC configuration.
- `OIDC_REDIRECT_URI` is set but does not start with `http://` or `https://`.
- More than one worker process is configured.

It logs a warning (and keeps running) when:

- `REGISTRATION=invite-only` has no invite token, so nobody can register.
- Only one of `OIDC_ISSUER_URL` and `OIDC_CLIENT_ID` is set, so OIDC stays disabled.
- The Typst CLI is not on `PATH`, so server-side export and rendering are unavailable.

## Example `.env`

```dotenv
REGISTRATION=invite-only
REGISTRATION_INVITE_TOKEN=replace-with-output-of-openssl-rand-hex-32
TRUST_PROXY=true
FORWARDED_ALLOW_IPS=127.0.0.1,::1,172.16.0.0/12
MAX_BYTES_PER_USER=1073741824
```

After changing `.env`, recreate the container:

```bash
docker compose up -d
```
