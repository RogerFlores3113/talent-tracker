"""Outreach: logging a first message moves someone on, follow-up sequences per job, and other clients' claims.

- Logging a call, email or message for someone still in a job's first stage moves them to the job's Contacted
  stage (settings outreach_moves, on unless "0"). A person in exactly one job in play gets that job.
- A job can have a short follow-up sequence (first message, then follow-ups N days after it). It lives in
  settings, and each person's place in it is worked out from their timeline: their first outreach on that job
  starts it, each later day with outreach is the next step, and a reply (or a move past Contacted) ends it.
  Nothing is sent by the app: follow-ups come due on Today and in list_followups with paste-ready text.
- other_clients: the clients someone is already submitted to, so adding them to another client's job warns.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .actions import company_key, day_text, job_label, late_like, submit_like
from .store import NOT_STRUCK, Store, as_local, now_iso, quoted, to_dt

MOVES_KEY = "outreach_moves"  # "0": logging outreach leaves people where they are
SEQ_PREFIX = "outreach_sequence:"  # settings: one job's follow-up steps, {"steps": [...], "since": iso}
OUTREACH_KINDS = ("call", "email", "message")
REPLY_KIND = "reply"  # log_activity kind: logged as a message marked as their reply
MAX_STEPS, LABEL_LIMIT, TEXT_LIMIT, DAY_LIMIT = 5, 60, 2000, 90
GROUP_AT = 4  # this many follow-ups due on one job fold into one Today row
DEFAULT_STEPS = (
    {"label": "First message", "day": 0,
     "text": "Hi {first_name}, I'm hiring for a {job} role at {company} and your background stood out. "
             "Open to a quick chat this week?"},
    {"label": "Follow-up", "day": 4,
     "text": "Hi {first_name}, following up on my note about the {job} role at {company}. "
             "Happy to share more if it's of interest."},
    {"label": "Last follow-up", "day": 7,
     "text": "Hi {first_name}, one last note on the {job} role at {company}. If now isn't the time, no worries, "
             "and I'd be glad to stay in touch."},
)
PLACEHOLDERS = ("{first_name}", "{name}", "{job}", "{company}")


# ------------------------------------------------------------------ moves
def moves_on(store: Store) -> bool:
    row = store._row("SELECT value FROM settings WHERE key = ?", (MOVES_KEY,))
    return not row or row["value"] != "0"


def _at(stages: list[dict[str, Any]], name: str | None) -> int | None:
    return next((i for i, s in enumerate(stages) if s["name"].lower() == (name or "").lower()), None)


def contact_stage(store: Store, job_id: int) -> dict[str, Any] | None:
    """Where outreach moves someone from the job's first stage: a stage named like Contacted or Outreach,
    else the second stage, while it is still in play."""
    stages = store.job_stages(job_id)
    if len(stages) < 2 or stages[0]["outcome"] != "open":
        return None
    later = [s for s in stages[1:] if s["outcome"] == "open"]
    return next((s for s in later if "contact" in s["name"].lower() or "outreach" in s["name"].lower()),
                stages[1] if stages[1]["outcome"] == "open" else None)


def job_for(store: Store, candidate_id: int) -> int | None:
    """The one job someone is in play on (an open job, a stage still in play), or None."""
    ids = []
    for p in store.candidate_pipelines(candidate_id):
        job = store._row("SELECT status FROM jobs WHERE id = ?", (p["job_id"],))
        stages = store.job_stages(p["job_id"])
        i = _at(stages, p["stage"])
        if job and job["status"] == "open" and (i is None or stages[i]["outcome"] == "open"):
            ids.append(p["job_id"])
    return ids[0] if len(ids) == 1 else None


def advance(store: Store, candidate_id: int, job_id: int, kind: str, at: str | None,
            by: str = "app") -> dict[str, Any] | None:
    """After outreach is logged: move them from the job's first stage to its Contacted stage.
    Returns {"from", "to", "action_id"} (action_id: the AI's stage move, for Changes), or None."""
    if kind not in OUTREACH_KINDS or not moves_on(store) or (at and at > now_iso()):
        return None
    app = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id))
    stages = store.job_stages(job_id)
    if not app or not stages or _at(stages, app["stage"]) != 0:
        return None
    target = contact_stage(store, job_id)
    if target is None:
        return None
    store.move_stage(candidate_id, job_id, target["name"], by=by)
    act = store._row("SELECT id FROM ai_actions WHERE candidate_id = ? AND job_id = ? AND kind = 'stage_move'"
                     " ORDER BY id DESC LIMIT 1", (candidate_id, job_id)) if by == "ai" else None
    return {"from": app["stage"], "to": target["name"], "action_id": act["id"] if act else None}


