"""The AI review queue, for Cautious mode.

With Cautious mode on (Settings), what the AI app would overwrite (a saved value replaced or cleared), a move
into a stage that counts as hired or out, a change to a job's saved details or status (a close included) and
activity it logs (notes, calls, emails, messages, meetings) wait here instead of landing. The recruiter keeps
(applies it, exactly once) or passes (drops it) each one at /review, one by one or a whole AI session at a time.
Filling an empty field and adding new people still apply straight away. An AI session is one run of the MCP
server (`talent mcp`): build_server makes its id.

Rows live in the ai_review table (store migration v10). Kept changes are applied as the AI app's own writes, so
they show on Changes like any other and can still be undone there.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from .store import AI_SOURCE, FIELD_LABELS, JOB_FIELD_LABELS, JOB_STATUS_LABELS, NotFound, Store, _labels, now_iso, quoted

SETTING = "ai_cautious"
QUEUE_CAP = 200  # changes that may wait at once; past this the AI app is told to stop and ask
KINDS = {"profile": "Profile change", "stage_move": "Stage move", "job_edit": "Job change", "note": "Logged activity"}
WAIT_NOTE = ("Nothing changed yet: Cautious mode is on, so the recruiter keeps or passes it in the app "
             "(Review). Don't send it again.")


def cautious(store: Store) -> bool:
    return store.setting(SETTING) == "1"


def set_cautious(store: Store, on: bool) -> None:
    with store._write():  # its own settings row (not one of the Today settings set_settings checks)
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                            " SET value = excluded.value", (SETTING, "1" if on else "0"))


def new_session() -> str:
    """An id for one AI session (one run of the MCP server)."""
    return secrets.token_hex(6)


def _out(r: dict[str, Any]) -> dict[str, Any]:
    try:
        r["payload"] = json.loads(r["payload"] or "{}")
    except ValueError:
        r["payload"] = {}
    return r


def get(store: Store, review_id: int) -> dict[str, Any]:
    r = store._row("""SELECT r.*, c.name AS candidate_name, j.title AS job_title, j.company AS job_company
                      FROM ai_review r LEFT JOIN candidates c ON c.id = r.candidate_id
                      LEFT JOIN jobs j ON j.id = r.job_id WHERE r.id = ?""", (review_id,))
    if r is None:
        raise NotFound("That change is gone. Reload the page.")
    return _out(r)


def pending(store: Store, candidate_id: int | None = None, job_id: int | None = None,
            session: str | None = None) -> list[dict[str, Any]]:
    """Changes that wait for review, oldest first."""
    where, params = ["r.status = 'pending'"], []
    for col, v in (("r.candidate_id", candidate_id), ("r.job_id", job_id), ("r.session", session)):
        if v is not None:
            where.append(f"{col} = ?")
            params.append(v)
    return [_out(r) for r in store._rows(
        f"""SELECT r.*, c.name AS candidate_name, j.title AS job_title, j.company AS job_company
            FROM ai_review r LEFT JOIN candidates c ON c.id = r.candidate_id LEFT JOIN jobs j ON j.id = r.job_id
            WHERE {' AND '.join(where)} ORDER BY r.id""", params)]


def count(store: Store) -> int:
    return store._row("SELECT count(*) AS n FROM ai_review WHERE status = 'pending'")["n"]


def _job(r: dict[str, Any]) -> str:
    title = r.get("job_title") or "a job"
    return f"{title} ({r['job_company']})" if r.get("job_company") else title


def _queue(store: Store, kind: str, summary: str, payload: dict[str, Any], *, candidate_id: int | None = None,
           job_id: int | None = None, session: str | None = None) -> dict[str, Any]:
    body = json.dumps(payload, sort_keys=True, default=str)
    with store._write():
        same = store._row("SELECT id FROM ai_review WHERE status = 'pending' AND kind = ? AND payload = ?"
                          " AND coalesce(candidate_id, 0) = ? AND coalesce(job_id, 0) = ?",
                          (kind, body, candidate_id or 0, job_id or 0))
        if same:  # sent twice: one row
            return get(store, same["id"])
        n = store._row("SELECT count(*) AS n FROM ai_review WHERE status = 'pending'")["n"]
        if n >= QUEUE_CAP:
            raise ValueError(f"No change made. {n} changes from your AI app already wait for the recruiter's review. "
                             "Ask them to review those in the app (Review) first.")
        rid = store._conn.execute(
            "INSERT INTO ai_review(kind, candidate_id, job_id, session, summary, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, candidate_id, job_id, session, summary, body, now_iso())).lastrowid
    return get(store, rid)


def _short(v: Any, width: int = 80) -> str:
    if isinstance(v, list):
        v = "; ".join(" at ".join(x for x in (e.get("title"), e.get("company")) if x) for e in v if isinstance(e, dict))
    text = " ".join(str(v or "").split())
    return (text[:width] + "...") if len(text) > width else text


def field_lines(r: dict[str, Any]) -> list[dict[str, str]]:
    """What a profile or job change would do, one line per field: {"label", "old", "new"}."""
    p = r["payload"]
    labels = JOB_FIELD_LABELS if r["kind"] == "job_edit" else FIELD_LABELS
    out = []
    for k, v in (p.get("fields") or {}).items():
        old = (p.get("old") or {}).get(k)
        if k == "status":
            old, v = JOB_STATUS_LABELS.get(old, old), JOB_STATUS_LABELS.get(v, v)
        out.append({"label": labels.get(k, k.replace("_", " ")), "old": _short(old) or "(empty)",
                    "new": _short(v) or "(cleared)"})
    if "work_history" in p:
        out.append({"label": "work history", "old": _short((p.get("old") or {}).get("work_history")) or "(empty)",
                    "new": _short(p["work_history"]) or "(cleared)"})
    return out


def queue_profile(store: Store, candidate_id: int, fields: dict[str, Any], work_history: list[dict[str, Any]] | None,
                  keys: list[str], session: str | None) -> dict[str, Any]:
    """Hold the AI app's overwrites of these saved fields (and work history) on one person."""
    row = store._row("SELECT * FROM candidates WHERE id = ?", (candidate_id,)) or {}
    prepared = store._prepare({k: v for k, v in fields.items() if k in keys and k != "work_history"})
    payload: dict[str, Any] = {"fields": prepared, "old": {k: row.get(k) for k in prepared}}
    if "work_history" in keys and work_history is not None:
        payload["work_history"] = work_history
        payload["old"]["work_history"] = store.work_history(candidate_id)
    labels = _labels(list(prepared) + (["work_history"] if "work_history" in payload else []))
    return _queue(store, "profile", f"Change {quoted(row.get('name'))}'s {labels}", payload,
                  candidate_id=candidate_id, session=session)


