"""The automatic backups in the data folder's backups/: list them, read what's in one, put one back.

App window only. The MCP server has no way to restore (or delete) anything.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .importers import backup_before_import, backup_label_path
from .paths import backups_dir
from .store import Store, now_iso

# backup_before_import names files "before-<reason>-YYYY-mm-dd-HHMMSS[-n].db".
NAME_RE = re.compile(r"^before-(?P<reason>[a-z0-9]+(?:-[a-z0-9]+)*?)-(?P<stamp>\d{4}-\d{2}-\d{2}-\d{6})(?:-\d+)?\.db$")
REASONS = {"import": "Before an import", "delete": "Before a delete", "merge": "Before a merge",
           "sample-removal": "Before removing sample data", "restore": "Before a restore",
           "undo-ai": "Before undoing AI changes", "ai-session": "At the start of an AI session",
           "session-rollback": "Before rolling back an AI session", "keep-all": "Before Keep all on Review"}
UNDO_KEY = "last_import_undo"  # settings row: which backup undoes the newest import, and the data right after it
LISTED = 60


def _reason(name: str) -> str:
    m = NAME_RE.match(name)
    reason = m["reason"] if m else ""
    return REASONS.get(reason) or ("Before " + reason.replace("-", " ") if reason else "Backup")


def _when(path: Path) -> datetime:
    m = NAME_RE.match(path.name)
    if m:
        try:
            return datetime.strptime(m["stamp"], "%Y-%m-%d-%H%M%S").astimezone()
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def read_counts(path: Path) -> dict[str, Any]:
    """People and jobs in a backup file, read without changing it. ok is False for a file we can't read."""
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?immutable=1", uri=True)
    except sqlite3.Error:
        return {"ok": False}
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"candidates", "jobs", "settings"} <= tables:
            return {"ok": False}
        return {"ok": True, "people": conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
                "jobs": conn.execute("SELECT count(*) FROM jobs").fetchone()[0]}
    except sqlite3.Error:
        return {"ok": False}
    finally:
        conn.close()


def _label(path: Path) -> str | None:
    """'Before importing Connections.csv (4,000 rows)', from the label saved next to an import's backup."""
    try:
        rec = json.loads(backup_label_path(path).read_text(encoding="utf-8"))
        rows = int(rec.get("rows") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    file = str(rec.get("file") or "").strip()[:120]
    counted = f"{rows:,} row{'' if rows == 1 else 's'}"
    return f"Before importing {file} ({counted})" if file else f"Before an import ({counted})"


def describe(path: Path) -> dict[str, Any]:
    when = _when(path)
    return {"name": path.name, "path": path, "why": _label(path) or _reason(path.name), "when": when.isoformat(),
            **read_counts(path)}


def list_backups(limit: int = LISTED) -> list[dict[str, Any]]:
    """The automatic backups, newest first, each with when, why and how many people and jobs it holds."""
    files = [f for f in backups_dir().iterdir() if f.is_file() and NAME_RE.match(f.name)]
    files.sort(key=lambda f: (_when(f), f.name), reverse=True)
    return [describe(f) for f in files[:limit]]


def backup_file(name: str) -> Path | None:
    """The backup with that file name, only if it's one of ours, straight inside the backups folder."""
    if not NAME_RE.match(name or ""):
        return None
    folder = backups_dir()
    path = folder / name
    return path if path.is_file() and path.resolve().parent == folder.resolve() else None


def restore(store: Store, name: str) -> dict[str, Any]:
    """Back up the data as it is now, then put the named backup in its place.

    Returns {"restored": describe(backup), "saved": Path of the copy taken first}."""
    path = backup_file(name)
    if path is None:
        raise ValueError("That backup isn't in the backups folder any more. Reload the page and pick another.")
    info = describe(path)
    if not info["ok"]:
        raise ValueError("That backup file can't be read, so nothing was changed. Pick another one.")
    saved = backup_before_import(store, reason="restore")
    mine = _ai_options(store)  # how the recruiter wants the AI app held now, not as it was in the old backup
    try:
        store.restore_from(path)
    except sqlite3.Error as exc:
        raise ValueError(f"Couldn't restore that backup, so your data wasn't changed ({exc}).") from exc
    _put_ai_options(store, mine)
    return {"restored": info, "saved": saved}


# Settings rows that say how far the AI app is held: kept as they are now when an older backup comes back.
AI_OPTION_KEYS = ("ai_cautious", "ai_hold_contacts", "ai_hold_moves", "ai_new_apps_trust")


def _ai_options(store: Store) -> dict[str, Any]:
    keys = {k: r["value"] for k in AI_OPTION_KEYS if (r := store._row("SELECT value FROM settings WHERE key = ?", (k,)))}
    apps = store._rows("SELECT name, first_seen, last_seen, trust FROM ai_apps")
    return {"keys": keys, "apps": apps}


def _put_ai_options(store: Store, mine: dict[str, Any]) -> None:
    with store._write():
        for k, v in mine["keys"].items():
            store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                                " value = excluded.value", (k, v))
        for a in mine["apps"]:  # the AI apps and the trust set for them
            store._conn.execute(
                "INSERT INTO ai_apps(name, first_seen, last_seen, trust) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE"
                " SET trust = excluded.trust", (a["name"], a["first_seen"], a["last_seen"], a["trust"]))


