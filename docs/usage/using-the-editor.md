# Using the editor

[Back to the documentation index](../README.md)

## How it works

The editor shows the Typst source on one side and the compiled PDF on the other. Changes sync live between everyone in the project, and presence indicators show who is working where. The PDF is compiled in each user's browser with Typst as WebAssembly, so there is no compile service on the server.

## Projects, documents and files

A project is one file tree of folders, documents and files.

- A **document** is a Typst text that compiles on its own. It is identified by its title and its folder, for example `Chapters/Introduction`. There is no `main.typ`; a project can contain several documents, and each one compiles independently.
- A **file** is everything else: Typst modules (`part.typ`), bibliographies (`refs.bib`), images, fonts and so on.
- Folders can be nested up to two levels inside a project.

Paths in Typst code are relative to the folder of the document or `.typ` file that contains them:

```typst
// In a document inside Chapters/
#include "part.typ"              // Chapters/part.typ
#image("../img/logo.png")        // img/logo.png
#image("/img/logo.png")          // also img/logo.png, from the project root
```

Documents cannot be included in other documents; put shared content into `.typ` files instead.

Packages from the Typst package registry (`#import "@preview/..."`) are loaded by the browser from `packages.typst.org`.

### Selecting several entries

Files, documents and folders in the sidebar can be selected together, also across projects:

- Ctrl+click (Cmd+click on macOS) adds or removes an entry, Shift+click selects a range, Ctrl+Shift+click adds a range.
- Up/Down move between rows, Shift+Up/Down extend the selection, Ctrl+Space toggles a row, Ctrl+A selects every visible entry and Esc clears the selection.
- The menu of a selected row (three dots or right-click) duplicates, copies, moves or trashes the whole selection. Dragging a selected row takes all of them along, Delete moves them to the trash.

### Templates and trash

- Save a document as a personal template and start new documents from it.
- Deleted documents, files and templates go to the trash first and can be restored from there.

## Roles

Every project has one owner. The owner can share the project with other users as **editor** or **reviewer**, directly or through an invite link (valid for 7 days).

| Role | Can do |
|---|---|
| Owner | Everything, including sharing, removing members and deleting the project. |
| Editor | Edit documents and files, accept or discard proposals. |
| Reviewer | Read, comment on lines, @-mention people and propose changes. Cannot change the source directly. |

Downgrading a member from editor to reviewer takes effect immediately, including in open editor sessions.

## Review

- **Comments** are attached to lines. Reply to them and resolve them when done.
- **Mentions**: write `@username` in a comment to notify someone.
- **Proposals**: reviewers (and agents in proposal mode) hand in changes as a proposal package. Owners and editors look at the diff and accept or discard it hunk by hunk. Nothing changes before a human accepts it.
- **Inbox**: mentions, proposals and review results arrive in your inbox.

## Checkpoints

Checkpoints are versions of the whole project, including its files. You can compare a checkpoint with the current state and roll back to it.

- Manual checkpoints are kept until you delete them.
- Automatic checkpoints are thinned out: the newest 10 are kept, and of the older ones one per week up to five weeks back. The first checkpoint of a project is always kept.

Each checkpoint stores a full copy of the project's files, so checkpoints count toward the storage of a project.

## Export

- A single document as `.typ`, PDF, SVG or PNG.
- A whole project as ZIP.

## Account settings

In the settings you can change your password, upload an avatar, manage API keys for agents (see [Agents and MCP](agents-and-mcp.md)) and adjust editor preferences such as the spell-check language.

## Limits

Your administrator sets per-user quotas for documents and storage, see [Configuration](../self-hosting/configuration.md). Independent of those, a document text can be up to 200 KB and a single file up to 10 MB.