def queue_move(store: Store, candidate_id: int, job_id: int, stage: str, before: str, notes: str | None,
               session: str | None) -> dict[str, Any]:
    c, j = store.get_candidate(candidate_id), store.get_job(job_id)
    job = j["title"] + (f" ({j['company']})" if j.get("company") else "")
    return _queue(store, "stage_move", f"Move {quoted(c['name'])} from {before} to {stage} on {job}",
                  {"stage": stage, "before": before, "notes": notes}, candidate_id=candidate_id, job_id=job_id,
                  session=session)


def queue_job(store: Store, job_id: int, fields: dict[str, Any], old: dict[str, Any],
              session: str | None) -> dict[str, Any]:
    j = store.get_job(job_id)
    job = j["title"] + (f" ({j['company']})" if j.get("company") else "")
    if list(fields) == ["status"]:
        what = {"closed": "Close", "on_hold": "Put on hold", "open": "Reopen"}.get(fields["status"], "Change")
        summary = f"{what} {job}"
    else:
        summary = f"Change {job}: " + ", ".join(JOB_FIELD_LABELS.get(k, k.replace("_", " ")) for k in fields)
    return _queue(store, "job_edit", summary, {"fields": fields, "old": old}, job_id=job_id, session=session)


def queue_note(store: Store, candidate_id: int, kind: str, body: str | None, at: str, job_id: int | None,
               session: str | None) -> dict[str, Any]:
    c = store.get_candidate(candidate_id)
    return _queue(store, "note", f"Log {'an' if kind[0] in 'aeiou' else 'a'} {kind} on {quoted(c['name'])}",
                  {"kind": kind, "body": body, "at": at, "job_id": job_id}, candidate_id=candidate_id,
                  job_id=job_id, session=session)


