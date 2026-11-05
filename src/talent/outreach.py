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
BATCH_AT = 3  # this many follow-ups due at the same step of one job fold into one Today row
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


def logged_kind_today(store: Store, candidate_id: int, kind: str, job_id: int | None,
                      now: datetime | None = None) -> bool:
    """Whether this kind of outreach (not a reply) was already logged for them today on this job (on the
    recruiter's clock), so a repeated batch doesn't log it twice."""
    now = now or datetime.now(timezone.utc)
    today = as_local(now).date()
    since = (now - timedelta(days=2)).replace(microsecond=0).isoformat()
    rows = store._rows(f"SELECT t.at, t.data FROM timeline t WHERE t.candidate_id = ? AND t.kind = ? AND t.at >= ?"
                       f" AND t.job_id IS ? AND {NOT_STRUCK}", (candidate_id, kind, since, job_id))
    return any(as_local(to_dt(r["at"])).date() == today and not _json(r["data"]).get("reply") for r in rows)


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
STEP_KEYS = {"label": ("label", "name", "title"), "day": ("day", "days", "after_days", "after", "offset"),
             "text": ("text", "body", "message", "template", "content")}
STEP_EXAMPLE = '{"label": "Nudge", "day": 3, "text": "Hi {first_name}, just checking in."}'


def _step_fields(s: dict[str, Any], i: int) -> dict[str, Any]:
    """A step's label, day and text, accepting the usual other names for each. Names it doesn't know are an error
    that says which keys to use."""
    known = {a for names in STEP_KEYS.values() for a in names}
    odd = [str(k) for k in s if str(k) not in known]
    if odd:
        raise ValueError(f"Step {i + 1} has keys I don't know ({', '.join(odd)}). Each step takes label, day and "
                         f"text, for example {STEP_EXAMPLE}.")
    return {k: next((s[a] for a in names if s.get(a) is not None), None) for k, names in STEP_KEYS.items()}


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
        f = _step_fields(s, i)
        label = " ".join(str(f["label"] or "").split())[:LABEL_LIMIT]
        text = str(f["text"] or "").strip()[:TEXT_LIMIT]
        day_raw = str(f["day"] if f["day"] is not None else "").strip()
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
            keep = {k: (old or {})[k] for k in ("enrolled", "paused", "starts") if (old or {}).get(k)}
            value = json.dumps({"steps": clean, "since": (old or {}).get("since") or now_iso(), **keep})
            store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                                " value = excluded.value", (key, value))
    return clean


def steps_from_form(form: Any) -> list[dict[str, Any]]:
    return [{"label": form.get(f"label_{i}"), "day": form.get(f"day_{i}"), "text": form.get(f"text_{i}")}
            for i in range(MAX_STEPS)]


