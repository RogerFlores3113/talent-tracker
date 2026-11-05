"""The AI review queue, for Cautious mode.

With Cautious mode on (Settings), what the AI app would overwrite (a saved value replaced or cleared), a move
into a stage that counts as hired or out, a change to a job's saved details, status (a close included) or stages,
activity it logs (notes, calls, emails, messages, meetings, interviews, and notes sent with a move or an add), a
change to an interview (a cancel, a new time, an outcome) or its feedback, a change to who a client's updates go to
(or a client detail it replaces) and a Mark as sent wait here instead of landing. The recruiter keeps
(applies it, exactly once) or passes (drops it) each one at /review, one by one or a whole AI session at a time.
Filling an empty field and adding new people still apply straight away. An AI session is one run of the MCP
server (`talent mcp`): build_server makes its id.

Rows live in the ai_review table (store migration v10). Kept changes are applied as the AI app's own writes, so
they show on Changes like any other and can still be undone there.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import actions, reports
from . import sessions as ledger
from .store import (AI_KEPT, AI_SOURCE, FIELD_LABELS, INTERVIEW_OUTCOME_LABELS, INTERVIEW_OUTCOMES, INTERVIEW_VERDICTS,
                    JOB_FIELD_LABELS, JOB_STATUS_LABELS, NotFound, Store, _labels, now_iso, parse_when, quoted)

SETTING = "ai_cautious"
# Cautious options (settings rows, "1" for on). Only read while Cautious mode is on.
HOLD_CONTACTS = "ai_hold_contacts"  # a new email or phone on a saved person waits too, not only a replaced one
HOLD_MOVES = "ai_hold_moves"  # every stage move waits, not only moves into a hired or out stage
OPTIONS = (HOLD_CONTACTS, HOLD_MOVES)
# Job fields that say where client updates get emailed: in Cautious mode the AI app's change to one always waits,
# even when it fills an empty one.
JOB_CONTACT_FIELDS = ("hiring_manager_email",)
CANDIDATE_CONTACT_FIELDS = ("email", "phone")
QUEUE_CAP = 200  # changes that may wait at once; past this the AI app is told to stop and ask
KINDS = {"profile": "Profile change", "stage_move": "Stage move", "job_edit": "Job change", "note": "Logged activity",
         "client_edit": "Client change", "client_sent": "Update marked as sent", "interview": "Interview change",
         "feedback": "Interview feedback", "job_stages": "Stage change", "offer": "Offer change", "todo": "To-do done"}
CLIENT_LABELS = {"contacts": "contacts", "update_day": "update day", "time_zone": "time zone", "notes": "notes"}
WAIT_NOTE = ("Nothing changed yet: Cautious mode is on, so the recruiter keeps or passes it in the app "
             "(Review). Don't send it again.")


def cautious(store: Store) -> bool:
    return store.setting(SETTING) == "1"


def set_cautious(store: Store, on: bool) -> None:
    with store._write():  # its own settings row (not one of the Today settings set_settings checks)
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                            " SET value = excluded.value", (SETTING, "1" if on else "0"))


def set_options(store: Store, **on: bool) -> None:
    with store._write():
        store._conn.executemany("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                                " SET value = excluded.value", [(k, "1" if v else "0") for k, v in on.items()
                                                                if k in OPTIONS])


def hold_contacts(store: Store) -> bool:
    return cautious(store) and store.setting(HOLD_CONTACTS) == "1"


def hold_moves(store: Store) -> bool:
    return cautious(store) and store.setting(HOLD_MOVES) == "1"


def extra_holds_text(store: Store) -> str:
    """The extra holds that are on, named in the notice after saving Cautious mode ("" when none is)."""
    bits = [x for on, x in ((hold_contacts(store), "new emails and phones on saved people"),
                            (hold_moves(store), "every stage move, a submit to a client included")) if on]
    return f" So do {' and '.join(bits)}." if bits else ""


def held_contacts(store: Store, saved: dict[str, Any], data: dict[str, Any]) -> list[str]:
    """The email and phone the AI app would fill in on a saved person, when Settings holds new ones too."""
    if not hold_contacts(store):
        return []
    return [k for k in CANDIDATE_CONTACT_FIELDS
            if str(data.get(k) or "").strip() and not str(saved.get(k) or "").strip()]


def todos_this_hour(store: Store) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    return store._row("SELECT count(*) AS n FROM ai_actions WHERE kind = 'todo_added' AND at >= ?", (since,))["n"]


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


def _dedupe_key(payload: dict[str, Any]) -> str:
    """What makes two waiting changes the same: the payload, less what it marks as loose (a note's default time)."""
    loose = set(payload.get("_loose") or ())
    return json.dumps({k: v for k, v in payload.items() if k not in loose}, sort_keys=True, default=str)


def _queue(store: Store, kind: str, summary: str, payload: dict[str, Any], *, candidate_id: int | None = None,
           job_id: int | None = None, session: str | None = None) -> dict[str, Any]:
    if candidate_id is not None:
        store.get_candidate(candidate_id)  # a plain "not found", never a database error
    if job_id is not None:
        store.get_job(job_id)
    body = json.dumps(payload, sort_keys=True, default=str)
    key = _dedupe_key(json.loads(body))
    with store._write():
        for r in store._rows("SELECT id, payload FROM ai_review WHERE status = 'pending' AND kind = ?"
                             " AND coalesce(candidate_id, 0) = ? AND coalesce(job_id, 0) = ?",
                             (kind, candidate_id or 0, job_id or 0)):
            try:
                same = _dedupe_key(json.loads(r["payload"] or "{}")) == key
            except ValueError:
                same = False
            if same:  # sent twice: one row
                return get(store, r["id"])
        n = store._row("SELECT count(*) AS n FROM ai_review WHERE status = 'pending'")["n"]
        if n >= QUEUE_CAP:
            raise ValueError(f"No change made. {n} changes from your AI app already wait for the recruiter's review. "
                             "Ask them to review those in the app (Review) first.")
        try:
            rid = store._conn.execute(
                "INSERT INTO ai_review(kind, candidate_id, job_id, session, summary, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, candidate_id, job_id, session, summary, body, now_iso())).lastrowid
        except sqlite3.IntegrityError:
            raise NotFound("That person or job is gone, so nothing was saved.") from None
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


def queue_submit(store: Store, candidate_id: int, job_id: int, session: str | None) -> dict[str, Any] | None:
    """Hold submit_to_client's move into the job's Submitted stage (Cautious mode with Hold every stage move).
    Checked the way chase.submit checks first, so a submit that can't be made is refused now. None when they are
    already in that stage, so there is nothing to move."""
    from .chase import submit_stage  # here: chase imports reports, which this module imports
    job, c = store.get_job(job_id), store.get_candidate(candidate_id)
    app = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id))
    if app is None:
        raise ValueError(f"{c['name']} isn't on {job['title']} yet. Add them to the job first.")
    target = submit_stage(store, job_id)
    if target is None:
        raise ValueError(f"{job['title']} has no stage that counts as Submitted. Add one (like Submitted to client), "
                         "or set a stage's Counts as to Submitted on the job's Edit stages page.")
    here = next((x for x in store.job_stages(job_id) if x["name"].lower() == app["stage"].lower()), None)
    if here is not None and here["outcome"] != "open":
        raise ValueError(f"{c['name']} is in {app['stage']} on {job['title']}, which counts as "
                         f"{'hired' if here['outcome'] == 'hired' else 'out'}. Move them back first if they're in play.")
    if app["stage"].lower() == target["name"].lower():
        return None
    if here is not None and here["position"] > target["position"]:
        raise ValueError(f"{c['name']} is already past Submitted on {job['title']} ({app['stage']}).")
    where = job["title"] + (f" ({job['company']})" if job.get("company") else "")
    return _queue(store, "stage_move", f"Submit {quoted(c['name'])}: move from {app['stage']} to {target['name']} "
                  f"on {where}", {"stage": target["name"], "before": app["stage"], "notes": None, "submit": True},
                  candidate_id=candidate_id, job_id=job_id, session=session)