def logged_today(store: Store, candidate_id: int, now: datetime | None = None) -> bool:
    """Whether a call, email or message was already logged for them today (on the recruiter's clock)."""
    today = as_local(now or datetime.now(timezone.utc)).date()
    since = (now or datetime.now(timezone.utc)) - timedelta(days=2)
    rows = store._rows(f"SELECT t.at FROM timeline t WHERE t.candidate_id = ? AND t.kind IN ('call', 'email', 'message')"
                       f" AND t.at >= ? AND {NOT_STRUCK}", (candidate_id, since.replace(microsecond=0).isoformat()))
    return any(as_local(to_dt(r["at"])).date() == today for r in rows)


def mark_reply(store: Store, entry_id: int) -> None:
    """Mark a logged message as their reply (it ends a follow-up sequence)."""
    with store._write():
        row = store._row("SELECT data FROM timeline WHERE id = ?", (entry_id,))
        data = _json(row["data"]) if row else {}
        if not data.get("reply"):
            store._conn.execute("UPDATE timeline SET data = ? WHERE id = ?",
                                (json.dumps({**data, "reply": True}), entry_id))


def _json(raw: Any) -> dict[str, Any]:
    try:
        v = json.loads(raw) if isinstance(raw, str) and raw else raw
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


# --------------------------------------------------------------- sequences
def clean_steps(raw: Any) -> list[dict[str, Any]]:
    """Steps as saved: up to 5, each with a label, a day (days after the first message, rising) and the text."""
    if raw in (None, "", []):
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("Send the steps as a list, each with a label, a day and the text.")
    steps: list[dict[str, Any]] = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise ValueError("Each step needs a label, a day and the text.")
        label = " ".join(str(s.get("label") or "").split())[:LABEL_LIMIT]
        text = str(s.get("text") or "").strip()[:TEXT_LIMIT]
        day_raw = str(s.get("day") if s.get("day") is not None else "").strip()
        if not label and not text and not day_raw:
            continue
        if not day_raw and not steps:
            day_raw = "0"
        if not re.fullmatch(r"\d{1,2}", day_raw):
            raise ValueError(f"Step {i + 1}: the day is a whole number of days after the first message.")
        day = int(day_raw)
        if day > DAY_LIMIT:
            raise ValueError(f"Step {i + 1}: keep it within {DAY_LIMIT} days of the first message.")
        if not steps and day != 0:
            raise ValueError("The first step is the first message, on day 0.")
        if steps and day <= steps[-1]["day"]:
            raise ValueError(f"Step {i + 1}: each follow-up comes on a later day than the one before.")
        steps.append({"label": label or ("First message" if not steps else f"Follow-up {len(steps)}"),
                      "day": day, "text": text})
    if len(steps) > MAX_STEPS:
        raise ValueError(f"A sequence has at most {MAX_STEPS} steps.")
    if len(steps) == 1:
        raise ValueError("Add at least one follow-up after the first message.")
    return steps


def sequence(store: Store, job_id: int) -> dict[str, Any] | None:
    """{"steps": [...], "since": iso} for a job with a follow-up sequence, else None."""
    raw = store._row("SELECT value FROM settings WHERE key = ?", (f"{SEQ_PREFIX}{job_id}",))
    seq = _json(raw["value"]) if raw else {}
    return seq if seq.get("steps") else None


def save_sequence(store: Store, job_id: int, steps: Any) -> list[dict[str, Any]]:
    """Set (or with no steps, remove) a job's follow-up sequence. People first contacted from now on follow it."""
    store.get_job(job_id)
    clean = clean_steps(steps)
    key = f"{SEQ_PREFIX}{job_id}"
    old = sequence(store, job_id)
    with store._write():
        if not clean:
            store._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            value = json.dumps({"steps": clean, "since": (old or {}).get("since") or now_iso()})
            store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                                " value = excluded.value", (key, value))
    return clean


def steps_from_form(form: Any) -> list[dict[str, Any]]:
    return [{"label": form.get(f"label_{i}"), "day": form.get(f"day_{i}"), "text": form.get(f"text_{i}")}
            for i in range(MAX_STEPS)]


def draft(step: dict[str, Any], person: dict[str, Any], job: dict[str, Any]) -> str:
    """A step's text with the person's first name and the job filled in, ready to paste."""
    name = " ".join(str(person.get("name") or "").split())
    first = name.split()[0] if name else "there"
    text = step.get("text") or ""
    for k, v in (("{first_name}", first), ("{name}", name), ("{job}", job.get("title") or "this"),
                 ("{company}", job.get("company") or "our team")):
        text = text.replace(k, v)
    return text


def _local_day(iso: str) -> date:
    return as_local(to_dt(iso)).date()


