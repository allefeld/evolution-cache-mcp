# evolution-cache-mcp

MCP server exposing one Evolution mail account's local cache (SQLite metadata
+ Maildir bodies) as read-only tools. One server process per account,
configured via `EVO_MAIL_PATH` / `EVO_MAIL_UID_TYPE` / `EVO_MAIL_INFO` env
vars — see README.md.

## Design notes

- `search()` returns a short `id` per message computed as `sha256(uid)[:12]`
  — deterministic, not a session counter or list position. This is
  deliberate: an earlier version handed out positional ids ("1", "2", ...)
  that got silently invalidated whenever another `search()` ran on the same
  folder before `get_body()` was called. Don't reintroduce counter/position
  based ids without solving that problem again.
- `get_body()` resolves that id back to the real uid via `_id_index`, a
  per-folder reverse map (`SELECT uid FROM "<folder>"`, each hashed once)
  built lazily and cached for the life of the process. It also accepts a
  literal raw uid directly, as a fallback.
- `_cur_index` (Maildir `cur/` listing) and `_id_index` are both built once
  per folder and never invalidated. Correct for a read-only server reading
  an on-disk snapshot within one session; would need rethinking if the
  server ever has to notice new mail arriving mid-session.
- `_strip_boilerplate()` removes Exchange's "You don't often get email
  from..." and "CAUTION: this email originated from outside..." banners from
  preview/body text. It's regex-based and best-effort — it will miss banner
  wordings not yet seen.

## Deferred features

Feedback from real agent usage (2026-07-07) surfaced two feature requests
that were deliberately deferred rather than folded into the existing tools,
because both need more than an incremental change:

- **Write actions** (mark-as-read, delete, move, flag). The server currently
  only ever opens `folders.db` and the Maildir `cur/` tree read-only. Writing
  back would mean either mutating files that Evolution's sync process owns
  and actively rewrites (real risk of corrupting Evolution's own state), or
  integrating with `evolution-data-server` over D-Bus — a fundamentally
  different transport than "read some files on disk." Worth a dedicated
  design pass, not a bolt-on.

- **Cross-folder / "all mail" search** (`folder=None` meaning search every
  folder). Doable, but each folder is a separate SQL table, so this means
  fanning out one query per folder (a busy account here has 18+ folders,
  some with 40k+ rows), merging results, and re-applying `limit`/`offset`
  globally after the merge. That's a real feature with its own performance
  and correctness surface, not a one-line addition to `search()`.

Both are candidates for a follow-up iteration if they turn out to matter in
practice.
