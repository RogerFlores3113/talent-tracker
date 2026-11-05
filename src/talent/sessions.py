"""The AI session ledger.

Each run of the MCP server (`talent mcp`) is one AI session, with its own id (review.new_session). At the session's
first write the app takes a backup of the whole database, and every AI action it logs (store._log_ai) is noted
here, so Changes and Review can group by session and one "Roll back this session" undoes exactly that run: its
writes newest first, its changes still waiting on Review passed, and a plain list of what it couldn't undo and why.
The AI app can't roll itself back: nothing here is an MCP tool.

A session's record is one settings row, "ai_session:<id>" (no migration): {"id", "started", "client",
"first_write", "backup", "actions": [ai_actions ids], "rolled_back": {...}}.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import NotFound, Store, now_iso

PREFIX = "ai_session:"
SHOWN_DAYS = 30  # sessions listed on Changes
SHOWN = 20
BACKUP_REASON = "ai-session"
ROLLBACK_REASON = "session-rollback"


def _key(session: str) -> str:
    return PREFIX + session


def get(store: Store, session: str) -> dict[str, Any] | None:
    row = store._row("SELECT value FROM settings WHERE key = ?", (_key(session),))
    if row is None:
        return None
    try:
        rec = json.loads(row["value"])
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _put(store: Store, rec: dict[str, Any]) -> None:
    """Save a record. Joins the caller's transaction (store._log_ai runs inside one)."""
    store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                        " SET value = excluded.value", (_key(rec["id"]), json.dumps(rec)))


def before_write(store: Store, session: str, started: str, client: str | None = None) -> dict[str, Any]:
    """Called before each write tool runs. The first time in a session, take its backup and start its record.
    A backup that fails doesn't stop the AI app; the record says so."""
    rec = get(store, session)
    if rec is not None and rec.get("first_write"):
        if client and not rec.get("client"):
            rec["client"] = client
            with store._write():
                _put(store, rec)
        return rec
    from .importers import backup_before_import  # here: importers imports this module's store
    backup, error = None, None
    try:
        backup = backup_before_import(store, reason=BACKUP_REASON).name
    except ValueError as exc:
        error = str(exc)
    rec = {**(rec or {}), "id": session, "started": (rec or {}).get("started") or started,
           "client": client or (rec or {}).get("client"), "first_write": now_iso(), "backup": backup,
           "backup_error": error, "actions": list((rec or {}).get("actions") or [])}
    with store._write():
        _put(store, rec)
    return rec


def note_action(store: Store, session: str, action_id: int) -> None:
    """Add one logged AI action to its session's record. Called by store._log_ai, inside its transaction."""
    rec = get(store, session) or {"id": session, "started": now_iso(), "client": None, "first_write": None,
                                  "backup": None, "actions": []}
    ids = rec.setdefault("actions", [])
    if action_id not in ids:
        ids.append(action_id)
    _put(store, rec)


def _all(store: Store) -> list[dict[str, Any]]:
    out = []
    for r in store._rows(f"SELECT value FROM settings WHERE key LIKE '{PREFIX}%'"):
        try:
            rec = json.loads(r["value"])
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


def label(rec: dict[str, Any]) -> str:
    """How a session is named on the page: its AI app, when it said."""
    return " ".join(str(rec.get("client") or "").split())[:60] or "Your AI app"


def _actions(store: Store, rec: dict[str, Any]) -> list[dict[str, Any]]:
    """The session's AI actions that still exist, newest first, as Store.ai_actions shapes them."""
    ids = {i for i in rec.get("actions") or [] if isinstance(i, int)}
    if not ids:
        return []
    return [a for a in store.ai_actions(rec.get("first_write") or rec.get("started"), limit=1_000_000)
            if a["id"] in ids]


def waiting(store: Store, session: str) -> list[int]:
    return [r["id"] for r in store._rows("SELECT id FROM ai_review WHERE status = 'pending' AND session = ?"
                                         " ORDER BY id", (session,))]


def listing(store: Store, days: int = SHOWN_DAYS, limit: int = SHOWN) -> list[dict[str, Any]]:
    """Recent AI sessions that wrote something, newest first, each with its changes and what Roll back would do."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    recs = sorted((r for r in _all(store) if (r.get("first_write") or r.get("started") or "") >= since),
                  key=lambda r: r.get("first_write") or r.get("started") or "", reverse=True)[:limit]
    out = []
    for rec in recs:
        acts = [a for a in _actions(store, rec) if not a.get("batch_id")]
        queued = waiting(store, rec["id"])
        if not acts and not queued and not rec.get("rolled_back"):
            continue
        pending = [a for a in acts if a["undo_pending"] and not a.get("confirmed_at")]
        out.append({**rec, "label": label(rec), "items": acts, "undoable": len(pending), "waiting": len(queued),
                    "kept": sum(1 for a in acts if a.get("confirmed_at") and not a["undone_at"]),
                    "can_roll_back": bool(pending or queued)})
    return out


def roll_back(store: Store, session: str) -> dict[str, Any]:
    """Undo everything one AI session wrote, newest first, and pass what it left waiting on Review. Changes the
    recruiter kept (on Review, or with Yes on Today) stay. A backup is taken first; if that fails, nothing changes.
    Returns {"undone": [...], "left": [...], "passed": n, "backup": file name}."""
    rec = get(store, session)
    if rec is None:
        raise NotFound("That AI session is gone. Reload the page.")
    from . import review
    from .importers import backup_before_import
    saved: Path = backup_before_import(store, reason=ROLLBACK_REASON)  # ValueError: nothing changed
    passed = review.decide_many(store, waiting(store, session), False)["done"]
    undone, left = [], []
    for a in sorted(_actions(store, rec), key=lambda a: a["id"], reverse=True):
        if a["undone_at"] or a["kind"] == "save_batch":  # a batch's saves are undone one by one
            continue
        if a.get("confirmed_at"):
            left.append(f"{a['summary']} (you kept it)")
            continue
        try:
            undone.append(store.undo_ai_action(a["id"], by="app"))
        except NotFound:
            continue  # went with a person or job taken out just before
        except ValueError as exc:
            left.append(f"{a['summary']} ({str(exc).rstrip('.')})")
    result = {"at": now_iso(), "undone": len(undone), "left": left, "passed": len(passed), "backup": saved.name}
    rec = get(store, session) or rec
    rec["rolled_back"] = result
    with store._write():
        _put(store, rec)
    return {"undone": undone, "left": left, "passed": len(passed), "backup": saved.name}


def notice(res: dict[str, Any], listed: Any = None) -> str:
    """What a roll back did, in one message."""
    show = listed or (lambda xs: "; ".join(xs))
    undone, left, passed = res["undone"], res["left"], res["passed"]
    if not undone and not left and not passed:
        return "Nothing from that AI session was left to roll back."
    bits = [f"Rolled back the AI session: undid {len(undone)} change{'' if len(undone) == 1 else 's'}"
            + (f" ({show(undone)})" if undone else "")]
    if passed:
        bits.append(f"passed {passed} that waited on Review")
    text = ", and ".join(bits) + "."
    if left:
        text += f" Couldn't undo {len(left)}: {show(left)}."
    return text + " A backup from just before is under Automatic backups in Settings."


__all__ = ["before_write", "get", "label", "listing", "note_action", "notice", "roll_back", "waiting"]