def preview(steps: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """When each follow-up would come due for someone messaged today: [{"label", "day", "on"}]."""
    today = as_local(now or datetime.now(timezone.utc)).date()
    return [{"label": s["label"], "day": s["day"], "on": today + timedelta(days=s["day"])} for s in steps[1:]]


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


def _scan(store: Store, job_id: int, now: datetime) -> tuple[dict[int, list[date]], dict[int, list[date]]]:
    """Every call, email and message logged on the job up to now, by person, as days (oldest first, one per day):
    (outreach days, reply days). Struck-out entries are left out."""
    rows = store._rows(
        f"""SELECT t.candidate_id, t.kind, t.at, t.data FROM timeline t
            WHERE t.job_id = ? AND t.kind IN ('call', 'email', 'message') AND t.at <= ? AND {NOT_STRUCK}
            ORDER BY t.at""", (job_id, now.replace(microsecond=0).isoformat()))
    touches: dict[int, list[date]] = {}
    replies: dict[int, list[date]] = {}
    for r in rows:
        day = _local_day(r["at"])
        days = (replies if _json(r["data"]).get("reply") else touches).setdefault(r["candidate_id"], [])
        if day not in days:
            days.append(day)
    return touches, replies


def _follows(days: list[date], cid: int, since_day: date, enrolled: set[int]) -> list[date]:
    """The days of outreach that count toward someone's sequence: all of them once enrolled, else those since it
    was set."""
    return days if cid in enrolled else [d for d in days if d >= since_day]


def progress(store: Store, job_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Where each person on this job is in its sequence: people with outreach logged on the job since the sequence
    was set, or enrolled later. status: due (the next step is due today or earlier), waiting, paused, replied,
    moved_on or finished."""
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
    enrolled, paused = set(seq.get("enrolled") or []), set(seq.get("paused") or [])
    starts = seq.get("starts") or {}
    all_touches, all_replies = _scan(store, job_id, now)
    apps = {a["candidate_id"]: a for a in store.pipeline(job_id)}
    out = []
    for cid, every in all_touches.items():
        days = _follows(every, cid, since_day, enrolled)
        if days and str(cid) in starts:  # enrolled "from today": their earlier messages count as the first step
            begin = date.fromisoformat(starts[str(cid)])
            days = [begin] + [d for d in days if d > begin]
        a = apps.get(cid)
        if not days or a is None:
            continue
        first, n = days[0], len(days)
        reply = next((d for d in _follows(all_replies.get(cid, []), cid, since_day, enrolled) if d >= first), None)
        i = _at(stages, a["stage"])
        person = {"candidate_id": cid, "name": a["candidate_name"], "stage": a["stage"], "sent": n,
                  "steps": len(steps), "first_on": first.isoformat(), "last_on": days[-1].isoformat()}
        if reply:
            out.append({**person, "status": "replied", "replied_on": reply.isoformat()})
        elif i is not None and (stages[i]["outcome"] != "open" or i > (reach or 0)):
            out.append({**person, "status": "moved_on"})
        elif n >= len(steps):
            out.append({**person, "status": "finished"})
        else:
            step = steps[n]
            due = first + timedelta(days=step["day"])
            if due <= days[-1]:  # sent the last one late: the next comes a day after it at the earliest
                due = days[-1] + timedelta(days=1)
            status = "paused" if cid in paused else "due" if due <= today else "waiting"
            out.append({**person, "status": status, "step": n + 1,
                        "label": step["label"], "due_on": due.isoformat(), "overdue": due < today,
                        "draft": draft(step, {"name": a["candidate_name"]}, job)})
    order = {"due": 0, "waiting": 1, "paused": 2, "finished": 3, "replied": 4, "moved_on": 5}
    out.sort(key=lambda p: (order[p["status"]], p.get("due_on") or "", p["name"]))
    return out


def not_following(store: Store, job_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """People already messaged on this job before its sequence was set (and not enrolled since) who are still in
    Sourced or Contacted and haven't replied: [{"candidate_id", "name", "stage", "sent", "first_on", "last_on"}],
    the longest wait first. They follow nothing until they are enrolled."""
    seq = sequence(store, job_id)
    if not seq:
        return []
    now = now or datetime.now(timezone.utc)
    stages = store.job_stages(job_id)
    contact = contact_stage(store, job_id)
    reach = _at(stages, contact["name"]) if contact else 0
    since_day = _local_day(seq["since"])
    enrolled = set(seq.get("enrolled") or [])
    all_touches, all_replies = _scan(store, job_id, now)
    apps = {a["candidate_id"]: a for a in store.pipeline(job_id)}
    out = []
    for cid, days in all_touches.items():
        a = apps.get(cid)
        if a is None or cid in enrolled or any(d >= since_day for d in days):
            continue
        if any(d >= days[0] for d in all_replies.get(cid, [])):
            continue
        i = _at(stages, a["stage"])
        if i is not None and (stages[i]["outcome"] != "open" or i > (reach or 0)):
            continue
        out.append({"candidate_id": cid, "name": a["candidate_name"], "stage": a["stage"], "sent": len(days),
                    "first_on": days[0].isoformat(), "last_on": days[-1].isoformat()})
    out.sort(key=lambda p: (p["last_on"], p["name"]))
    return out


def _put_seq(store: Store, job_id: int, seq: dict[str, Any] | None) -> None:
    """Write (or with None, remove) a job's sequence setting as it is. In a transaction of the caller's."""
    key = f"{SEQ_PREFIX}{job_id}"
    if seq is None:
        store._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                            " value = excluded.value", (key, json.dumps(seq)))


START_CHOICES = ("today", "first_message")


def clean_start(raw: Any) -> str:
    """'today' (the default) or 'first_message'; anything else is a plain error."""
    v = str(raw or "today").strip().lower().replace(" ", "_").replace("-", "_")
    if v not in START_CHOICES:
        raise ValueError("enroll_from is 'today' (count from today) or 'first_message' (count from their first message).")
    return v


def enroll_existing(store: Store, job_id: int, candidate_ids: list[int] | None = None,
                    start: str = "today") -> list[dict[str, Any]]:
    """Enroll people already messaged on this job (see not_following) into its sequence, all of them or just these
    ids. start="today" (the default) counts the clock from today, their earlier message being step one, so nobody
    shows overdue at once; start="first_message" counts from their first message. Returns who was enrolled."""
    start = clean_start(start)
    seq = sequence(store, job_id)
    if not seq:
        raise ValueError("Set up follow-ups for this job first, then enroll the people you already messaged.")
    people = [p for p in not_following(store, job_id) if candidate_ids is None or p["candidate_id"] in candidate_ids]
    if people:
        with store._write():
            seq["enrolled"] = sorted({*(seq.get("enrolled") or []), *(p["candidate_id"] for p in people)})
            if start == "today":
                today = as_local(datetime.now(timezone.utc)).date().isoformat()
                seq["starts"] = {**(seq.get("starts") or {}), **{str(p["candidate_id"]): today for p in people}}
            _put_seq(store, job_id, seq)
    return people


def set_paused(store: Store, job_id: int, candidate_ids: list[int], paused: bool) -> int:
    """Pause (or resume) follow-ups for these people on this job. Returns how many changed."""
    seq = sequence(store, job_id)
    if not seq:
        raise ValueError("Set up follow-ups for this job first.")
    now = set(seq.get("paused") or [])
    wanted = now | set(candidate_ids) if paused else now - set(candidate_ids)
    if wanted != now:
        with store._write():
            seq["paused"] = sorted(wanted)
            _put_seq(store, job_id, seq)
    return len(wanted ^ now)


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


def _generic_text(store: Store, job_id: int, step: int) -> str:
    """A step's text with the job and client filled in; {first_name} stays for the recruiter to swap."""
    seq = sequence(store, job_id) or {"steps": []}
    s = seq["steps"][step - 1] if 0 < step <= len(seq["steps"]) else {}
    job = store.get_job(job_id)
    text = (s.get("text") or "").replace("{name}", "{first_name}")
    return text.replace("{job}", job.get("title") or "this").replace("{company}", job.get("company") or "our team")


def today_items(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """Today rows: each follow-up due, or one row per job and step when several people are due at the same step
    (one text to copy for the step, a copy button for each person, and one "I sent them all")."""
    items: list[dict[str, Any]] = []
    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for p in due(store, now=now):
        groups.setdefault((p["job_id"], p["step"]), []).append(p)
    for (jid, step), ps in groups.items():
        if len(ps) >= BATCH_AT:
            names = ", ".join(p["name"] for p in ps[:3]) + (f" and {len(ps) - 3} more" if len(ps) > 3 else "")
            items.append({"key": f"followups:{jid}:{step}:{ps[0]['due_on']}", "kind": "followup", "rank": 1,
                          "when": min(p["due_on"] for p in ps) + "T00:00:00+00:00", "job_id": jid,
                          "title": f"{len(ps)} follow-ups due: {ps[0]['label']} for {ps[0]['job']}",
                          "detail": f"Step {step} of {ps[0]['steps']} · " + names + ".",
                          "batch": [{"candidate_id": p["candidate_id"], "name": p["name"], "draft": p["draft"]}
                                    for p in ps],
                          "draft": _generic_text(store, jid, step), "step": step,
                          "overdue": any(p["overdue"] for p in ps), "href": f"/jobs/{jid}#followups"})
            continue
        for p in ps:
            items.append({"key": f"followup:{p['candidate_id']}:{jid}:{p['step']}", "kind": "followup", "rank": 1,
                          "when": p["due_on"] + "T00:00:00+00:00", "candidate_id": p["candidate_id"], "job_id": jid,
                          "title": f"{p['label']} to {p['name']}",
                          "detail": f"{p['job']} · step {p['step']} of {p['steps']} · {due_text(p, now)}",
                          "draft": p["draft"], "overdue": p["overdue"], "href": f"/jobs/{jid}#followups"})
    return items + reply_items(store, now)


def follow_up_words(p: dict[str, Any]) -> str:
    """'follow-up 1 of 2': step 1 is the first message, so the follow-ups count from step 2."""
    return f"follow-up {p['step'] - 1} of {p['steps'] - 1}"


def summary_line(p: dict[str, Any], now: datetime | None = None) -> str:
    """One plain line for the AI app: who, which step, when."""
    if p["status"] in ("due", "waiting"):
        return (f"{quoted(p['name'])}: {p['label']} ({follow_up_words(p)}), {due_text(p, now)}")
    word = {"replied": "replied", "moved_on": f"moved on to {p['stage']}", "paused": "follow-ups paused",
            "finished": f"no reply after all {p['steps']} steps"}[p["status"]]
    return f"{quoted(p['name'])}: {word}"


def copy_sequence(store: Store, job_id: int, from_job_id: int) -> list[dict[str, Any]]:
    """Use another job's follow-up steps on this job (they replace any it had). {job} and {company} in the text
    fill in with this job's own title and client."""
    if from_job_id == job_id:
        raise ValueError("Pick another job to copy the steps from.")
    store.get_job(job_id)
    src = sequence(store, from_job_id)
    if not src:
        raise ValueError("That job has no follow-up steps to copy.")
    return save_sequence(store, job_id, [dict(s) for s in src["steps"]])


def sequence_jobs(store: Store, exclude: int | None = None) -> list[dict[str, Any]]:
    """Jobs with a follow-up sequence (open ones first), to copy steps from: [{"id", "name", "steps"}]."""
    out = []
    for r in store._rows(f"SELECT key FROM settings WHERE key LIKE '{SEQ_PREFIX}%'"):
        tail = r["key"][len(SEQ_PREFIX):]
        if not tail.isdigit() or int(tail) == exclude:
            continue
        job = store._row("SELECT id, title, company, status FROM jobs WHERE id = ?", (int(tail),))
        seq = sequence(store, int(tail))
        if job and seq:
            out.append({"id": job["id"], "name": job_label(job["title"], job.get("company")),
                        "steps": len(seq["steps"]), "open": job["status"] == "open"})
    out.sort(key=lambda j: (not j["open"], j["name"].lower(), j["id"]))
    return out


def reply_stage(store: Store, candidate_id: int, job_id: int) -> dict[str, Any] | None:
    """After someone replied: {"from", "to"} when they still sit in the job's first or Contacted stage and the
    job has an open stage after Contacted (Screening, say). The recruiter decides; nothing moves by itself."""
    job = store._row("SELECT status FROM jobs WHERE id = ?", (job_id,))
    app = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id))
    if not job or job["status"] != "open" or not app:
        return None
    stages = store.job_stages(job_id)
    i = _at(stages, app["stage"])
    contact = contact_stage(store, job_id)
    reach = _at(stages, contact["name"]) if contact else None
    if i is None or reach is None or stages[i]["outcome"] != "open" or i > reach:
        return None
    nxt = next((s for s in stages[reach + 1:] if s["outcome"] == "open"), None)
    return {"from": app["stage"], "to": nxt["name"]} if nxt else None


REPLY_DAYS = 14  # a reply offers its stage move on Today for this long


def reply_items(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """Today rows: people who replied on a job in the last two weeks and still sit in Sourced or Contacted there,
    each with a "Move to Screening" button (next_stage)."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=REPLY_DAYS)).replace(microsecond=0).isoformat()
    rows = store._rows(
        f"""SELECT t.candidate_id, t.job_id, max(t.at) AS at, c.name, j.title, j.company FROM timeline t
            JOIN candidates c ON c.id = t.candidate_id JOIN jobs j ON j.id = t.job_id
            WHERE t.kind = 'message' AND t.job_id IS NOT NULL AND t.at >= ? AND t.at <= ? AND {NOT_STRUCK}
              AND json_valid(t.data) AND json_extract(t.data, '$.reply') = 1
            GROUP BY t.candidate_id, t.job_id ORDER BY max(t.at) DESC""",
        (since, now.replace(microsecond=0).isoformat()))
    items = []
    for r in rows:
        move = reply_stage(store, r["candidate_id"], r["job_id"])
        if not move:
            continue
        items.append({"key": f"replied:{r['candidate_id']}:{r['job_id']}", "kind": "replied", "rank": 1,
                      "when": r["at"], "candidate_id": r["candidate_id"], "job_id": r["job_id"],
                      "title": f"{r['name']} replied. Move them to {move['to']}?",
                      "detail": f"{job_label(r['title'], r.get('company'))} · still in {move['from']}",
                      "stage": move["from"], "next_stage": move["to"], "href": f"/candidates/{r['candidate_id']}"})
    return items


# ---------------------------------------------------------- batch and bulk
def log_sent_batch(store: Store, job_id: int, candidate_ids: list[int], now: datetime | None = None) -> dict[str, Any]:
    """"I sent them all": log the follow-up each person is due as a message on this job, once each. Anyone who
    already has a message logged on the job today is left alone, so a second press logs nothing twice.
    Returns {"logged": [names], "already": [names]}."""
    store.get_job(job_id)
    steps = {p["candidate_id"]: p for p in progress(store, job_id, now)}
    logged, already = [], []
    for cid in candidate_ids[:200]:
        cand = store.get_candidate(cid)
        if logged_kind_today(store, cid, "message", job_id, now):
            already.append(cand["name"])
            continue
        label = (steps.get(cid) or {}).get("label") or "Message"
        entry = store.log(cid, "message", f"{label} sent", job_id=job_id, source="App")
        advance(store, cid, job_id, "message", entry["at"])
        logged.append(cand["name"])
    return {"logged": logged, "already": already}


def _names(names: list[str], most: int = 3) -> str:
    return ", ".join(names[:most]) + (f" and {len(names) - most} more" if len(names) > most else "")


def bulk_apply(store: Store, job_id: int, action: str, candidate_ids: list[int], start: str = "today") -> str:
    """The Outreach tab's bulk actions on the people ticked: sent, replied, pause, resume or enroll. Returns the
    one-line notice. Raises ValueError with a plain reason."""
    if not candidate_ids:
        raise ValueError("Tick the people first.")
    store.get_job(job_id)
    if action == "sent":
        got = log_sent_batch(store, job_id, candidate_ids)
        text = f"Logged a message to {len(got['logged'])} {'person' if len(got['logged']) == 1 else 'people'}"
        text += f" ({_names(got['logged'])})." if got["logged"] else "."
        if got["already"]:
            text += f" Already logged today for {_names(got['already'])}, so not again."
        return text
    if action == "replied":
        done = []
        for cid in candidate_ids[:200]:
            if cid in {p["candidate_id"] for p in progress(store, job_id) if p["status"] == "replied"}:
                continue
            entry = store.log(cid, "message", "Replied", job_id=job_id, source="App")
            mark_reply(store, entry["id"])
            done.append(store.get_candidate(cid)["name"])
        return (f"Marked {len(done)} as replied ({_names(done)}). Their follow-ups stop." if done
                else "They are all marked as replied already.")
    if action in ("pause", "resume"):
        n = set_paused(store, job_id, candidate_ids, action == "pause")
        word = "Paused" if action == "pause" else "Resumed"
        return f"{word} follow-ups for {n} {'person' if n == 1 else 'people'}." if n else "Nothing to change."
    if action == "enroll":
        got = enroll_existing(store, job_id, candidate_ids, start)
        return (f"Enrolled {len(got)} {'person' if len(got) == 1 else 'people'} ({_names([g['name'] for g in got])}). "
                + ("Their next follow-up counts from today." if clean_start(start) == "today"
                   else "Their next follow-up comes due from their first message.") if got
                else "None of them needed enrolling: they follow the steps already, or have replied or moved on.")
    raise ValueError("Pick an action.")


# ------------------------------------------------------------ outreach tab
def _rate(n: int, of: int) -> str:
    return f"{round(100 * n / of)}%" if of else "none yet"


FILTERS = ("due", "quiet", "replied", "paused")
FILTER_OF = {"due": "due", "waiting": "due", "paused": "paused", "replied": "replied", "moved_on": "replied"}


def table(store: Store, job_id: int, now: datetime | None = None, only: str | None = None) -> dict[str, Any]:
    """Everyone messaged on a job, for the Outreach tab and list_outreach: one row per person (first message, how
    many sent, current step, next due, replied or not, source) and reply rates per step and per source.
    A step's rate is the people who replied after it, of those who got it. Rows come due first (overdue, due
    today, later), then paused, quiet (nothing to do) and replied; only= one of FILTERS shows just that group."""
    now = now or datetime.now(timezone.utc)
    today = as_local(now).date().isoformat()
    job, seq = store.get_job(job_id), sequence(store, job_id)
    steps = (seq or {}).get("steps") or []
    stages = store.job_stages(job_id)
    contact = contact_stage(store, job_id)
    reach = _at(stages, contact["name"]) if contact else 0
    touches, replies = _scan(store, job_id, now)
    prog = {p["candidate_id"]: p for p in progress(store, job_id, now)}
    loose = {p["candidate_id"] for p in not_following(store, job_id, now)}
    apps = {a["candidate_id"]: a for a in store.pipeline(job_id)}
    ids = [c for c in touches if c in apps]
    sources = {r["id"]: (r["source"] or "").strip() for r in store._rows(
        f"SELECT id, source FROM candidates WHERE id IN ({', '.join('?' * len(ids))})", tuple(ids))} if ids else {}
    people, sent_at, got_reply = [], {}, {}
    for cid in ids:
        days, a = touches[cid], apps[cid]
        reply = next((d for d in replies.get(cid, []) if d >= days[0]), None)
        p = prog.get(cid)
        i = _at(stages, a["stage"])
        if reply:
            status = "replied"
        elif p:
            status = p["status"]
        elif i is not None and (stages[i]["outcome"] != "open" or i > (reach or 0)):
            status = "moved_on"
        else:
            status = "not_following" if cid in loose else "no_sequence"
        row = {"candidate_id": cid, "name": a["candidate_name"], "stage": a["stage"], "source": sources.get(cid) or "",
               "first_on": days[0].isoformat(), "last_on": days[-1].isoformat(), "sent": len(days), "status": status,
               "replied": bool(reply), "replied_on": reply.isoformat() if reply else None,
               "step": f"{min(len(days), len(steps))} of {len(steps)}" if steps and cid not in loose else None,
               "next_label": p.get("label") if p and status in ("due", "waiting", "paused") else None,
               "next_due": p.get("due_on") if p and status in ("due", "waiting", "paused") else None,
               "next_due_text": due_text(p, now) if p and status in ("due", "waiting") else None}
        for k in range(1, len(days) + 1):
            sent_at[k] = sent_at.get(k, 0) + 1
        if reply:
            k = sum(1 for d in days if d <= reply) or 1
            got_reply[k] = got_reply.get(k, 0) + 1
        people.append(row)
    for r in people:
        d = r["next_due"] if r["status"] in ("due", "waiting") else None
        r["group"] = FILTER_OF.get(r["status"], "quiet")
        r["rank"] = 0 if d and d < today else 1 if d and d == today else 2 if d else 3 if r["status"] == "paused" else \
            4 if r["group"] == "quiet" else 5
    people.sort(key=lambda r: (r["rank"], r["next_due"] or "9999", r["name"]))
    counts = {f: sum(1 for r in people if r["group"] == f) for f in FILTERS}
    only = only if only in FILTERS else None
    shown = [r for r in people if r["group"] == only] if only else people
    by_step = []
    for k in range(1, max([len(steps), *sent_at, 0]) + 1):
        n, r = sent_at.get(k, 0), got_reply.get(k, 0)
        by_step.append({"step": k, "label": steps[k - 1]["label"] if k <= len(steps) else f"Message {k}",
                        "sent": n, "replied": r, "rate": _rate(r, n)})
    by_source: dict[str, dict[str, int]] = {}
    for r in people:
        g = by_source.setdefault(r["source"] or "No source saved", {"contacted": 0, "replied": 0})
        g["contacted"] += 1
        g["replied"] += int(r["replied"])
    sources_out = sorted(({"source": k, **v, "rate": _rate(v["replied"], v["contacted"])} for k, v in by_source.items()),
                         key=lambda g: (-g["contacted"], g["source"]))
    replied = sum(1 for r in people if r["replied"])
    return {"job": {"id": job_id, "title": job["title"], "company": job.get("company")}, "has_sequence": bool(seq),
            "people": shown, "filter": only, "counts": counts, "by_step": by_step, "by_source": sources_out,
            "totals": {"contacted": len(people), "replied": replied, "rate": _rate(replied, len(people)),
                       "due": sum(1 for r in people if r["status"] == "due"),
                       "not_following": sum(1 for r in people if r["status"] == "not_following"),
                       "paused": sum(1 for r in people if r["status"] == "paused")}}


# ------------------------------------------------------- the AI sets one
def ai_set_sequence(store: Store, job_id: int, steps: Any, enroll_existing_too: bool = False,
                    start: str = "today") -> dict[str, Any]:
    """The AI app sets a job's follow-up steps (never removes them), and with enroll_existing_too also enrolls the
    people already messaged. One logged AI action, so it shows on Changes and can be undone."""
    store.get_job(job_id)
    start = clean_start(start)
    clean = clean_steps(steps)
    if not clean:
        raise ValueError("Send at least two steps: the first message on day 0, then a follow-up on a later day. "
                         f"Each step takes label, day and text, for example {STEP_EXAMPLE}.")
    old = sequence(store, job_id)
    save_sequence(store, job_id, clean)
    new = sequence(store, job_id) or {}
    enrolled = enroll_existing(store, job_id, None, start) if enroll_existing_too else []
    final = sequence(store, job_id) or {}
    with store._write():
        action = store._log_ai("sequence_set", job_id=job_id, old={"sequence": old}, new={"sequence": final},
                               body=f"{len(clean)} steps" + (f", enrolled {len(enrolled)} already messaged"
                                                              if enrolled else ""))
    return {"steps": clean, "enrolled": [{"candidate_id": p["candidate_id"], "name": p["name"]} for p in enrolled],
            "not_following": len(not_following(store, job_id)), "action_id": action,
            "replaced": bool(old), "since": new.get("since")}


def ai_blocker(store: Store, a: dict[str, Any]) -> str | None:
    """Why an AI sequence change can't be put back: it was changed again since."""
    now = sequence(store, a["job_id"]) if a.get("job_id") else None
    return None if now == (a.get("new_data") or {}).get("sequence") else "the follow-up steps were changed again since"


def ai_undo(store: Store, a: dict[str, Any]) -> str:
    reason = ai_blocker(store, a)
    if reason:
        raise ValueError(reason[:1].upper() + reason[1:] + ".")
    with store._write():
        _put_seq(store, a["job_id"], (a.get("old_data") or {}).get("sequence"))
        store._conn.execute("UPDATE ai_actions SET undone_at = ? WHERE id = ?", (now_iso(), a["id"]))
    return a["summary"]


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


__all__ = ["preview", "ai_blocker", "ai_set_sequence", "ai_undo", "bulk_apply", "enroll_existing", "follow_up_words",
           "log_sent_batch", "not_following", "set_paused", "table", "MOVES_KEY", "OUTREACH_KINDS", "REPLY_KIND", "DEFAULT_STEPS", "advance", "board_badges", "clean_steps",
           "contact_stage", "copy_sequence", "draft", "logged_kind_today", "reply_items", "reply_stage", "sequence_jobs", "due", "job_for", "logged_today", "mark_reply", "other_clients",
           "other_clients_text", "progress", "save_sequence", "sequence", "steps_from_form", "summary_line",
           "today_items"]