def ai_lines(store: Store, candidate_id: int | None = None, job_id: int | None = None) -> list[str]:
    """What waits for review, as the AI app reads it back: "Change "Ana Ruiz"'s title (to 'Staff Engineer')"."""
    out = []
    for r in pending(store, candidate_id=candidate_id, job_id=job_id):
        bits = [f"{x['label']} to {x['new']!r}" for x in field_lines(r)] if r["kind"] in ("profile", "job_edit") else []
        if r["kind"] == "note" and r["payload"].get("body"):
            bits.append(_short(r["payload"]["body"], 60))
        out.append(r["summary"] + (f" ({'; '.join(bits)})" if bits else "") + f" (review id {r['id']})")
    return out


# ------------------------------------------------------------------ decide
def _claim(store: Store, review_id: int, status: str) -> dict[str, Any]:
    """Mark a waiting change kept or passed, once. Raises ValueError when it was already decided."""
    r = get(store, review_id)
    with store._write():
        cur = store._conn.execute("UPDATE ai_review SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                                  (status, now_iso(), review_id))
    if cur.rowcount != 1:
        raise ValueError("That change was already kept or passed.")
    return r


def _result(store: Store, review_id: int, text: str, status: str | None = None) -> None:
    with store._write():
        store._conn.execute("UPDATE ai_review SET result = ?, status = coalesce(?, status) WHERE id = ?",
                            (text, status, review_id))


def _confirm_newest(store: Store, kind: str, candidate_id: int | None, job_id: int) -> None:
    """A kept move or job close was the recruiter's choice, so Today doesn't ask "Right?" about it again."""
    a = store._row("SELECT max(id) AS id FROM ai_actions WHERE kind = ? AND job_id = ? AND (? IS NULL OR candidate_id = ?)",
                   (kind, job_id, candidate_id, candidate_id))
    if a and a["id"]:
        store.confirm_ai_action(a["id"])


def _apply(store: Store, r: dict[str, Any]) -> str:
    """Make one kept change and say what happened. Raises ValueError when it can't be made any more."""
    p, kind = r["payload"], r["kind"]
    cid, jid = r.get("candidate_id"), r.get("job_id")
    if kind == "profile":
        if cid is None:
            raise ValueError("That person is gone.")
        row = store._row("SELECT * FROM candidates WHERE id = ?", (cid,)) or {}
        old = p.get("old") or {}
        fields = {k: v for k, v in (p.get("fields") or {}).items() if row.get(k) == old.get(k)}
        history = p.get("work_history") if "work_history" in p and store.work_history(cid) == old.get("work_history") else None
        skipped = [k for k in (p.get("fields") or {}) if k not in fields]
        if "work_history" in p and history is None:
            skipped.append("work_history")
        if not fields and history is None:
            raise ValueError("Those details were changed since, so nothing was changed.")
        store.update_candidate(cid, work_history=history, by="ai", **fields)
        return r["summary"] + (f" (left the {_labels(skipped)} as it was changed since)" if skipped else "")
    if kind == "stage_move":
        app = next((a for a in store.candidate_pipelines(cid) if a["job_id"] == jid), None) if cid else None
        if app is None:
            raise ValueError("They are no longer in that job.")
        if app["stage"].lower() != (p.get("before") or "").lower():
            raise ValueError(f"They were moved to {app['stage']} since, so the move wasn't made.")
        store.move_stage(cid, jid, p["stage"], p.get("notes"), by="ai")
        store.mark_ai_stage(cid, jid, p["before"])
        _confirm_newest(store, "stage_move", cid, jid)
        return r["summary"]
    if kind == "job_edit":
        if jid is None:
            raise ValueError("That job is gone.")
        job, old = store.get_job(jid), p.get("old") or {}
        fields = {k: v for k, v in (p.get("fields") or {}).items() if job.get(k) == old.get(k)}
        if not fields:
            raise ValueError("Those details were changed since, so nothing was changed.")
        store.update_job(jid, by="ai", **fields)
        if "status" in fields:
            _confirm_newest(store, "job_edit", None, jid)
        skipped = [k for k in (p.get("fields") or {}) if k not in fields]
        return r["summary"] + (" (left " + ", ".join(k.replace("_", " ") for k in skipped) + " as it was changed since)"
                               if skipped else "")
    if kind == "note":
        if cid is None:
            raise ValueError("That person is gone.")
        entry = store.log(cid, p["kind"], p.get("body"), at=p.get("at"), job_id=p.get("job_id"), source=AI_SOURCE)
        store.mark_ai_note(entry)
        return r["summary"]
    raise ValueError("That change can't be made here.")


def keep(store: Store, review_id: int) -> str:
    """Apply one waiting change, exactly once (a second Keep, or a Keep after Pass, is refused)."""
    r = _claim(store, review_id, "kept")
    try:
        what = _apply(store, r)
    except (ValueError, NotFound) as exc:
        reason = str(exc).rstrip(".")
        _result(store, review_id, f"Couldn't keep it: {reason}", "passed")  # decided: it won't wait again
        raise ValueError(f"Couldn't keep it: {reason}.") from None
    _result(store, review_id, what)
    return what


def pass_(store: Store, review_id: int) -> str:
    r = _claim(store, review_id, "passed")
    _result(store, review_id, "Passed")
    return r["summary"]


def decide_many(store: Store, ids: list[int], keep_them: bool) -> dict[str, list[str]]:
    done, left = [], []
    for rid in ids:
        try:
            done.append(keep(store, rid) if keep_them else pass_(store, rid))
        except NotFound:
            continue
        except ValueError as exc:
            left.append(str(exc).rstrip("."))
    return {"done": done, "left": left}


def sessions(store: Store) -> list[dict[str, Any]]:
    """Waiting changes grouped by AI session, newest session first: {"session", "started", "items"}."""
    groups: dict[str, dict[str, Any]] = {}
    for r in pending(store):
        key = r.get("session") or ""
        g = groups.setdefault(key, {"session": key, "started": r["created_at"], "items": []})
        r["lines"] = field_lines(r) if r["kind"] in ("profile", "job_edit") else []
        g["items"].append(r)
    return sorted(groups.values(), key=lambda g: g["started"], reverse=True)


def decided(store: Store, since: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
    """Kept and passed changes, newest first, for Changes."""
    return [_out(r) for r in store._rows(
        """SELECT r.*, c.name AS candidate_name, j.title AS job_title, j.company AS job_company
           FROM ai_review r LEFT JOIN candidates c ON c.id = r.candidate_id LEFT JOIN jobs j ON j.id = r.job_id
           WHERE r.status <> 'pending' AND r.decided_at >= ? ORDER BY r.decided_at DESC, r.id DESC LIMIT ?""",
        (since or "", limit))]


def today_items(store: Store) -> list[dict[str, Any]]:
    """One Today item while AI changes wait for review."""
    row = store._row("SELECT count(*) AS n, max(id) AS last, max(created_at) AS at FROM ai_review WHERE status = 'pending'")
    if not row or not row["n"]:
        return []
    n = row["n"]
    return [{"key": f"aireview:{row['last']}", "kind": "ai_change", "rank": 0, "when": row["at"],
             "title": f"{n} change{'' if n == 1 else 's'} from your AI app wait{'s' if n == 1 else ''} for your review.",
             "detail": "Keep or pass each one. Nothing changed until you do.", "href": "/review", "button": "Review"}]


__all__ = ["KINDS", "SETTING", "cautious", "count", "decide_many", "decided", "field_lines", "get", "keep",
           "new_session", "pass_", "pending", "queue_job", "queue_move", "queue_note", "queue_profile", "sessions",
           "set_cautious", "today_items", "ai_lines", "WAIT_NOTE"]
