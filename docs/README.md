# Typst Editor Documentation

This folder holds the documentation for Typst Editor, a self-hosted, collaborative Typst editor. Start with [Installation](self-hosting/installation.md), then pick the pages you need.

## Contents

### Running a server

1. [Installation](self-hosting/installation.md): run the container with Docker or Compose, create the first account, run without Docker.
2. [Configuration](self-hosting/configuration.md): every setting, where to put it, defaults and startup checks.
3. [Reverse proxy and TLS](self-hosting/reverse-proxy.md): expose the app on a domain with Caddy or nginx, proxy trust and cookies.
4. [Single Sign-On (OIDC)](self-hosting/single-sign-on.md): log in through Authentik, Authelia, Kanidm or any OpenID Connect provider.
5. [Backups and upgrades](self-hosting/backup-and-upgrades.md): back up and restore `DATA_DIR`, upgrade the image, rotate the invite code.

### Using the app

6. [Using the editor](usage/using-the-editor.md): projects, documents and files, roles, review and proposals, checkpoints, export.
7. [Agents and MCP](usage/agents-and-mcp.md): API keys, key modes, connecting Claude Code, OpenCode and other MCP clients.

### Working on the code

8. [Development](development/development.md): local setup, repository layout, tests and the frontend build.

## Other files in the repository

- [README](../README.md): project overview and quick start.
- [CONTRIBUTING.md](../CONTRIBUTING.md): how to propose changes.
- [SECURITY.md](../SECURITY.md): how to report vulnerabilities, hardening notes for operators.
- [`.env.example`](../.env.example) and [`config.example.toml`](../config.example.toml): every setting with a one-line description.
