---
name: typst-editor
description: Work on the user's self-hosted Typst projects through MCP (ls, read, write, edit, move, delete, search, render, export, upload, comment, propose, inbox). Use for any document or project file in the typst-editor server.
---

# Typst Editor (MCP)

The server describes its tools itself; this file only holds the workflow and the rules.
Tool errors are JSON `{code, message, hint, ...}`; a 200 does not mean success.

## Setup

If the server is not connected yet: ask the user for the server address (as reachable from where you run) and an API key (Settings → API keys), then register `<server>/mcp` as a Streamable HTTP MCP server in your own client with the header `Authorization: Bearer tpe_...`. Find out how your client does that; keep the key out of committed files. Check with `ls("/")`.

## Model

- Each project is **one file tree** of folders, documents and files. A document is `/{Project}/{folder}/{Title}` and **is its title** (a trailing `.typ` is accepted when no file has that name); there is no main.typ. Everything else is a file at its path: `/P/Chapters/part.typ`, `/P/img/logo.png`, `/P/refs.bib`. A new path with a suffix creates a file, without one a document.
- Quoted paths in Typst are **relative to the folder** of the document or `.typ` file that contains them: a document in `Chapters/` reads `#include "part.typ"` from `/P/Chapters/part.typ` and `#image("../img/logo.png")` from `/P/img/logo.png`; `"/img/logo.png"` starts at the project root. Documents cannot be included; `outline` shows what each import resolves to.
- Proposals sit under `/P/.proposals/`, private templates under `/.templates/`, deleted things under `/.trash/`. Other people's projects are `Title@owner`.
- Copy paths from `ls`/`search`, never guess. `version` (from `read`) is a content hash of the live text.

## Loop

`ls` → `read(outline=true)` → `edit` / `write` → `render` (errors) → `render(pages="1-3")` to look → `export`.

- `read` shows open comments (id, line, quote). Answer with `comment(reply_to=…)`, close with `comment(id, resolved=true)`.
- `edit`: `old` is exact text with 2-3 lines of context; all edits of one call apply atomically. Prefer `edit` over `write`.
- `write` over an existing text needs `base_version` (or `force=true`). Binary files: `content_base64`, or `upload` (one-time curl link) above ~100 KB. `copy_from` copies server-side.
- `move` renames/moves documents, files, folders (paths end with `/`), projects; quoted references keep reaching their files (also for moved folders and documents moved to another folder; `updated_references` counts the texts changed). A `.typ` file moved to a document path becomes a document (`broken_references` lists texts that still include it); a document moved to a `.typ` path becomes a file.
- `render` without `pages` is the cheap check; fix every error before looking at images.
- `export` returns a `url` valid for 5 minutes (curl it) and, when small, `content_base64`; `upload` links work once. Roughly 40 calls per 16 s are allowed.

## Proposals

In shared projects, for reviewer keys and keys in mode `propose`, `edit`/`write`/`move`/`delete` on documents and files do not change anything: they add to a **proposal package** that humans accept hunk by hunk.

- Send related changes as **one package**: `propose(project, ops=[…], title)`, or pass `package=<id>` to every following call. Never one package per line.
- Correct a package by adding to it, or remove it: `delete("/P/.proposals/<id>")`. Your own comments go with `comment(id, delete=true)`.
- Nothing is applied before a human accepts it. `inbox` tells you the verdict (accepted, partly accepted, discarded).

## Errors

| code | do |
| --- | --- |
| `anchor_ambiguous` | add context from `candidates`, or `all=true` |
| `anchor_gone` | `read` again; `closest` points at the likely line |
| `stale_version` | `read` again, redo the change |
| `compile_error` | fix `diagnostics` (path, line, col), `render` again |
| `forbidden` | ask the human for a stronger key or a project share |
| `rate_limited` / `busy` | wait `retry_after`, retry once |
| `too_large` | text ≤ 200 KB, files ≤ 10 MB; use `upload` |

`inbox(wait=25)` holds until a mention of `@<your key name>` or a review result arrives; `ack` what you handled.
