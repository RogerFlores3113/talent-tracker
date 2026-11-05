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

from .store import BATCH_KINDS, NotFound, Store, now_iso

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
    """How a session is named on the page. The name comes from the AI app's own hello, which it picks itself
    (often a bare "mcp"), so it is shown as what the app calls itself, not as a fact."""
    said = " ".join(str(rec.get("client") or "").split())[:60]
    return f"Your AI app (calls itself {said})" if said else "Your AI app"


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


def _will_undo(a: dict[str, Any]) -> bool:
    """Whether Roll back would undo this listed change (a batch counts once, while any of its changes can be)."""
    if a["undone_at"] or a.get("confirmed_at"):
        return False
    return bool(a["undo_pending"] or (a["kind"] in BATCH_KINDS and a["undoable"]))


def later_moves(store: Store, acts: list[dict[str, Any]]) -> list[str]:
    """Stage moves the recruiter made by hand, after the session changed that person on that job, that still
    stand. Roll back leaves them as they are, so the confirm names them: "You moved Marcus Lee to Hired on
    Backend after it"."""
    first: dict[tuple[int, int], str] = {}
    for a in acts:
        if a["undone_at"] or a.get("confirmed_at") or not a.get("candidate_id") or not a.get("job_id"):
            continue
        key = (a["candidate_id"], a["job_id"])
        first[key] = min(first.get(key) or a["at"], a["at"])
    out = []
    for (cid, jid), at in sorted(first.items(), key=lambda kv: kv[1]):
        row = store._row("""SELECT t.body, t.data, c.name, j.title, j.company, p.stage FROM timeline t
                            JOIN candidates c ON c.id = t.candidate_id JOIN jobs j ON j.id = t.job_id
                            JOIN applications p ON p.candidate_id = t.candidate_id AND p.job_id = t.job_id
                            WHERE t.candidate_id = ? AND t.job_id = ? AND t.kind = 'stage' AND t.created_at >= ?
                            ORDER BY t.id DESC LIMIT 1""", (cid, jid, at))
        if row is None or row["body"] != row["stage"]:
            continue
        try:
            data = json.loads(row["data"] or "{}") or {}
        except ValueError:
            data = {}
        if data.get("by") == "ai" or data.get("undone_at"):
            continue
        job = row["title"] + (f" ({row['company']})" if row.get("company") else "")
        out.append(f"You moved {row['name']} to {row['stage']} on {job} after it")
    return out


def listing(store: Store, days: int = SHOWN_DAYS, limit: int = SHOWN) -> list[dict[str, Any]]:
    """Recent AI sessions that wrote something, newest first, each with its changes and what Roll back would do."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    recs = sorted((r for r in _all(store) if (r.get("first_write") or r.get("started") or "") >= since),
                  key=lambda r: r.get("first_write") or r.get("started") or "", reverse=True)[:limit]
    out = []
    for rec in recs:
        every = _actions(store, rec)
        acts = [a for a in every if not a.get("batch_id")]
        queued = waiting(store, rec["id"])
        if not acts and not queued and not rec.get("rolled_back"):
            continue
        g = {**rec, "label": label(rec), "items": acts, "undoable": sum(1 for a in acts if _will_undo(a)),
             "waiting": len(queued), "kept": sum(1 for a in acts if a.get("confirmed_at") and not a["undone_at"]),
             "undone": sum(1 for a in acts if a["undone_at"]),
             "later": later_moves(store, [a for a in every if _will_undo(a)])}
        g["blocked"] = len(acts) - g["undoable"] - g["kept"] - g["undone"]
        g["can_roll_back"] = bool(g["undoable"] or queued)
        g["confirm"] = confirm_text(g)
        out.append(g)
    return out


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def confirm_text(g: dict[str, Any]) -> str:
    """The question before Roll back, counted the way the session's row counts its changes."""
    n, undo = len(g["items"]), g["undoable"]
    text = "Roll back this AI session?"
    if n and undo == n:
        text += f" Its {_n(n, 'change')} {'is' if n == 1 else 'are'} undone, newest first."
    elif n:
        text += f" {undo} of its {_n(n, 'change')} {'is' if undo == 1 else 'are'} undone, newest first."
        rest = [x for x in (f"{g['kept']} you kept" if g["kept"] else "",
                            f"{g['undone']} already undone" if g["undone"] else "",
                            f"{g['blocked']} that can't be undone" if g.get("blocked") else "") if x]
        if rest:
            text += " Left as they are: " + ", ".join(rest) + "."
    if g["waiting"]:
        text += f" {_n(g['waiting'], 'change')} waiting on Review {'is' if g['waiting'] == 1 else 'are'} passed."
    later = g.get("later") or []
    if later:
        shown = "; ".join(later[:3]) + (f"; and {len(later) - 3} more" if len(later) > 3 else "")
        text += (f" {shown}. {'That move stays' if len(later) == 1 else 'Those moves stay'}, so check "
                 f"{'it' if len(later) == 1 else 'them'} after.")
    return text + " A backup is saved first."