def mark_submit(store: Store, candidate_id: int, job_id: int) -> None:
    """Mark the AI app's newest move of this person on this job as a submit to the client, so it counts toward
    the hourly cap on what the AI app changes."""
    with store._write():
        a = store._row("SELECT id, new_data FROM ai_actions WHERE kind = 'stage_move' AND candidate_id = ? AND job_id = ?"
                       " ORDER BY id DESC LIMIT 1", (candidate_id, job_id))
        if a is None:
            return
        try:
            new = json.loads(a["new_data"] or "{}") or {}
        except ValueError:
            new = {}
        store._conn.execute("UPDATE ai_actions SET new_data = ? WHERE id = ?",
                            (json.dumps({**new, "submit": True}), a["id"]))


def stale_move(store: Store, r: dict[str, Any]) -> str | None:
    """Why a waiting stage move is out of date: a newer move of the same person on the same job waits too, or they
    were moved since. None while it still fits."""
    cid, jid = r.get("candidate_id"), r.get("job_id")
    if r["kind"] != "stage_move" or cid is None or jid is None:
        return None
    app = next((a for a in store.candidate_pipelines(cid) if a["job_id"] == jid), None)
    before = (r["payload"].get("before") or "")
    if app is None:
        return "They are no longer on this job."
    if app["stage"].lower() != before.lower():
        return f"They were moved to {app['stage']} since, so Keep won't make this move."
    if store._row("SELECT 1 FROM ai_review WHERE status = 'pending' AND kind = 'stage_move' AND candidate_id = ?"
                  " AND job_id = ? AND id > ?", (cid, jid, r["id"])):
        return "A newer move for them on this job waits here too. Keep the one you want."
    return None


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


