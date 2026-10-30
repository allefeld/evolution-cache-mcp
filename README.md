# evolution-cache-mcp

MCP server that exposes a local Evolution mail cache (SQLite metadata +
Maildir-style message bodies) as tools for an LLM agent: `list_folders`,
`search`, `get_body`.

Each server instance is scoped to a single mail account, configured through
environment variables — run one instance per account.

## Configuration

| Env var             | Description                                                                                                                                              |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `EVO_MAIL_PATH`     | Full path to the account's cache dir (contains `folders.db` and `folders/`)                                                                              |
| `EVO_MAIL_UID_TYPE` | `ews` (body filename is `sha256(uid)`), `imap` (filename is the uid itself), or `graph` (filename is `md5(uid)`, for Microsoft365/Graph-backed accounts) |
| `EVO_MAIL_INFO`     | Free-text account identity, e.g. `"Organisation, you@example.ac.uk"` — used only in the server's agent-facing instructions                           |

Account cache directories live under `~/.cache/evolution/mail/<account-id>/`.

## Install

```
uv tool install git+https://github.com/allefeld/evolution-cache-mcp
```

(or `pipx install git+https://github.com/allefeld/evolution-cache-mcp`)

This installs the `evolution-cache-mcp` command, which speaks MCP over stdio.

## Tools

- `list_folders()` — folder names with total/unread counts.
- `search(folder, ...)` — filter by unread/flagged, date range, sender/subject/body
  substring; paginate with `limit`/`offset`; pick columns with `fields`
  (`preview` is omitted by default — set `include_preview=true` to include it).
- `get_body(folder, id, ...)` — full plain-text body of one message, paginated
  with `max_lines`/`offset_lines`. `id` is the short id from `search`, not the
  raw mailbox UID.

## Notes

- Read-only — no mark-as-read/unread, move, delete, or flag actions.
- `search` operates on one folder at a time; there's no cross-folder/"all
  mail" search.
- Exchange/Outlook "external sender" banners are stripped from preview/body
  text before they count toward response size.
- See `CLAUDE.md` for design rationale and deferred features.

## Example `mcpServers` config

```json
{
  "mcpServers": {
    "evo-mail-work": {
      "command": "evolution-cache-mcp",
      "env": {
        "EVO_MAIL_PATH": "/home/you/.cache/evolution/mail/<work-account-id>",
        "EVO_MAIL_UID_TYPE": "ews",
        "EVO_MAIL_INFO": "Work, you@example.com"
      }
    },
    "evo-mail-personal": {
      "command": "evolution-cache-mcp",
      "env": {
        "EVO_MAIL_PATH": "/home/you/.cache/evolution/mail/<personal-account-id>",
        "EVO_MAIL_UID_TYPE": "imap",
        "EVO_MAIL_INFO": "Personal, you@example.com"
      }
    }
  }
}
```

***

This software was developed by Carsten Allefeld using Claude Code as a coding agent. To the extent any copyright subsists in the code, it is released under the terms of the GNU General Public License, version 3 or later.