#!/usr/bin/env python3
"""
evolution_cache_mcp.server — MCP server over an Evolution mail cache
(SQLite metadata + Maildir-style message bodies).

Each server process is scoped to a single account, configured via
environment variables:

  EVO_MAIL_PATH      Full path to the account's cache directory
                      (contains folders.db and a folders/ tree).
  EVO_MAIL_UID_TYPE   "ews"  — body filename is sha256(uid)
                      "imap" — body filename IS the uid string
  EVO_MAIL_INFO       Free-text account identity, e.g. "Work, you@example.com"
                      — used only to fill in the server's agent-facing
                      instructions, not in any query or lookup.
"""

import os
import re
import hashlib
import sqlite3
import email
from email import policy
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

# ── Config ────────────────────────────────────────────────────────────────────

def _load_config():
    path = os.environ.get("EVO_MAIL_PATH")
    uid_type = os.environ.get("EVO_MAIL_UID_TYPE")
    info = os.environ.get("EVO_MAIL_INFO")
    if not path:
        raise RuntimeError("EVO_MAIL_PATH environment variable is required")
    if uid_type not in ("ews", "imap"):
        raise RuntimeError('EVO_MAIL_UID_TYPE must be "ews" or "imap"')
    if not info:
        raise RuntimeError("EVO_MAIL_INFO environment variable is required")
    if not os.path.isdir(path):
        raise RuntimeError(f"EVO_MAIL_PATH does not exist or is not a directory: {path}")
    return path, uid_type, info

ACCOUNT_PATH, UID_TYPE, ACCOUNT_INFO = _load_config()
DB_PATH = os.path.join(ACCOUNT_PATH, "folders.db")

# ── Body lookup ───────────────────────────────────────────────────────────────

def _cur_dir(folder):
    return os.path.join(ACCOUNT_PATH, "folders", folder.replace("/", os.sep), "cur")

# Per-run cache: maps cur_dir path → {filename: full_path}
_cur_index: dict[str, dict[str, str]] = {}

def _build_index(c: str) -> dict[str, str]:
    """Walk a cur/ directory once and return filename→full_path index."""
    if c not in _cur_index:
        idx: dict[str, str] = {}
        for root, _dirs, files in os.walk(c):
            for f in files:
                idx[f] = os.path.join(root, f)
        _cur_index[c] = idx
    return _cur_index[c]

def _find_body_file(folder: str, uid) -> str | None:
    target = (hashlib.sha256(str(uid).encode()).hexdigest()
              if UID_TYPE == "ews" else str(uid))
    return _build_index(_cur_dir(folder)).get(target)

# Boilerplate banners Exchange/Outlook injects into message bodies that carry
# no actual content but eat into preview/body length.
_BOILERPLATE_PATTERNS = [
    re.compile(r"You don't often get email from .*?Learn why this is important(?: at \S+)?\.?",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"CAUTION:\s*This email originated from outside(?: of)? (?:the|your|this) organi[sz]ation\.?[^.]{0,300}\.?",
               re.IGNORECASE),
]

def _strip_boilerplate(text: str) -> str:
    for pattern in _BOILERPLATE_PATTERNS:
        text = pattern.sub("", text)
    return text.strip()