def queue_note(store: Store, candidate_id: int, kind: str, body: str | None, at: str | None, job_id: int | None,
               session: str | None, interviewer: str | None = None) -> dict[str, Any]:
    """Hold an entry the AI app logs. at None means now; then the time doesn't make it a different note, so the
    same note sent again while it waits is one row."""
    c = store.get_candidate(candidate_id)
    payload: dict[str, Any] = {"kind": kind, "body": body, "at": at or now_iso(), "job_id": job_id}
    if at is None:
        payload["_loose"] = ["at"]
    if interviewer and interviewer.strip():
        payload["interviewer"] = interviewer.strip()
    return _queue(store, "note", f"Log {'an' if kind[0] in 'aeiou' else 'a'} {kind} on {quoted(c['name'])}",
                  payload, candidate_id=candidate_id, job_id=job_id, session=session)


def client_diff(store: Store, key: str, given: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(old, new) of the client details update_client would really change, cleaned the way the app saves them.
    Raises ValueError on a value the app would refuse."""
    before = reports.client(store, key)
    old, new = {}, {}
    for k, v in given.items():
        if k not in reports.CLIENT_AI_FIELDS:
            raise ValueError(f"Unknown client field {k}. It accepts: {', '.join(reports.CLIENT_AI_FIELDS)}.")
        v = (str(v) if v is not None else "").strip() or None
        limit = reports.FIELD_LIMITS.get(k)
        if limit and v and len(v) > limit:
            raise ValueError(f"The {k} are too long ({len(v):,} characters). The limit is {limit:,}.")
        if k == "update_day" and v:
            v = v.capitalize()
            if v not in reports.UPDATE_DAYS:
                raise ValueError("Pick a day of the week for updates.")
        if k == "time_zone":
            v = reports.normalize_tz(v)
        if before.get(k) != v:
            old[k], new[k] = before.get(k), v
    return old, new


def queue_client(store: Store, key: str, fields: dict[str, Any], old: dict[str, Any],
                 session: str | None) -> dict[str, Any]:
    """Hold the AI app's change to a client's details (who updates go to, above all)."""
    c = reports.client(store, key)
    summary = f"Change {quoted(c['name'])}'s " + ", ".join(CLIENT_LABELS.get(k, k) for k in fields)
    return _queue(store, "client_edit", summary, {"key": c["key"], "client": c["name"], "fields": fields, "old": old},
                  session=session)


def queue_sent(store: Store, key: str, manager: str | None, manager_name: str | None,
               session: str | None) -> dict[str, Any]:
    """Hold the AI app's Mark as sent of a client's (or one hiring manager's) update."""
    c = reports.client(store, key)
    who = f"{quoted(manager_name)}'s update" if manager_name else f"the update to {quoted(c['name'])}"
    return _queue(store, "client_sent", f"Mark {who} as sent",
                  {"key": c["key"], "client": c["name"], "manager": manager, "manager_name": manager_name,
                   "last": _last_sent_id(store, c["key"], manager)}, session=session)


def _last_sent_id(store: Store, key: str, manager: str | None) -> int | None:
    """The newest Mark as sent of this client's update (or this hiring manager's), so a kept one can tell it went
    out again since."""
    for u in reports.updates(store, key, 200):
        raw = reports._get_json(store, reports.UPDATE_KEY.format(u["id"]))
        if (raw.get("manager") if isinstance(raw, dict) else None) == manager:
            return u["id"]
    return None


def _interview_whose(iv: dict[str, Any]) -> str:
    name = actions.interview_name(iv)
    return f"{quoted(iv['candidate_name'])}'s interview" + (f" ({name})" if name else "")


def queue_interview(store: Store, interview_id: int, args: dict[str, Any], session: str | None) -> dict[str, Any]:
    """Hold the AI app's cancel, new time or outcome of an interview (args as update_interview takes them).
    Raises ValueError for what update_interview would refuse, so the AI app hears it now."""
    iv = store.get_interview(interview_id)
    whose, bits = _interview_whose(iv), []
    new_at = iv["at"]
    if args.get("at") and str(args["at"]).strip():
        new_at = parse_when(args["at"])
    if args.get("cancel"):
        bits.append(f"Cancel {whose}")
    elif new_at != iv["at"]:
        bits.append(f"Move {whose} to {actions.local_time(new_at)}")
    if args.get("outcome") is not None:
        key = " ".join(str(args["outcome"]).strip().lower().replace("_", " ").replace("-", " ").split()).strip(" .!")
        value = INTERVIEW_OUTCOMES.get(key) or INTERVIEW_VERDICTS.get(key)
        if value is None:
            raise ValueError("Outcome must be one of: passed, not passed, no show. Words like strong yes and "
                             "mixed count as passed, and strong no as not passed; the word is kept in the note.")
        if new_at > now_iso():
            raise ValueError("That interview hasn't happened yet, so it can't have an outcome. Record how it "
                             "went after it takes place, or change its time first.")
        bits.append(f"Record {whose} as {INTERVIEW_OUTCOME_LABELS.get(value, value)}")
    if args.get("interviewer") is not None:
        bits.append(f"Set the interviewer on {whose}" if str(args["interviewer"]).strip()
                    else f"Clear the interviewer on {whose}")
    if not bits:
        bits.append(f"Change {whose}")
    summary = bits[0] + "".join(f", and {b[0].lower()}{b[1:]}" for b in bits[1:])
    return _queue(store, "interview", summary, {"interview_id": interview_id, "args": args,
                                                "before": {"at": iv["at"], "data": iv["data"]}},
                  candidate_id=iv["candidate_id"], job_id=iv.get("job_id"), session=session)


def queue_feedback(store: Store, interview_id: int, feedback: str, interviewer: str | None,
                   session: str | None, verdict: str | None = None) -> dict[str, Any]:
    iv = store.get_interview(interview_id)
    who = f" from {quoted(interviewer.strip())}" if interviewer and interviewer.strip() else ""
    said = f" (verdict: {verdict})" if verdict else ""
    return _queue(store, "feedback", f"Save feedback{who}{said} on {_interview_whose(iv)}",
                  {"interview_id": interview_id, "feedback": feedback, "interviewer": interviewer,
                   **({"verdict": verdict} if verdict else {})},
                  candidate_id=iv["candidate_id"], job_id=iv.get("job_id"), session=session)


def _stage_shape(store: Store, job_id: int) -> list[list[Any]]:
    return [[s["id"], s["name"], s["outcome"]] for s in store.job_stages(job_id)]


def dry_run(store: Store, fn: Any) -> Any:
    """Run fn() (store writes made with _write) and take every write back: to check a change before it waits."""
    with store._lock:
        store._depth += 1
        store._conn.execute("SAVEPOINT review_dry_run")
        try:
            return fn()
        finally:
            store._conn.execute("ROLLBACK TO review_dry_run")
            store._conn.execute("RELEASE review_dry_run")
            store._depth -= 1


def queue_stages(store: Store, job_id: int, args: dict[str, Any], session: str | None) -> dict[str, Any]:
    """Hold the AI app's edit of a job's stages. It is checked first, so a change that can't be made is refused now."""
    j = store.get_job(job_id)
    res = dry_run(store, lambda: store.edit_job_stages(job_id, by="ai", **args))
    job = j["title"] + (f" ({j['company']})" if j.get("company") else "")
    if not res.get("changes"):
        raise ValueError("No change to the stages of " + job + ".")
    return _queue(store, "job_stages", f"Change the stages of {job}: {res['changes']}",
                  {"args": args, "before": _stage_shape(store, job_id)}, job_id=job_id, session=session)


def queue_todo(store: Store, row: dict[str, Any], session: str | None) -> dict[str, Any]:
    """Hold the AI app's Mark done of one of the recruiter's own to-dos."""
    return _queue(store, "todo", f"Mark the to-do {quoted(row.get('text'))} done",
                  {"suggestion_id": row["id"], "text": row.get("text")}, candidate_id=row.get("candidate_id"),
                  job_id=row.get("job_id"), session=session)


def email_changes(store: Store, job_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Hiring manager emails the AI app set (a new one or a replaced one) that the recruiter hasn't kept or undone,
    while the job still has that email: {"id", "job_id", "title", "email", "old", "at"}, newest first. Client
    updates can go to these addresses, so the client page and Today point them out."""
    where, params = "", []
    if job_ids is not None:
        if not job_ids:
            return []
        where = f" AND a.job_id IN ({', '.join('?' * len(job_ids))})"
        params = list(job_ids)
    out, seen = [], set()
    for a in store._rows(
            f"""SELECT a.id, a.kind, a.job_id, a.old_data, a.new_data, a.at, a.undone_at, a.confirmed_at,
                       j.title, j.company, j.hiring_manager_email AS now
                FROM ai_actions a JOIN jobs j ON j.id = a.job_id
                WHERE a.kind IN ('job_edit', 'job_created'){where} ORDER BY a.id DESC""", params):
        if a["job_id"] in seen:
            continue
        try:
            new, old = json.loads(a["new_data"] or "{}") or {}, json.loads(a["old_data"] or "{}") or {}
        except ValueError:
            continue
        if a["kind"] == "job_created":
            new, old = (new.get("job") or {}), {}
        if not new.get("hiring_manager_email"):
            continue
        seen.add(a["job_id"])
        if a["undone_at"] or a["confirmed_at"] or a["now"] != new["hiring_manager_email"]:
            continue
        out.append({"id": a["id"], "job_id": a["job_id"], "title": a["title"], "company": a["company"],
                    "email": new["hiring_manager_email"], "old": old.get("hiring_manager_email"), "at": a["at"],
                    "confirmable": a["kind"] == "job_edit"})
    return out


def keep_email(store: Store, action_id: int) -> None:
    """The recruiter checked an email the AI app set on a job (even one it set when it made the job)."""
    a = store._row("SELECT kind, undone_at FROM ai_actions WHERE id = ?", (action_id,))
    if a is None or a["kind"] not in ("job_edit", "job_created"):
        raise NotFound("That change is gone. Reload the page.")
    if a["undone_at"]:
        raise ValueError("That change was undone.")
    with store._write():
        store._conn.execute("UPDATE ai_actions SET confirmed_at = coalesce(confirmed_at, ?) WHERE id = ?",
                            (now_iso(), action_id))


def contacts_change(store: Store, key: str | None) -> dict[str, Any] | None:
    """The AI app's latest change to this client's contacts, while the recruiter hasn't said Keep or Undo and the
    contacts are still what the AI app set: {"id", "old", "new", "at"}, for "Contacts changed by your AI app"."""
    if not key:
        return None
    row = reports.get_row(store, key)
    if not row:
        return None
    for a in store._rows("SELECT id, old_data, new_data, at, undone_at, confirmed_at FROM ai_actions"
                         " WHERE kind = 'client_edit' AND target_id = ? ORDER BY id DESC", (row["id"],)):
        try:
            new, old = json.loads(a["new_data"] or "{}") or {}, json.loads(a["old_data"] or "{}") or {}
        except ValueError:
            continue
        if "contacts" not in new:
            continue
        if a["undone_at"] or a["confirmed_at"] or new.get("_key") != key or row.get("contacts") != new["contacts"]:
            return None
        return {"id": a["id"], "old": old.get("contacts"), "new": new["contacts"], "at": a["at"]}
    return None


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
        if p.get("submit"):
            mark_submit(store, cid, jid)
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
        if _kept_before(store, r):  # the same note, sent again after the first was kept: logged once
            return r["summary"] + " (it was already logged, so it wasn't logged twice)"
        entry = store.log(cid, p["kind"], p.get("body"), at=p.get("at"), job_id=p.get("job_id"), source=AI_SOURCE,
                          interviewer=p.get("interviewer"))
        store.mark_ai_note(entry)
        return r["summary"]
    return _apply_more(store, r)


def _kept_before(store: Store, r: dict[str, Any]) -> bool:
    """Whether a waiting change just like this one was already kept (a note at its default time, sent twice)."""
    if not (r["payload"].get("_loose")):
        return False
    key = _dedupe_key(r["payload"])
    for o in store._rows("SELECT payload FROM ai_review WHERE status = 'kept' AND kind = ? AND id <> ?"
                         " AND coalesce(candidate_id, 0) = ? AND coalesce(job_id, 0) = ?"
                         " AND coalesce(result, '') NOT LIKE 'Couldn''t%'",
                         (r["kind"], r["id"], r.get("candidate_id") or 0, r.get("job_id") or 0)):
        try:
            if _dedupe_key(json.loads(o["payload"] or "{}")) == key:
                return True
        except ValueError:
            continue
    return False


def _confirm_action(store: Store, action_id: int | None) -> None:
    if action_id:
        store.confirm_ai_action(action_id)


def _apply_more(store: Store, r: dict[str, Any]) -> str:
    """Kept client, interview, feedback and stage changes (see _apply)."""
    p, kind = r["payload"], r["kind"]
    if kind == "client_edit":
        try:
            now = reports.client(store, p["key"])
        except NotFound:
            raise ValueError("That client is gone.") from None
        old = p.get("old") or {}
        fields = {k: v for k, v in (p.get("fields") or {}).items() if now.get(k) == old.get(k)}
        if not fields:
            raise ValueError("Those details were changed since, so nothing was changed.")
        out = reports.ai_save_client(store, p["key"], **fields)
        _confirm_action(store, out.get("action_id"))  # kept on Review: the recruiter has seen it
        skipped = [CLIENT_LABELS.get(k, k) for k in (p.get("fields") or {}) if k not in fields]
        return r["summary"] + (f" (left the {', '.join(skipped)} as changed since)" if skipped else "")
    if kind == "client_sent":
        if reports.get_row(store, p["key"]) is None and p.get("last") is not None:
            raise ValueError("That client is gone.")
        if _last_sent_id(store, p["key"], p.get("manager")) != p.get("last"):
            raise ValueError("That update was marked as sent since, so it wasn't marked again.")
        reports.ai_mark_sent(store, p["key"], manager=p.get("manager"))
        return r["summary"]
    if kind == "interview":
        try:
            iv = store.get_interview(p["interview_id"])
        except NotFound:
            raise ValueError("That interview is gone.") from None
        before = p.get("before") or {}
        if iv["at"] != before.get("at") or iv["data"] != (before.get("data") or {}):
            raise ValueError("That interview was changed since, so nothing was changed.")
        store.update_interview(p["interview_id"], by="ai", **(p.get("args") or {}))
        return r["summary"]
    if kind == "feedback":
        try:
            if p.get("verdict"):  # one interviewer's own verdict, with their words (interviews.give_verdict)
                from .interviews import give_verdict
                give_verdict(store, p["interview_id"], p["verdict"], p.get("interviewer"), p["feedback"], by="ai")
            else:
                store.add_interview_feedback(p["interview_id"], p["feedback"], p.get("interviewer"), by="ai")
        except NotFound:
            raise ValueError("That interview is gone.") from None
        return r["summary"]
    if kind == "job_stages":
        jid = r.get("job_id")
        if jid is None:
            raise ValueError("That job is gone.")
        if _stage_shape(store, jid) != p.get("before"):
            raise ValueError("The job's stages were changed since, so nothing was changed.")
        store.edit_job_stages(jid, by="ai", **(p.get("args") or {}))
        return r["summary"]
    if kind == "offer":
        from .offers import apply_review  # here: offers imports this module
        return apply_review(store, r)
    if kind == "todo":
        s = store._row("SELECT done_at FROM suggestions WHERE id = ?", (p.get("suggestion_id"),))
        if s is None:
            raise ValueError("That to-do is gone.")
        if s["done_at"]:
            raise ValueError("That to-do is already done.")
        store.complete_suggestion(p["suggestion_id"], by="ai")
        return r["summary"]
    raise ValueError("That change can't be made here.")


def keep(store: Store, review_id: int) -> str:
    """Apply one waiting change, exactly once (a second Keep, or a Keep after Pass, is refused)."""
    r = _claim(store, review_id, "kept")
    token = AI_KEPT.set(True)  # what it writes is the recruiter's choice: Undo all AI leaves it unless asked
    try:
        what = _apply(store, r)
    except (ValueError, NotFound) as exc:
        reason = str(exc).rstrip(".")
        _result(store, review_id, f"Couldn't keep it: {reason}", "passed")  # decided: it won't wait again
        raise ValueError(f"Couldn't keep it: {reason}.") from None
    finally:
        AI_KEPT.reset(token)
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
        r["lines"] = field_lines(r) if r["kind"] in ("profile", "job_edit") else _more_lines(r)
        r["detail"] = _detail(r)
        r["stale"] = stale_move(store, r)
        g["items"].append(r)
    for g in groups.values():
        rec = ledger.get(store, g["session"]) if g["session"] else None
        g["label"] = ledger.label(rec) if rec else "Your AI app"
        g["started"] = (rec or {}).get("started") or g["started"]
        g["ledger"] = rec is not None  # it has a ledger, so Roll back this session can run
    return sorted(groups.values(), key=lambda g: g["started"], reverse=True)


def _more_lines(r: dict[str, Any]) -> list[dict[str, str]]:
    """What a client or interview change would do, one line per field: {"label", "old", "new"}."""
    p = r["payload"]
    if r["kind"] == "offer":
        from .offers import review_lines  # here: offers imports this module
        return review_lines(r)
    if r["kind"] == "client_edit":
        return [{"label": CLIENT_LABELS.get(k, k), "old": _short((p.get("old") or {}).get(k)) or "(empty)",
                 "new": _short(v) or "(cleared)"} for k, v in (p.get("fields") or {}).items()]
    if r["kind"] == "interview":
        args, before = p.get("args") or {}, p.get("before") or {}
        out = []
        if args.get("at") and str(args["at"]).strip() and not args.get("cancel"):
            try:
                out.append({"label": "time", "old": actions.local_time(before.get("at")),
                            "new": actions.local_time(parse_when(args["at"]))})
            except ValueError:
                pass
        if args.get("interviewer") is not None:
            out.append({"label": "interviewer", "old": _short((before.get("data") or {}).get("interviewer")) or "(empty)",
                        "new": _short(args["interviewer"]) or "(cleared)"})
        return out
    return []


def _detail(r: dict[str, Any]) -> str:
    """Text the AI app sent with a waiting change (feedback, an outcome note or a reason), shown as its words."""
    p = r["payload"]
    if r["kind"] == "feedback":
        return p.get("feedback") or ""
    if r["kind"] == "interview":
        args = p.get("args") or {}
        return " ".join(str(args[k]) for k in ("note", "reason") if args.get(k))
    return ""


def decided(store: Store, since: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
    """Kept and passed changes, newest first, for Changes."""
    return [_out(r) for r in store._rows(
        """SELECT r.*, c.name AS candidate_name, j.title AS job_title, j.company AS job_company
           FROM ai_review r LEFT JOIN candidates c ON c.id = r.candidate_id LEFT JOIN jobs j ON j.id = r.job_id
           WHERE r.status <> 'pending' AND r.decided_at >= ? ORDER BY r.decided_at DESC, r.id DESC LIMIT ?""",
        (since or "", limit))]


def pass_since(store: Store, since: str) -> int:
    """Pass every change that waits on Review and was sent at or after this time (Undo all AI does this)."""
    ids = [r["id"] for r in store._rows("SELECT id FROM ai_review WHERE status = 'pending' AND created_at >= ?",
                                        (since,))]
    return len(decide_many(store, ids, False)["done"])


def today_items(store: Store) -> list[dict[str, Any]]:
    """One Today item while AI changes wait for review, and one for each job email the AI app set unchecked."""
    out = []
    for e in email_changes(store)[:5]:
        job = e["title"] + (f" ({e['company']})" if e.get("company") else "")
        out.append({"key": f"aiemail:{e['id']}", "kind": "ai_change", "rank": 0, "when": e["at"],
                    "title": f"Your AI app set where updates for {job} go: {e['email']}.",
                    "detail": "Client updates can be emailed there. Check it on the job.",
                    "href": f"/jobs/{e['job_id']}", "button": "Check"})
    row = store._row("SELECT count(*) AS n, max(id) AS last, max(created_at) AS at FROM ai_review WHERE status = 'pending'")
    if not row or not row["n"]:
        return out
    n = row["n"]
    return out + [{"key": f"aireview:{row['last']}", "kind": "ai_change", "rank": 0, "when": row["at"],
                   "title": f"{n} change{'' if n == 1 else 's'} from your AI app wait{'s' if n == 1 else ''} for your review.",
                   "detail": "Keep or pass each one. Nothing changed until you do.", "href": "/review",
                   "button": "Review"}]


__all__ = ["extra_holds_text", "mark_submit", "queue_submit", "stale_move", "HOLD_CONTACTS", "HOLD_MOVES", "JOB_CONTACT_FIELDS", "email_changes", "hold_contacts", "hold_moves",
           "held_contacts", "keep_email", "pass_since", "queue_todo", "set_options", "todos_this_hour",
           "KINDS", "SETTING", "cautious", "client_diff", "contacts_change", "count", "decide_many", "decided",
           "dry_run", "field_lines", "get", "keep", "new_session", "pass_", "pending", "queue_client", "queue_feedback",
           "queue_interview", "queue_job", "queue_move", "queue_note", "queue_profile", "queue_sent", "queue_stages",
           "sessions", "set_cautious", "today_items", "ai_lines", "WAIT_NOTE"]
