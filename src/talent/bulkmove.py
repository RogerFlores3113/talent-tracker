"""Move several people in one job to another stage in one action, with one undo.

The job table's "Move N ticked to [stage]" bar uses this. Each person's move is an ordinary stage entry on their
timeline (so their own history is right); the batch is remembered as one setting so Changes and the status line
can offer a single Undo. The AI has no tool for this."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .store import NotFound, Store

PREFIX = "bulk_move:"
MAX_PEOPLE = 5000


def _put(store: Store, key: str, value: str) -> None:
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))


def _people(n: int) -> str:
    return f"{n} {'person' if n == 1 else 'people'}"


def move_many(store: Store, job_id: int, candidate_ids: list[int], stage: str, by: str = "app") -> dict[str, Any]:
    """Move these people (who are in the job) to the stage. Returns moved, same (already there), missing (not in the
    job), the stage name and the batch id (None when nobody moved). Raises ValueError for an unknown stage."""
    job = store.get_job(job_id)
    target = store.resolve_stage(job_id, stage)
    moves: list[dict[str, Any]] = []
    same = missing = 0
    for cid in list(dict.fromkeys(candidate_ids))[:MAX_PEOPLE]:
        row = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?", (cid, job_id))
        if row is None:
            missing += 1
            continue
        if row["stage"] == target["name"]:
            same += 1
            continue
        store.move_stage(cid, job_id, target["name"], by=by)
        entry = store._row("SELECT id FROM timeline WHERE candidate_id = ? AND job_id = ? AND kind = 'stage'"
                           " ORDER BY id DESC LIMIT 1", (cid, job_id))
        moves.append({"candidate_id": cid, "from": row["stage"], "timeline_id": entry["id"] if entry else None})
    batch_id = None
    if moves:
        rows = store._rows(f"SELECT key FROM settings WHERE key LIKE '{PREFIX}%'")
        batch_id = 1 + max((int(r["key"][len(PREFIX):]) for r in rows if r["key"][len(PREFIX):].isdigit()), default=0)
        _put(store, f"{PREFIX}{batch_id}", json.dumps({
            "job_id": job_id, "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "stage": target["name"], "moves": moves}))
    return {"moved": len(moves), "same": same, "missing": missing, "stage": target["name"], "batch_id": batch_id,
            "job_title": job["title"]}


def summary(res: dict[str, Any]) -> str:
    """The status line: 'Moved 12 people to Screening. 2 were already there.'"""
    if not res["moved"]:
        text = f"Nobody moved. {_people(res['same'])} {'was' if res['same'] == 1 else 'were'} already in {res['stage']}." \
            if res["same"] else "Nobody moved."
    else:
        text = f"Moved {_people(res['moved'])} to {res['stage']}."
        if res["same"]:
            text += f" {res['same']} {'was' if res['same'] == 1 else 'were'} already there."
    if res["missing"]:
        text += f" {res['missing']} {'was' if res['missing'] == 1 else 'were'} not in this job and left alone."
    return text


def batch(store: Store, batch_id: int) -> dict[str, Any] | None:
    raw = store.setting(f"{PREFIX}{batch_id}")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def batches(store: Store, since: str | None = None) -> list[dict[str, Any]]:
    """Bulk moves, newest first, each with a Changes line and whether it can still be undone."""
    out = []
    for r in store._rows(f"SELECT key, value FROM settings WHERE key LIKE '{PREFIX}%'"):
        bid = r["key"][len(PREFIX):]
        try:
            b = json.loads(r["value"])
        except ValueError:
            continue
        if not bid.isdigit() or not isinstance(b, dict) or (since and (b.get("at") or "") < since):
            continue
        job = store._row("SELECT title, company FROM jobs WHERE id = ?", (b.get("job_id"),))
        if not job:
            continue
        n = len(b.get("moves") or [])
        out.append({**b, "id": int(bid), "job_title": job["title"],
                    "text": f"Moved {_people(n)} to {b.get('stage')} in {job['title']}"
                            + (f" ({job['company']})" if job["company"] else ""),
                    "undoable": not b.get("undone_at")})
    return sorted(out, key=lambda b: (b["at"], b["id"]), reverse=True)


def undo(store: Store, batch_id: int, by: str = "app") -> str:
    """Put everyone back in the stage they were in, unless they were moved again since. Returns the notice.
    Raises NotFound or ValueError (already undone)."""
    key = f"{PREFIX}{batch_id}"
    b = batch(store, batch_id)
    if not b:
        raise NotFound("That move is gone. Reload the page.")
    if b.get("undone_at"):
        raise ValueError("That move was already undone.")
    back = stayed = 0
    for m in b.get("moves") or []:
        try:
            if not m.get("timeline_id"):
                raise ValueError("no entry")
            store.undo_change(m["timeline_id"], by=by)
            back += 1
        except (ValueError, NotFound):
            stayed += 1
    b["undone_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    _put(store, key, json.dumps(b))
    notice = f"Undid the move. {_people(back)} back in the stage they were in."
    if stayed:
        notice += f" {stayed} {'person was' if stayed == 1 else 'people were'} moved again since, so they stay put."
    return notice