def progress(store: Store, job_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Where each person on this job is in its sequence: people with outreach logged on the job since the sequence
    was set. status: due (the next step is due today or earlier), waiting, replied, moved_on or finished."""
    seq = sequence(store, job_id)
    if not seq:
        return []
    now = now or datetime.now(timezone.utc)
    today = as_local(now).date()
    job, steps = store.get_job(job_id), seq["steps"]
    stages = store.job_stages(job_id)
    contact = contact_stage(store, job_id)
    reach = _at(stages, contact["name"]) if contact else 0
    since_day = _local_day(seq["since"])
    rows = store._rows(
        f"""SELECT t.candidate_id, t.kind, t.at, t.data FROM timeline t
            WHERE t.job_id = ? AND t.kind IN ('call', 'email', 'message') AND t.at <= ? AND {NOT_STRUCK}
            ORDER BY t.at""", (job_id, now.replace(microsecond=0).isoformat()))
    touches: dict[int, list[date]] = {}
    replied: dict[int, date] = {}
    for r in rows:
        day = _local_day(r["at"])
        if day < since_day:
            continue
        if _json(r["data"]).get("reply"):
            replied.setdefault(r["candidate_id"], day)
        else:
            days = touches.setdefault(r["candidate_id"], [])
            if day not in days:
                days.append(day)
    apps = {a["candidate_id"]: a for a in store.pipeline(job_id)}
    out = []
    for cid, days in touches.items():
        a = apps.get(cid)
        if a is None:
            continue
        first, n = days[0], len(days)
        i = _at(stages, a["stage"])
        person = {"candidate_id": cid, "name": a["candidate_name"], "stage": a["stage"], "sent": n,
                  "steps": len(steps), "first_on": first.isoformat(), "last_on": days[-1].isoformat()}
        if cid in replied and replied[cid] >= first:
            out.append({**person, "status": "replied", "replied_on": replied[cid].isoformat()})
        elif i is not None and (stages[i]["outcome"] != "open" or i > (reach or 0)):
            out.append({**person, "status": "moved_on"})
        elif n >= len(steps):
            out.append({**person, "status": "finished"})
        else:
            step = steps[n]
            due = first + timedelta(days=step["day"])
            if due <= days[-1]:  # sent the last one late: the next comes a day after it at the earliest
                due = days[-1] + timedelta(days=1)
            out.append({**person, "status": "due" if due <= today else "waiting", "step": n + 1,
                        "label": step["label"], "due_on": due.isoformat(), "overdue": due < today,
                        "draft": draft(step, {"name": a["candidate_name"]}, job)})
    order = {"due": 0, "waiting": 1, "finished": 2, "replied": 3, "moved_on": 4}
    out.sort(key=lambda p: (order[p["status"]], p.get("due_on") or "", p["name"]))
    return out


def due(store: Store, job_id: int | None = None, now: datetime | None = None,
        days_ahead: int = 0) -> list[dict[str, Any]]:
    """Follow-ups due today or earlier (or within days_ahead), on open jobs, each with its job and draft."""
    now = now or datetime.now(timezone.utc)
    limit = (as_local(now).date() + timedelta(days=days_ahead)).isoformat()
    ids = [job_id] if job_id is not None else [
        int(r["key"][len(SEQ_PREFIX):]) for r in store._rows(f"SELECT key FROM settings WHERE key LIKE '{SEQ_PREFIX}%'")
        if r["key"][len(SEQ_PREFIX):].isdigit()]
    out = []
    for jid in ids:
        job = store._row("SELECT id, title, company, status FROM jobs WHERE id = ?", (jid,))
        if not job or (job_id is None and job["status"] != "open"):
            continue
        for p in progress(store, jid, now):
            if p["status"] in ("due", "waiting") and p["due_on"] <= limit:
                out.append({**p, "job_id": jid, "job": job_label(job["title"], job.get("company"))})
    out.sort(key=lambda p: (p["due_on"], p["job"], p["name"]))
    return out


def due_text(p: dict[str, Any], now: datetime | None = None) -> str:
    today = as_local(now or datetime.now(timezone.utc)).date()
    d = date.fromisoformat(p["due_on"])
    if d == today:
        return "due today"
    if d < today:
        n = (today - d).days
        return f"{n} day{'' if n == 1 else 's'} overdue"
    return "due " + day_text(d)


def today_items(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """Today rows: each follow-up due, or one row per job when several are due there."""
    items: list[dict[str, Any]] = []
    by_job: dict[int, list[dict[str, Any]]] = {}
    for p in due(store, now=now):
        by_job.setdefault(p["job_id"], []).append(p)
    for jid, ps in by_job.items():
        if len(ps) >= GROUP_AT:
            names = ", ".join(p["name"] for p in ps[:3]) + (f" and {len(ps) - 3} more" if len(ps) > 3 else "")
            items.append({"key": f"followups:{jid}:{ps[0]['due_on']}", "kind": "followup", "rank": 1,
                          "when": ps[0]["due_on"] + "T00:00:00+00:00", "job_id": jid,
                          "title": f"{len(ps)} follow-ups due for {ps[0]['job']}", "detail": names + ".",
                          "href": f"/jobs/{jid}#followups"})
            continue
        for p in ps:
            items.append({"key": f"followup:{p['candidate_id']}:{jid}:{p['step']}", "kind": "followup", "rank": 1,
                          "when": p["due_on"] + "T00:00:00+00:00", "candidate_id": p["candidate_id"], "job_id": jid,
                          "title": f"{p['label']} to {p['name']}",
                          "detail": f"{p['job']} · step {p['step']} of {p['steps']} · {due_text(p, now)}",
                          "draft": p["draft"], "overdue": p["overdue"], "href": f"/jobs/{jid}#followups"})
    return items


def summary_line(p: dict[str, Any], now: datetime | None = None) -> str:
    """One plain line for the AI app: who, which step, when."""
    if p["status"] in ("due", "waiting"):
        return (f"{quoted(p['name'])}: {p['label']} (step {p['step']} of {p['steps']}), {due_text(p, now)}")
    word = {"replied": "replied", "moved_on": f"moved on to {p['stage']}",
            "finished": f"no reply after all {p['steps']} steps"}[p["status"]]
    return f"{quoted(p['name'])}: {word}"


# ---------------------------------------------------------- other clients
def other_clients(store: Store, candidate_id: int, job_id: int) -> list[dict[str, Any]]:
    """Other clients' open jobs where this person is already submitted (a Submitted or Presented stage, or one
    after it, or an interview or offer stage) and still in play."""
    job = store._row("SELECT company FROM jobs WHERE id = ?", (job_id,))
    here = company_key(job["company"]) if job and (job.get("company") or "").strip() else None
    out = []
    for p in store.candidate_pipelines(candidate_id):
        if p["job_id"] == job_id or not (p.get("job_company") or "").strip():
            continue
        if company_key(p["job_company"]) == here:
            continue
        status = store._row("SELECT status FROM jobs WHERE id = ?", (p["job_id"],))
        if not status or status["status"] != "open":
            continue
        stages = store.job_stages(p["job_id"])
        i = _at(stages, p["stage"])
        if i is None or stages[i]["outcome"] != "open":
            continue
        first_submit = next((k for k, s in enumerate(stages) if submit_like(s["name"])), None)
        if not ((first_submit is not None and i >= first_submit) or late_like(p["stage"])):
            continue
        out.append({"client": p["job_company"].strip(), "job_id": p["job_id"], "job": p["job_title"],
                    "stage": p["stage"], "since": p.get("stage_changed_at")})
    return out


def other_clients_text(store: Store, candidate_id: int, job_id: int) -> str | None:
    """'Heads up: "Lena Hoffmann" is in Submitted to client at Harbor Freight Labs (since Nov 5). ...' or None."""
    found = other_clients(store, candidate_id, job_id)
    if not found:
        return None
    name = store.get_candidate(candidate_id)["name"]
    bits = []
    for f in found[:3]:
        since = to_dt(f["since"]) if f.get("since") else None
        bits.append(f"in {f['stage']} at {f['client']}" + (f" (since {day_text(as_local(since), '%b %-d')})"
                                                            if since else ""))
    more = f" and {len(found) - 3} more" if len(found) > 3 else ""
    return (f"Heads up: {quoted(name)} is already {'; '.join(bits)}{more}. Check for a conflict before you "
            "submit them to another client.")


def board_badges(store: Store, job_id: int) -> dict[int, tuple[str, str]]:
    """For the board: candidate id -> 'In play at 1 other client' (with the client names in the title)."""
    out = {}
    for a in store.pipeline(job_id):
        found = other_clients(store, a["candidate_id"], job_id)
        if found:
            n = len({f["client"].lower() for f in found})
            out[a["candidate_id"]] = (f"In play at {n} other client{'' if n == 1 else 's'}",
                                      "; ".join(f"{f['stage']} at {f['client']}" for f in found))
    return out


__all__ = ["MOVES_KEY", "OUTREACH_KINDS", "REPLY_KIND", "DEFAULT_STEPS", "advance", "board_badges", "clean_steps",
           "contact_stage", "draft", "due", "job_for", "logged_today", "mark_reply", "other_clients",
           "other_clients_text", "progress", "save_sequence", "sequence", "steps_from_form", "summary_line",
           "today_items"]
