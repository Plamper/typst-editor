# Agents and MCP

[Back to the documentation index](../README.md)

Typst Editor has a built-in [Model Context Protocol](https://modelcontextprotocol.io) server. AI agents such as Claude Code, OpenCode and other MCP clients can read and edit your projects, render pages, export documents, comment and hand in proposals. They work on the live text, so people typing at the same time are not disturbed.

## Endpoint

- URL: `https://typst.example.com/mcp` (your server address plus `/mcp`).
- Transport: Streamable HTTP.
- Authentication: `Authorization: Bearer tpe_...` with an API key.

## API keys

Create keys in the app under **Settings > API keys**. The full key is shown once; the server only stores its hash.

Each key has:

| Property | Values | Meaning |
|---|---|---|
| Name | free text | Shown as the author of the agent's comments and proposals. Agents can be @-mentioned by this name. |
| Role | `editor`, `reviewer` | Upper limit for what the key may do. The effective right in a project is the lower of the key role and your own role there. A key never gets owner rights. |
| Mode | `auto`, `direct`, `propose` | How changes are applied, see below. |
| Expiry | optional | The key stops working after this date. |

Keys can be revoked at any time. The app keeps a log of the last 500 tool calls per key.

### Modes

| Mode | Behaviour |
|---|---|
| `auto` (default) | Edits apply directly in projects nobody else is a member of. In shared projects, changes become proposals. |
| `direct` | Edits apply directly wherever the key's role allows it. |
| `propose` | Changes to documents and files always become proposals. |

Reviewer keys always create proposals. Creating projects, folders and personal templates is never turned into a proposal.

A key cannot create or switch to a key with more rights or a freer mode than its own.

## Connecting a client

### Claude Code

```bash
claude mcp add --transport http typst-editor https://typst.example.com/mcp \
  --header "Authorization: Bearer tpe_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### OpenCode

In `opencode.json`:

```json
{
  "mcp": {
    "typst-editor": {
      "type": "remote",
      "url": "https://typst.example.com/mcp",
      "headers": {
        "Authorization": "Bearer {env:TYPST_EDITOR_KEY}"
      }
    }
  }
}
```

### Other clients

Register a Streamable HTTP MCP server with the URL above and the `Authorization` header. Keep the key out of files you commit. Check the connection by asking the agent to list `/`.

## The skill

[`skills/typst-editor/SKILL.md`](../../skills/typst-editor/SKILL.md) is a short skill that teaches an agent the workflow and rules of this server. Copy the `skills/typst-editor` folder into your client's skills directory (for Claude Code, for example `~/.claude/skills/`).

## Tools

The server describes its tools to the client. Overview:

| Tool | Purpose |
|---|---|
| `ls` | List projects, folders, documents and files. |
| `read` | Read a document or file, optionally with an outline of imports and open comments. |
| `write` | Create or overwrite a document or file. |
| `edit` | Replace exact text passages; all edits of one call apply atomically. |
| `move` | Rename or move documents, files, folders and projects, rewriting references. |
| `delete` | Move something to the trash. |
| `search` | Search across projects. |
| `render` | Compile a document and return errors, or images of selected pages. |
| `export` | Export as PDF, SVG, PNG or ZIP; returns a download link valid for 5 minutes. |
| `upload` | Get a one-time curl link for large files. |
| `comment` | Add, answer, resolve or delete comments. |
| `propose` | Hand in several changes as one proposal package. |
| `inbox` | Wait for mentions and review results, and acknowledge them. |

## Paths

Agents address everything by path:

- `/Project/Folder/Document` for a document (a document is its title).
- `/Project/img/logo.png` for a file.
- `/Project/.proposals/` for proposal packages, `/.templates/` for personal templates, `/.trash/` for deleted items.
- `Title@owner` for projects shared with you by another user.

## Limits

- Tool calls are rate-limited per key, see `RATE_MCP_PER_MIN` in [Configuration](../self-hosting/configuration.md).
- A document text can be up to 200 KB, a file up to 10 MB. Use `upload` for files larger than about 100 KB.
- `render` and `export` need the Typst CLI on the server. The Docker image includes it.

Tool errors are returned as JSON with `code`, `message` and usually a `hint`, for example `stale_version`, `compile_error` or `rate_limited`. The skill lists how an agent should react to each.