# ------------------------------------------------------------ undo an import
def content_digest(store: Store) -> str:
    """A fingerprint of everything the recruiter or the AI can change (not settings), in any row order.
    Settings, the search outbox and the full-text index (virtual tables and their shadow tables) don't count."""
    total, mod = 0, 1 << 128
    with store._lock:
        rows = store._conn.execute(
            "SELECT name, coalesce(sql, '') FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
        virtual = [n for n, sql in rows if sql.upper().startswith("CREATE VIRTUAL TABLE")]
        skip = {"settings", "search_outbox", *virtual}
        tables = sorted(n for n, _ in rows if n not in skip and not any(n.startswith(v + "_") for v in virtual))
        for table in tables:
            for row in store._conn.execute(f'SELECT * FROM "{table}"'):
                h = hashlib.sha256(repr((table, tuple(row))).encode()).digest()
                total = (total + int.from_bytes(h[:16], "big")) % mod
    return f"{total:032x}"


def remember_import(store: Store, backup: str | Path) -> None:
    """After an import: note its backup and what the data looks like now, so Undo can tell if it changed since."""
    value = json.dumps({"backup": Path(backup).name, "digest": content_digest(store)})
    with store._lock, store._conn:
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (UNDO_KEY, value))


def import_undo_state(store: Store, name: str) -> str:
    """'ok' when Undo can put back the backup taken before this import, 'changed' when something else changed
    since, 'gone' when that backup or its record is no longer here."""
    row = store._row("SELECT value FROM settings WHERE key = ?", (UNDO_KEY,))
    try:
        rec = json.loads(row["value"]) if row else {}
    except ValueError:
        rec = {}
    if not rec or rec.get("backup") != name or backup_file(name) is None:
        return "gone"
    return "ok" if rec.get("digest") == content_digest(store) else "changed"


# ------------------------------------------------------- imports on Changes
IMPORT_LOG_KEY = "import_log"  # settings row: the newest imports, each one line on Changes
IMPORT_LOG_KEPT = 50


def record_import(store: Store, result: dict[str, Any], file_name: str | None, total: int) -> dict[str, Any]:
    """After an import (app or command line): one entry on Changes, and Undo for it while nothing else changed."""
    backup = Path(result["backup"]).name if result.get("backup") else None
    if backup:
        remember_import(store, result["backup"])
    entry = {"at": now_iso(), "file": (file_name or "").strip()[:120], "rows": total, "added": result.get("added", 0),
             "updated": result.get("updated", 0), "unchanged": result.get("unchanged", 0),
             "skipped": result.get("skipped_rows", 0), "pipelines": result.get("pipelines_added", 0),
             "notes": result.get("notes", 0), "moved": len(result.get("moved") or []), "changed": bool(backup),
             "backup": backup, "started": result.get("run")}  # started: so Changes tucks its per-person rows under it
    log = [entry] + import_log(store)
    value = json.dumps(log[:IMPORT_LOG_KEPT])
    with store._lock, store._conn:
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (IMPORT_LOG_KEY, value))
    return entry


def import_log(store: Store) -> list[dict[str, Any]]:
    """The imports recorded here, newest first."""
    row = store._row("SELECT value FROM settings WHERE key = ?", (IMPORT_LOG_KEY,))
    try:
        log = json.loads(row["value"]) if row else []
    except ValueError:
        return []
    return [e for e in log if isinstance(e, dict) and e.get("at")] if isinstance(log, list) else []


def import_text(entry: dict[str, Any]) -> str:
    """'Imported Connections.csv: 4,000 rows, 3,950 new, 50 updated'."""
    rows = int(entry.get("rows") or 0)
    parts = [f"{rows:,} row{'' if rows == 1 else 's'}"]
    for key, word in (("added", "new"), ("updated", "updated"), ("skipped", "skipped")):
        if entry.get(key):
            parts.append(f"{int(entry[key]):,} {word}")
    if entry.get("pipelines"):
        parts.append(f"{int(entry['pipelines']):,} put in job pipelines")
    if entry.get("moved"):
        parts.append(f"{int(entry['moved']):,} stage move{'' if entry['moved'] == 1 else 's'}")
    if entry.get("notes"):
        parts.append(f"{int(entry['notes']):,} note{'' if entry['notes'] == 1 else 's'} added")
    if not entry.get("changed"):
        parts.append("nothing changed")
    return f"Imported {entry.get('file') or 'a CSV file'}: " + ", ".join(parts)