def _extract_body(filepath: str, max_lines: int = 60, offset_lines: int = 0) -> tuple[str, bool, list[str]]:
    """Return (body_text, truncated, attachment_filenames) from an RFC822 file."""
    with open(filepath, "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)

    def _text(m):
        if m.is_multipart():
            for part in m.walk():
                if (part.get_content_type() == "text/plain"
                        and "attachment" not in str(part.get("Content-Disposition", ""))):
                    try:
                        return part.get_content()
                    except Exception:
                        raw = part.get_payload(decode=True)
                        if raw:
                            cs = part.get_content_charset() or "utf-8"
                            return raw.decode(cs, errors="replace")
        else:
            try:
                return m.get_content()
            except Exception:
                raw = m.get_payload(decode=True)
                if raw:
                    cs = m.get_content_charset() or "utf-8"
                    return raw.decode(cs, errors="replace")
        return ""

    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition", ""))
            filename = part.get_filename()
            if filename and ("attachment" in disp or part.get_content_disposition() == "attachment"):
                attachments.append(filename)

    text = _strip_boilerplate(_text(msg))
    lines = text.splitlines()
    window = lines[offset_lines:offset_lines + max_lines]
    truncated = offset_lines + max_lines < len(lines)
    return "\n".join(window), truncated, attachments

# ── Database ──────────────────────────────────────────────────────────────────

def _connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

def _query(table: str, filters: dict, limit: int, offset: int) -> list[dict]:
    con = _connect()
    con.row_factory = sqlite3.Row
    clauses, params = ["deleted = 0"], []

    if filters.get("unread"):
        clauses.append("read = 0")
    if filters.get("flagged"):
        clauses.append("important = 1")
    if filters.get("since") is not None:
        clauses.append("dsent >= ?"); params.append(filters["since"])
    if filters.get("until") is not None:
        clauses.append("dsent <= ?"); params.append(filters["until"])
    if filters.get("from_search"):
        clauses.append("mail_from LIKE ?"); params.append(f"%{filters['from_search']}%")
    if filters.get("subject_search"):
        clauses.append("subject LIKE ?"); params.append(f"%{filters['subject_search']}%")
    if filters.get("body_search"):
        clauses.append("preview LIKE ?"); params.append(f"%{filters['body_search']}%")

    safe = table.replace('"', '""')
    sql = (f'SELECT uid, subject, mail_from, mail_to, dsent, read, replied, '
           f'important, attachment, preview '
           f'FROM "{safe}" WHERE {" AND ".join(clauses)} '
           f'ORDER BY dsent DESC LIMIT ? OFFSET ?')
    params.append(limit)
    params.append(offset)

    try:
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as e:
        con.close()
        raise ValueError(f"Folder '{table}' not found or query error: {e}")
    con.close()
    return rows

def _list_folders() -> list[tuple]:
    con = _connect()
    rows = con.execute(
        "SELECT folder_name, visible_count, unread_count FROM folders ORDER BY folder_name"
    ).fetchall()
    con.close()
    return rows

def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"

def _parse_date(s: str, end_of_day: bool = False) -> int:
    if end_of_day:
        return int(datetime.strptime(s + " 23:59:59", "%Y-%m-%d %H:%M:%S").timestamp())
    return int(datetime.strptime(s, "%Y-%m-%d").timestamp())

# ── Field selection ───────────────────────────────────────────────────────────

_DEFAULT_FIELDS = ["id", "date", "from", "subject", "read", "flagged", "replied", "attachment"]
_ALL_FIELDS = set(_DEFAULT_FIELDS) | {"to", "preview"}

def _row_to_record(row: dict, msg_id: str, fields: list[str]) -> dict[str, Any]:
    values = {
        "id": msg_id,
        "date": _fmt_ts(row["dsent"]),
        "from": row["mail_from"],
        "to": row["mail_to"],
        "subject": row["subject"],
        "read": bool(row["read"]),
        "flagged": bool(row["important"]),
        "replied": bool(row["replied"]),
        "attachment": bool(row["attachment"]),
        "preview": _strip_boilerplate((row["preview"] or "").replace("\n", " "))[:300],
    }
    return {f: values[f] for f in fields}

# search() hands out a short id per message instead of the raw UID (which for
# EWS accounts can run 150+ characters). The id is a truncated hash of the
# UID, not a position or counter, so it's the same value every time for a
# given message: stable across restarts and unaffected by other search()
# calls in between — nothing to invalidate.
_ID_HASH_LEN = 12  # hex chars; collision odds are negligible up to ~10^5 messages/folder

def _short_id(uid) -> str:
    return hashlib.sha256(str(uid).encode()).hexdigest()[:_ID_HASH_LEN]

# Per-folder reverse index: short id → real uid. Built lazily by scanning all
# uids in a folder once; unlike a per-search cache this covers the whole
# folder, so it stays valid regardless of which/how many searches ran.
_id_index: dict[str, dict[str, str]] = {}

def _build_id_index(folder: str) -> dict[str, str]:
    if folder not in _id_index:
        con = _connect()
        safe = folder.replace('"', '""')
        try:
            uids = [str(r[0]) for r in con.execute(f'SELECT uid FROM "{safe}"').fetchall()]
        except sqlite3.OperationalError as e:
            con.close()
            raise ValueError(f"Folder '{folder}' not found or query error: {e}")
        con.close()
        _id_index[folder] = {_short_id(u): u for u in uids}
    return _id_index[folder]

def _resolve_id(folder: str, msg_id: str) -> str:
    """Resolve a short id back to its real uid. Falls back to treating msg_id
    as a literal raw uid if it isn't a known short id for this folder."""
    return _build_id_index(folder).get(msg_id, msg_id)

# ── MCP server ────────────────────────────────────────────────────────────────

_INSTRUCTIONS = f"""\
This server exposes the mailbox for {ACCOUNT_INFO} via a local, read-only
Evolution cache — only what Evolution has already synced to disk, no live
IMAP/Exchange access.

Call list_folders() first if you don't already know the folder name you
need; naming conventions vary by account (e.g. "Inbox" vs "INBOX", "Junk
Email" vs "Spam") and shouldn't be assumed.

search() requires a folder argument — there is no cross-folder/all-mail
search. Iterate per folder if you need to check several.

Each search result carries a short id (not the raw mailbox uid), stable
across restarts — pass it to get_body(folder, id) for the full text.

preview is omitted from search results by default; set include_preview=true
to add it, or use fields to request exactly the columns you need. Leave
preview off for folders you expect to hold many messages, to keep responses
small.

No write actions are supported (no mark-as-read/unread, move, delete,
flag), since this server only reads a local cache file — writing to it
risks corrupting Evolution's own sync state.

Common patterns:
  list_folders()
  search(folder="<folder>", unread=True)
  search(folder="<folder>", from_search="<sender>")
  search(folder="<folder>", subject_search="<keyword>")
  get_body(folder="<folder>", id="<id from search>")
  search(folder="<folder>", since="2026-01-01", unread=True)
"""

mcp = FastMCP("evolution-cache-mcp", instructions=_INSTRUCTIONS)

@mcp.tool()
def list_folders() -> list[dict[str, Any]]:
    """List mail folders in this account with total and unread message counts."""
    return [
        {"folder": name, "total": total, "unread": unread}
        for name, total, unread in _list_folders()
    ]

@mcp.tool()
def search(
    folder: str,
    unread: bool = False,
    flagged: bool = False,
    since: str | None = None,
    until: str | None = None,
    from_search: str | None = None,
    subject_search: str | None = None,
    body_search: str | None = None,
    fields: list[str] | None = None,
    include_preview: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Search messages in a folder (use list_folders for valid folder names).

    since/until are dates in YYYY-MM-DD format. from_search/subject_search/
    body_search are substring matches. offset/limit paginate large result sets.

    Each result has a short "id" derived from the message's UID — stable
    across restarts and unaffected by other searches — pass it to
    get_body(folder, id) to fetch full text.

    fields restricts the returned columns to a subset of:
    id, date, from, to, subject, read, flagged, replied, attachment, preview.
    If omitted, all of those except "to" and "preview" are returned;
    set include_preview=true to add "preview" without listing fields explicitly.
    """
    if fields is not None:
        unknown = set(fields) - _ALL_FIELDS
        if unknown:
            raise ToolError(f"Unknown field(s): {sorted(unknown)}. Valid fields: {sorted(_ALL_FIELDS)}")
        selected_fields = fields
    else:
        selected_fields = _DEFAULT_FIELDS + (["preview"] if include_preview else [])

    filters = {"unread": unread, "flagged": flagged}
    if from_search:
        filters["from_search"] = from_search
    if subject_search:
        filters["subject_search"] = subject_search
    if body_search:
        filters["body_search"] = body_search
    if since:
        filters["since"] = _parse_date(since)
    if until:
        filters["until"] = _parse_date(until, end_of_day=True)

    try:
        rows = _query(folder, filters, limit, offset)
    except ValueError as e:
        raise ToolError(str(e))

    return [_row_to_record(row, _short_id(row["uid"]), selected_fields) for row in rows]

@mcp.tool()
def get_body(folder: str, id: str, max_lines: int = 60, offset_lines: int = 0) -> dict[str, Any]:
    """Fetch the plain-text body of one message.

    id is the short id returned by search() for this folder (or a raw uid, as
    a fallback). max_lines/offset_lines page through long messages — if
    "truncated" comes back true, call again with a larger offset_lines to
    continue reading.
    """
    try:
        uid = _resolve_id(folder, id)
    except ValueError as e:
        raise ToolError(str(e))

    fpath = _find_body_file(folder, uid)
    if fpath:
        try:
            body, truncated, attachments = _extract_body(fpath, max_lines, offset_lines)
        except Exception as e:
            raise ToolError(f"Error reading message body: {e}")
        return {"source": "cache", "body": body, "truncated": truncated, "attachments": attachments}

    con = _connect()
    safe = folder.replace('"', '""')
    row = con.execute(f'SELECT preview FROM "{safe}" WHERE uid = ?', (uid,)).fetchone()
    if not row:
        try:
            row = con.execute(f'SELECT preview FROM "{safe}" WHERE uid = ?', (int(uid),)).fetchone()
        except ValueError:
            pass
    con.close()

    if row and row[0]:
        return {"source": "preview", "body": _strip_boilerplate(row[0]), "truncated": False, "attachments": []}
    return {"source": "unavailable", "body": None, "truncated": False, "attachments": []}

def main():
    mcp.run()

if __name__ == "__main__":
    main()
