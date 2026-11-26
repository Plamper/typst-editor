# Backups and upgrades

[Back to the documentation index](../README.md)

## What to back up

Everything lives in `DATA_DIR` (`/app/data` in the container):

| Path | Content |
|---|---|
| `app.db` | SQLite database: accounts, projects, documents, comments, versions, API keys. |
| `files/` | Uploaded project files (images, bibliographies, fonts, modules). |
| `git/` | Git repositories of projects that save their versions with Git. |
| `.secret_key` | Key that encrypts the Git access tokens in `app.db`. Kept out of the backup archive on purpose; without it, project owners enter their tokens again. |
| `.invite_token` | Generated invite code, if you did not set `REGISTRATION_INVITE_TOKEN`. |
| `backup/` | Backups written by the backup script. |

Your `.env` or `config.toml` holds the rest of the state worth keeping.

## Creating a backup

[`scripts/backup.py`](../../scripts/backup.py) is part of the image. It writes a consistent copy of the database (`VACUUM INTO`, safe while the app is running), checks it with `PRAGMA integrity_check` and archives `files/` and `git/` next to it:

```bash
docker compose exec app python scripts/backup.py
```

Output in `DATA_DIR/backup/`:

- `app-YYYYMMDD-HHMMSS.db`
- `app-YYYYMMDD-HHMMSS-files.tar.gz`

Options:

| Option | Description |
|---|---|
| `--keep N` | Keep the newest `N` backups, delete older ones (default `7`, minimum `1`). |
| `--no-include-files` | Back up only the database. |
| `--dry-run` | Print the target paths without writing anything. |

Backups are `chmod 600` but not encrypted. They contain password hashes and sessions. Copy them off the host and encrypt them, for example:

```bash
docker compose cp app:/app/data/backup ./typst-backup
age --encrypt -r <recipient> -o app.db.age typst-backup/app-20260101-030000.db
```

### Scheduling

Run the script from the host's cron or a systemd timer, for example nightly at 03:00:

```cron
0 3 * * * cd /srv/typst-editor && docker compose exec -T app python scripts/backup.py --keep 14
```

## Restoring a backup

There is no restore command; the backup script never replaces live data. To restore manually:

1. Stop the app: `docker compose stop app`.
2. Copy the backup files into a place you can reach, for example with a helper container:

   ```bash
   docker run --rm -v typst-data:/data -v "$PWD/typst-backup:/backup" alpine sh -c '
     cd /data &&
     mkdir -p pre-restore && mv app.db app.db-wal app.db-shm files pre-restore/ 2>/dev/null;
     cp /backup/app-20260101-030000.db app.db &&
     tar -xzf /backup/app-20260101-030000-files.tar.gz -C /data &&
     chown -R 999:999 /data'
   ```

   This moves the current data to `pre-restore/` instead of deleting it. Remove that folder once the restore is confirmed.
3. Start the app: `docker compose start app`, and check that documents and files are back.

Test a restore on a separate volume from time to time, so you know the backups work.

## Upgrading

The image tag `main` follows the rolling `main` build. To upgrade:

```bash
docker compose exec app python scripts/backup.py   # back up first
docker compose pull
docker compose up -d
```

With plain Docker, `docker pull ghcr.io/fl-grtg/typst-editor:main`, then remove and re-create the container with the same volume.

Database migrations run automatically on startup and are append-only. If the app cannot initialize `DATA_DIR` after an upgrade, it logs the reason and exits; check volume permissions and free disk space, and restore the backup you made before the upgrade if needed.

## Rotating the invite code

- If you set `REGISTRATION_INVITE_TOKEN`: change the value in `.env` and run `docker compose up -d`.
- If the code was generated: the entrypoint only generates and applies it on the start that creates the database, so after a restart it is no longer active. Set `REGISTRATION_INVITE_TOKEN` explicitly to enable sign-up with a code again.

Existing accounts and project invite links are not affected.

## Logs

```bash
docker compose logs -f app
```

Compose keeps three rotated log files of 10 MB each.