def roll_back(store: Store, session: str) -> dict[str, Any]:
    """Undo everything one AI session wrote, newest first, and pass what it left waiting on Review. Changes the
    recruiter kept (on Review, or with Yes on Today) stay. A backup is taken first; if that fails, nothing changes.
    Counts go by the session's rows on Changes: a batch is one change. Returns {"undone": [...], "left": [...],
    "kept": n, "passed": n, "later": [...], "backup": file name}; "left" holds only what really couldn't be undone."""
    rec = get(store, session)
    if rec is None:
        raise NotFound("That AI session is gone. Reload the page.")
    from . import review
    from .importers import backup_before_import
    every = _actions(store, rec)
    later = later_moves(store, [a for a in every if _will_undo(a)])
    queued = waiting(store, session)
    if not queued and not any(_will_undo(a) for a in every):  # nothing to undo: no backup, and the first result stays
        old = rec.get("rolled_back") or {}
        return {"undone": [], "left": [], "kept": old.get("kept", 0), "passed": 0, "later": later, "backup": None}
    saved: Path = backup_before_import(store, reason=ROLLBACK_REASON)  # ValueError: nothing changed
    passed = review.decide_many(store, queued, False)["done"]
    kids: dict[int, list[dict[str, Any]]] = {}
    for a in every:
        if a.get("batch_id"):
            kids.setdefault(a["batch_id"], []).append(a)
    undone, left, kept = [], [], 0

    def still_there(a: dict[str, Any]) -> bool:
        try:
            return not store.ai_action(a["id"])["undone_at"]
        except NotFound:
            return False  # went with a person or job taken out just before

    def undo_one(a: dict[str, Any]) -> str | None:
        try:
            return store.undo_ai_action(a["id"], by="app")
        except NotFound:
            return None
        except ValueError as exc:
            if not still_there(a):
                return None  # undone along with something else, so nothing failed
            left.append(f"{a['summary']} ({str(exc).rstrip('.')})")
            return None

    for a in sorted((a for a in every if not a.get("batch_id")), key=lambda a: a["id"], reverse=True):
        if a["undone_at"] or not still_there(a):
            continue
        if a.get("confirmed_at"):
            kept += 1
            continue
        if a["kind"] == "save_batch":  # its saves are undone one by one, so one the recruiter kept stays
            did = 0
            for k in sorted(kids.get(a["id"], []), key=lambda k: k["id"], reverse=True):
                if k["undone_at"] or not still_there(k):
                    continue
                if k.get("confirmed_at"):
                    kept += 1
                    continue
                did += bool(undo_one(k))
            if did:
                undone.append(a["summary"])
            continue
        what = undo_one(a)
        if what:
            undone.append(what)
    result = {"at": now_iso(), "undone": len(undone), "left": left, "kept": kept, "passed": len(passed),
              "backup": saved.name, "later": later}
    rec = get(store, session) or rec
    rec["rolled_back"] = result
    with store._write():
        _put(store, rec)
    return {"undone": undone, "left": left, "kept": kept, "passed": len(passed), "later": later,
            "backup": saved.name}


def notice(res: dict[str, Any], listed: Any = None) -> str:
    """What a roll back did, in one message."""
    show = listed or (lambda xs: "; ".join(xs))
    undone, left, passed, kept = res["undone"], res["left"], res["passed"], res.get("kept", 0)
    if not undone and not left and not passed:
        return "Nothing from that AI session was left to roll back." + (
            f" Left {kept} you kept." if kept else "")
    bits = [f"Rolled back the AI session: undid {_n(len(undone), 'change')}" + (f" ({show(undone)})" if undone else "")]
    if passed:
        bits.append(f"passed {passed} that waited on Review")
    text = ", and ".join(bits) + "."
    if kept:
        text += f" Left {kept} you kept."
    if left:
        text += f" Couldn't undo {len(left)}: {show(left)}."
    later = res.get("later") or []
    if later:
        text += " Still as you left it: " + "; ".join(later[:5]) + "."
    return text + " A backup from just before is under Automatic backups in Settings."


__all__ = ["before_write", "confirm_text", "get", "label", "later_moves", "listing", "note_action", "notice", "roll_back", "waiting"]
