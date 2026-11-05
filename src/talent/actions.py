"""Today's action items, computed on read from the database.

Nothing here is stored except snoozes, AI suggestions and follow-up dates. Each
kind of item can be switched off in Settings, and its threshold changed.
"""
from __future__ import annotations

import difflib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any


from .store import LIVE_INTERVIEW, NotFound, Store, _stage_key, _stage_plan, parse_when, to_dt

FLAGS: tuple[tuple[str, str, str], ...] = (
    # (setting key, label, help)
    ("flag_interviews", "Upcoming interviews", "Interviews in the next two weeks."),
    ("flag_suggestions", "Follow-ups", "Follow-ups you scheduled and ones your AI app wrote for you."),
    ("flag_stuck", "Stuck in a stage", "Candidates who haven't moved stage in a while."),
    ("flag_thin", "Jobs that need more candidates", "Open jobs with too few people still in the running."),
    ("flag_stale", "Profiles that may be out of date", "Candidates whose details haven't been updated in months."),
)

THRESHOLDS: tuple[tuple[str, str], ...] = (
    ("stuck_days", "Stuck after this many days in a stage"),
    ("thin_min", "A job needs more candidates below this many (3 per opening, up to this)"),
    ("stale_days", "A profile may be out of date after this many days"),
)

STALE_NAMES = 3
NAMES_PER_STAGE = 10  # a status update lists this many people per stage, then "and 233 more"
BOARD_CARDS = 50  # cards per board column before "Show more"
# Stages a client or hiring manager shouldn't see by default (people not presented yet).
INTERNAL_STAGE_WORDS = frozenset({"sourced", "contacted", "new", "lead", "leads", "applied", "prospect", "prospects"})
OUT_STAGES = ("Rejected", "Withdrawn")
CLOSE_REASON = "Reason: Position filled"
INHOUSE_KEY = "inhouse_wording"
STAGE_SETS: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "default": ("Default", None),
    "agency": ("Agency", ("Sourced", "Screened", "Submitted", "Client interview", "Offer", "Placed", "Rejected",
                          "Withdrawn")),
    "inhouse": ("In-house", ("Applied", "Recruiter screen", "Hiring manager screen", "Interviews", "Offer", "Hired",
                             "Rejected", "Withdrawn")),
}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _days_ago(iso: str | None, now: datetime) -> int:
    dt = to_dt(iso)
    return (now - dt).days if dt else 0


def _int(cfg: dict[str, str], key: str, default: int) -> int:
    raw = str(cfg.get(key) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def job_label(title: str | None, company: str | None) -> str:
    """'Senior Backend Engineer, Acme Payments' (or just the title)."""
    return ", ".join(x for x in (title, company) if x)


def twin_hints(jobs: list[dict[str, Any]]) -> dict[int, str]:
    """{job id: location, or '#id'} for open jobs that share a title and company with another open job,
    so two "Backend Engineer, Acme" openings can be told apart in lists."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for j in jobs:
        if j.get("status", "open") == "open":
            groups.setdefault(((j.get("title") or "").lower(), (j.get("company") or "").lower()), []).append(j)
    return {j["id"]: (j.get("location") or f"#{j['id']}") for g in groups.values() if len(g) > 1 for j in g}


def local_time(iso: str | None, fmt: str = "%a %b %-d, %-I:%M %p") -> str:
    dt = to_dt(iso)
    if not dt:
        return ""
    local = dt.astimezone()
    try:
        return local.strftime(fmt)
    except ValueError:  # Windows strftime has no %-d
        return local.strftime(fmt.replace("%-", "%#"))


def _in_stage(days: int) -> str:
    return "today" if days <= 0 else "1 day" if days == 1 else f"{days} days"


# ---------------------------------------------------------------- settings
def _put_setting(store: Store, key: str, value: str) -> None:
    # Store.set_settings only takes the known settings, so these extra keys are written directly.
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))


def _drop_setting(store: Store, key: str) -> None:
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# --------------------------------------------------------------- follow-ups
def _relative_day(v: str, today: date) -> date | None:
    """'tomorrow', 'friday', 'this fri', 'next tue', 'in 3 days', 'in 2 weeks' -> a day. None if it isn't one."""
    low = re.sub(r"\s+", " ", v.strip().lower().rstrip("."))
    if low == "tomorrow":
        return today + timedelta(days=1)
    m = re.fullmatch(r"in (\d{1,3}) (day|days|week|weeks)", low)
    if m:
        n = int(m.group(1))
        return today + timedelta(days=n * (7 if m.group(2).startswith("week") else 1))
    if low in ("next week",):
        return today + timedelta(days=7)
    m = re.fullmatch(r"(?:(this|next|on) )?([a-z]{3,9})", low)
    if m:
        word = m.group(2)
        hits = [i for i, d in enumerate(WEEKDAYS) if d.startswith(word)]
        if len(hits) == 1:
            ahead = (hits[0] - today.weekday()) % 7 or 7  # always a day after today: the coming one
            return today + timedelta(days=ahead)
    return None


def parse_day(value: str, today: date | None = None) -> date:
    """'2026-12-01', any date store.parse_when reads ('12/1/2026', 'Dec 1, 2026', 'today'), or a word like
    'tomorrow', 'friday', 'next tue' or 'in 3 days', as a local day. A weekday is the coming one."""
    v = (value or "").strip()
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        pass
    rel = _relative_day(v, today or datetime.now().astimezone().date())
    if rel is not None:
        return rel
    try:
        return to_dt(parse_when(v, default_now=False)).astimezone().date()
    except (ValueError, AttributeError):
        raise ValueError(f"Couldn't read the follow-up date {v!r}. Try 2026-12-01, Dec 1, friday or tomorrow.") from None


def add_followup(store: Store, text: str, on: date | str | None = None, *, candidate_id: int | None = None,
                 job_id: int | None = None, source: str = "App", note: str | None = None) -> dict[str, Any]:
    """Add a to-do to Today. With `on`, it shows from that day on (the log form's "Follow up on")."""
    day = parse_day(on) if isinstance(on, str) else on
    s = store.add_suggestion(text, candidate_id=candidate_id, job_id=job_id, source=source,
                             due_on=day.isoformat() if day else None, note=note)
    return {**s, "due_on": s.get("due_on")} if s.get("due_on") else {k: v for k, v in s.items() if k != "due_on"}


def schedule_followup(store: Store, candidate_id: int, on: date | str, job_id: int | None = None,
                      note: str | None = None) -> dict[str, Any]:
    """Add "Follow up with <name>" to Today, starting on the given day, with the note it was logged with."""
    cand = store.get_candidate(candidate_id)
    return add_followup(store, f"Follow up with {cand['name']}", on, candidate_id=candidate_id, job_id=job_id,
                        note=note)


def complete_followup(store: Store, suggestion_id: int) -> None:
    store.complete_suggestion(suggestion_id)


def _day_label(day: str) -> str:
    return local_time(day + "T12:00:00", "%a %b %-d") if day else ""


def due_text(due_on: str | None, today: str) -> str:
    """'Due today', 'Due Fri Nov 6', or 'Overdue, due Wed Nov 4'."""
    if not due_on:
        return ""
    if due_on == today:
        return "Due today"
    return ("Overdue, due " if due_on < today else "Due ") + _day_label(due_on)


# ------------------------------------------------------------- interviews
def _closing_pairs(store: Store, candidate_ids: set[int]) -> set[tuple[int, int]]:
    """(candidate, job) pairs whose stage is a closing one (hired or no longer in the running)."""
    if not candidate_ids:
        return set()
    marks = ",".join("?" * len(candidate_ids))
    rows = store._rows(
        f"""SELECT a.candidate_id, a.job_id FROM applications a
            JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
            WHERE s.outcome != 'open' AND a.candidate_id IN ({marks})""", sorted(candidate_ids))
    return {(r["candidate_id"], r["job_id"]) for r in rows}


def _live_interviews(store: Store, rows: list[dict[str, Any]], jobs: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop interviews for closed jobs and for candidates whose application there has closed."""
    closing = _closing_pairs(store, {r["candidate_id"] for r in rows if r.get("job_id")})
    out = []
    for r in rows:
        jid = r.get("job_id")
        if jid is not None:
            job = jobs.get(jid)
            if job and job["status"] == "closed":
                continue
            if (r["candidate_id"], jid) in closing:
                continue
        out.append(r)
    return out


def next_interviews(store: Store, job_id: int, now: datetime | None = None) -> dict[int, str]:
    """{candidate_id: ISO time} of the next scheduled interview for each candidate still in the running for
    this job (interviews for this job or with no job). Cancelled ones and ones with an outcome don't count."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).replace(microsecond=0).isoformat()
    rows = store._rows(
        f"""SELECT t.candidate_id, min(t.at) AS at FROM timeline t
           JOIN applications a ON a.candidate_id = t.candidate_id AND a.job_id = ?
           JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
           WHERE t.kind = 'interview' AND t.at >= ? AND (t.job_id = ? OR t.job_id IS NULL)
             AND s.outcome = 'open' AND {LIVE_INTERVIEW}
           GROUP BY t.candidate_id""", (job_id, start, job_id))
    return {r["candidate_id"]: r["at"] for r in rows}


def ai_identity_changes(store: Store, now: datetime | None = None, days: int = 7) -> list[dict[str, Any]]:
    """One Today item per candidate whose name, email, phone or LinkedIn the AI app changed lately
    (and nobody undid), so a prompt-injected profile can't quietly redirect outreach."""
    import json

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    watched = {"name": "name", "email": "email", "phone": "phone", "linkedin_url": "LinkedIn"}
    rows = store._rows(
        """SELECT t.id, t.candidate_id, t.at, t.data, c.name AS candidate_name FROM timeline t
           JOIN candidates c ON c.id = t.candidate_id
           WHERE t.kind = 'change' AND t.source = 'AI app' AND t.at >= ? ORDER BY t.at DESC, t.id DESC""", (since,))
    found: dict[int, dict[str, Any]] = {}
    for r in rows:
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        fields = [f for f in watched if f in (data.get("new") or {})]
        if not fields or data.get("undone_at"):
            continue
        item = found.setdefault(r["candidate_id"], {
            "key": f"aichange:{r['id']}", "kind": "ai_change", "rank": 1, "when": r["at"], "fields": [],
            "candidate_id": r["candidate_id"], "name": r["candidate_name"], "href": "/changes?who=ai&since=7"})
        item["fields"] += [f for f in fields if f not in item["fields"]]
    out = []
    for item in found.values():
        fields = item.pop("fields")
        labels = [watched[f] for f in watched if f in fields]
        what = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
        name = item.pop("name")
        out.append({**item, "title": f"Your AI app changed {name}'s {what}. Review or undo.",
                    "detail": "Check it's right before you reach out."})
    return out


def ai_pipeline_changes(store: Store, now: datetime | None = None, days: int = 7) -> list[dict[str, Any]]:
    """Today items for what the AI app did to pipelines lately (and nobody undid): people it moved out
    of a job, and jobs it closed or put on hold."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    out_by_job: dict[int, dict[str, Any]] = {}
    for r in store._rows(
            """SELECT a.id, a.job_id, a.at, j.title FROM ai_actions a JOIN jobs j ON j.id = a.job_id
               JOIN timeline t ON t.id = a.target_id
               JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(t.body)
               WHERE a.kind = 'stage_move' AND a.undone_at IS NULL AND a.at >= ? AND s.outcome = 'lost'
               ORDER BY a.id DESC""", (since,)):
        item = out_by_job.setdefault(r["job_id"], {"id": r["id"], "at": r["at"], "title": r["title"], "n": 0})
        item["n"] += 1
    items = [{"key": f"aimove:{i['id']}", "kind": "ai_change", "rank": 1, "when": i["at"], "job_id": job_id,
              "title": f"Your AI app moved {i['n']} {'person' if i['n'] == 1 else 'people'} out of {i['title']}. "
                       "Review or undo.", "detail": "", "href": "/changes?who=ai&since=7"}
             for job_id, i in out_by_job.items()]
    words = {"closed": "closed", "on_hold": "put {} on hold"}
    for job in store._rows("SELECT id, title FROM jobs WHERE status IN ('closed', 'on_hold') AND updated_at >= ?",
                           (since,)):
        change = store.ai_status_change(job["id"])
        if change and change["at"] >= since:
            verb = words[change["status"]]
            what = verb.format(job["title"]) if "{}" in verb else f"{verb} {job['title']}"
            items.append({"key": f"aistatus:{job['id']}:{change['at']}", "kind": "ai_change", "rank": 1,
                          "when": change["at"], "job_id": job["id"], "title": f"Your AI app {what}.", "detail": "",
                          "href": "/changes?who=ai&since=7"})
    return items


# ------------------------------------------------------------------ jobs
def _offer_like(job: dict[str, Any]) -> bool:
    """Someone sits in an in-play stage named like Offer (Offer, Offer out, Verbal offer)."""
    return any(s["count"] and s["outcome"] == "open" and "offer" in _stage_key(s["name"]).split()
               for s in job.get("stage_counts", []))


def hire_word(store: Store, job_id: int) -> str:
    """'placed' when the job's hire stage is called Placed, else 'hired'."""
    won = [s["name"] for s in store.job_stages(job_id) if s["outcome"] == "hired"]
    return "placed" if won and _stage_key(won[0]) == "placed" else "hired"


def job_numbers(store: Store, job_id: int, omit: set[str] | None = None,
                groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """In play, hired and out counts, openings, and whether the role is filled.

    With `omit` (stage keys), the counts cover only the people in the other stages, as a status update
    shows them; `filled` always counts every hire."""
    job = store.get_job(job_id)
    groups = groups if groups is not None else store.pipeline_by_stage(job_id)
    n = {o: sum(len(g["candidates"]) for g in groups if g["outcome"] == o and _stage_key(g["name"]) not in (omit or ()))
         for o in ("open", "hired", "lost")}
    all_hired = sum(len(g["candidates"]) for g in groups if g["outcome"] == "hired")
    openings = job.get("openings") or 1
    return {"active": n["open"], "hired": n["hired"], "out": n["lost"], "openings": openings,
            "filled": all_hired >= openings, "filled_seats": min(all_hired, openings), "word": hire_word(store, job_id)}


def thin_target(job: dict[str, Any], cap: int = 5) -> int:
    """How many active candidates a job should have: its own min_active, else 3 per opening up to `cap`."""
    return job.get("min_active") or min(3 * (job.get("openings") or 1), cap)


def hired_text(hired: int, openings: int, word: str = "hired") -> str:
    """'57 hired, 1 opening', 'none hired yet, 2 openings' or 'none placed yet' (one opening)."""
    seats = f"{openings} opening{'' if openings == 1 else 's'}"
    if hired:
        return f"{hired} {word}, {seats}"
    return f"none {word} yet" + (f", {seats}" if openings > 1 else "")


def filled_text(filled: int, openings: int) -> str:
    """'1 of 2 openings filled' (filled never counts past the openings)."""
    return f"{min(filled, openings)} of {openings} opening{'' if openings == 1 else 's'} filled"


def headline(nums: dict[str, Any], status: str = "open") -> str:
    """'2 candidates in process, none placed yet. 1 out.' or '1 candidate in process, 1 hired, 2 openings.'"""
    a, h, word = nums["active"], nums["hired"], nums["word"]
    parts = [f"{a} candidate{'' if a == 1 else 's'} in process" if a else "No one in process",
             hired_text(h, nums["openings"], word)]
    text = ", ".join(parts) + "."
    if nums["out"]:
        text += f" {nums['out']} out."
    if nums["filled"] and status == "open":
        text += " The role is filled."
    prefix = {"on_hold": "On hold. ", "closed": "Closed. "}.get(status, "")
    return prefix + text


def _job_name(job: dict[str, Any], hint: str | None = None) -> str:
    """'Staff Backend Engineer (Ramp)', or '(Ramp, Austin)' when another open job has the same name."""
    extra = ", ".join(x for x in (job.get("company"), hint) if x)
    return job["title"] + (f" ({extra})" if extra else "")


# ------------------------------------------------------------------ today
def action_items(store: Store, now: datetime | None = None) -> dict[str, Any]:
    """{'items': [...], 'counts': {...}, 'hidden': n, 'scheduled': n, 'scheduled_later': n, 'stale_total': n,
    'next_week': [...]}.

    Items are ordered most urgent first: interviews, then follow-ups, then the rest.
    Interviews 8 to 14 days out, and to-dos due in the next 14 days, are listed apart under next_week and
    don't count as things to do. `scheduled` counts every future to-do, `scheduled_later` the ones after that.
    Jobs that need more candidates fold into one row, and so do out-of-date profiles.
    """
    now = now or datetime.now(timezone.utc)
    cfg = store.settings()
    on = lambda k: cfg.get(k, "1") == "1"  # noqa: E731
    snoozed = store.snoozed_keys()
    jobs = {j["id"]: j for j in store.list_jobs()}
    items: list[dict[str, Any]] = []
    hidden = scheduled = scheduled_later = 0

    hints = twin_hints(list(jobs.values()))
    today = now.astimezone().date().isoformat()

    def label(job_id: int | None, fallback: str | None = None) -> str:
        j = jobs.get(job_id) if job_id else None
        if not j:
            return fallback or ""
        name = job_label(j["title"], j.get("company"))
        return f"{name} ({hints[j['id']]})" if j["id"] in hints else name

    next_week: list[dict[str, Any]] = []
    if on("flag_interviews"):
        week_end = (now + timedelta(days=7)).replace(microsecond=0).isoformat()
        for t in _live_interviews(store, store.upcoming_interviews(days=14, now=now), jobs):
            job = jobs.get(t.get("job_id")) if t.get("job_id") else None
            bits = [label(t.get("job_id"), t.get("job_title")), t.get("body"),
                    "job on hold" if job and job["status"] == "on_hold" else None]
            row = {
                "key": f"interview:{t['id']}", "kind": "interview", "rank": 0, "when": t["at"],
                "title": f"Interview with {t['candidate_name']}", "interview_id": t["id"],
                "detail": " · ".join(x for x in bits if x), "on_hold": bool(job and job["status"] == "on_hold"),
                "candidate_id": t["candidate_id"], "job_id": t.get("job_id"),
                "href": f"/candidates/{t['candidate_id']}#timeline",
            }
            if t["at"] > week_end:
                if row["key"] in snoozed:
                    hidden += 1
                else:
                    next_week.append(row)
            else:
                items.append(row)

    if on("flag_suggestions"):
        horizon = (now + timedelta(days=14)).astimezone().date().isoformat()
        for s in store.open_suggestions():
            due = s.get("due_on") or ""
            mine = (s.get("source") or "") != "AI app"
            detail = [s.get("note"), label(s.get("job_id"), s.get("job_title"))]
            if not mine:
                detail.append("Suggested by your AI app")
            if due > today:
                scheduled += 1
                if due <= horizon:  # listed under Next week with its day, not counted as a thing to do
                    next_week.append({
                        "key": f"suggestion:{s['id']}", "kind": "suggestion", "rank": 1,
                        "when": due + "T00:00:00+00:00", "title": s["text"],
                        "detail": " · ".join(x for x in detail if x), "due_on": due, "overdue": False,
                        "due_text": due_text(due, today), "candidate_id": s.get("candidate_id"),
                        "job_id": s.get("job_id"), "suggestion_id": s["id"],
                        "href": f"/candidates/{s['candidate_id']}" if s.get("candidate_id")
                        else f"/jobs/{s['job_id']}" if s.get("job_id") else "/"})
                else:
                    scheduled_later += 1
                continue
            items.append({
                "key": f"suggestion:{s['id']}", "kind": "suggestion", "rank": 1,
                "when": (due + "T00:00:00+00:00") if due else s["created_at"],
                "title": s["text"], "detail": " · ".join(x for x in detail if x),
                "note": s.get("note"), "due_on": due or None, "overdue": bool(due and due < today),
                "due_text": due_text(due, today), "due_day": _day_label(due),
                "candidate_id": s.get("candidate_id"), "job_id": s.get("job_id"), "suggestion_id": s["id"],
                "href": f"/candidates/{s['candidate_id']}" if s.get("candidate_id")
                else f"/jobs/{s['job_id']}" if s.get("job_id") else "/",
            })

    items += ai_identity_changes(store, now) + ai_pipeline_changes(store, now)
    if on("flag_stuck"):
        limit = _int(cfg, "stuck_days", 7)
        for a in store.open_applications():
            days = _days_ago(a["stage_changed_at"], now)
            if days >= limit:
                nxt = store.next_stage(a["job_id"], a["stage"])
                items.append({
                    "key": f"stuck:{a['id']}", "kind": "stuck", "rank": 2, "when": a["stage_changed_at"],
                    "title": f"{a['candidate_name']} has been in {a['stage']} for {days} days",
                    "detail": label(a["job_id"], a["job_title"]), "candidate_id": a["candidate_id"],
                    "job_id": a["job_id"], "stage": a["stage"], "next_stage": nxt, "days": days,
                    "href": f"/candidates/{a['candidate_id']}",
                })

    if on("flag_thin"):
        default_min = _int(cfg, "thin_min", 5)
        thin = []
        for j in jobs.values():
            minimum = thin_target(j, default_min)
            filled = j["hired_count"] >= (j.get("openings") or 1)
            if j["status"] != "open" or j["active_count"] >= minimum or filled or _offer_like(j):
                continue
            if f"thin:{j['id']}" in snoozed:
                hidden += 1
            else:
                thin.append({**j, "aim_min": minimum, "name": _job_name(j, hints.get(j["id"]))})

        def says(j: dict[str, Any]) -> str:
            n, m = j["active_count"], j["aim_min"]
            return (f"{j['name']} has {'no' if n == 0 else n} active candidate{'' if n == 1 else 's'}. "
                    f"Aim for at least {m}.")

        if len(thin) == 1:
            j = thin[0]
            items.append({
                "key": f"thin:{j['id']}", "kind": "thin", "rank": 3, "when": j["updated_at"],
                "title": says(j), "detail": "", "job_id": j["id"], "jobs": [j], "href": f"/jobs/{j['id']}",
            })
        elif thin:
            items.append({
                "key": "thin:all", "kind": "thin", "rank": 3, "when": max(j["updated_at"] for j in thin),
                "title": f"{len(thin)} jobs have thin pipelines",
                "detail": " ".join(says(j) for j in thin),
                "jobs": thin, "aim": "", "href": "/jobs?status=open",
            })

    stale_total = 0
    if on("flag_stale"):
        days = _int(cfg, "stale_days", 180)
        cutoff = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
        skip = sorted(int(k[6:]) for k in snoozed if k.startswith("stale:") and k[6:].isdigit())
        where = "updated_at < ?" + (f" AND id NOT IN ({','.join('?' * len(skip))})" if skip else "")
        stale_total = store._row(f"SELECT count(*) AS n FROM candidates WHERE {where}", (cutoff, *skip))["n"]
        if stale_total:
            oldest = [store._hydrate(r) for r in store._rows(
                f"SELECT * FROM candidates WHERE {where} ORDER BY updated_at, id LIMIT ?",
                (cutoff, *skip, STALE_NAMES))]
            if stale_total == 1:
                c = oldest[0]
                items.append({
                    "key": f"stale:{c['id']}", "kind": "stale", "rank": 4, "when": c["updated_at"],
                    "title": f"Refresh {c['name']}'s profile",
                    "detail": f"Last updated {_days_ago(c['updated_at'], now)} days ago",
                    "candidate_id": c["id"], "href": f"/candidates/{c['id']}#refresh",
                })
            else:
                names = ", ".join(c["name"] for c in oldest)
                more = stale_total - len(oldest)
                items.append({
                    "key": "stale:all", "kind": "stale", "rank": 4, "when": oldest[0]["updated_at"],
                    "title": f"{stale_total} profiles may be out of date",
                    "detail": f"Not updated in {days} days. Oldest: {names}" + (f" and {more} more." if more else "."),
                    "candidates": oldest, "href": "/candidates?sort=updated&dir=asc",
                })

    visible = []
    for i in items:
        if i["key"] in snoozed:
            hidden += 1
        else:
            visible.append(i)
    visible.sort(key=lambda i: (i["rank"], i["when"] or ""))
    counts: dict[str, int] = {}
    for i in visible:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    shown_stale = stale_total if any(i["kind"] == "stale" for i in visible) else 0
    next_week.sort(key=lambda i: i["when"] or "")
    return {"items": visible, "counts": counts, "hidden": hidden, "scheduled": scheduled,
            "scheduled_later": scheduled_later, "stale_total": shown_stale, "stale_more": 0, "next_week": next_week}


# ---------------------------------------------------------- status update
def _omit_key(job_id: int) -> str:
    return f"status_omit:{job_id}"


def internal_like(name: str | None) -> bool:
    """A stage named like Sourced, Contacted, New, Lead or Applied: people not presented to anyone yet."""
    key = _stage_key(name)
    return bool(key) and (key in INTERNAL_STAGE_WORDS or key.split()[0] in INTERNAL_STAGE_WORDS)


def default_omit(store: Store, job_id: int) -> list[str]:
    """The job's in-play stages named like internal ones, left out of status updates until the recruiter chooses."""
    return [s["name"] for s in store.job_stages(job_id) if s["outcome"] == "open" and internal_like(s["name"])]


def has_saved_omit(store: Store, job_id: int) -> bool:
    return bool(store.setting(_omit_key(job_id)))


def saved_omit(store: Store, job_id: int) -> list[str]:
    """The stages left out of this job's status update: the recruiter's choice (remembered per job), or the
    internal-looking stages (Sourced, Contacted...) when they haven't chosen."""
    import json

    raw = store.setting(_omit_key(job_id))
    if not raw:
        return default_omit(store, job_id)
    try:
        names = json.loads(raw)
    except ValueError:
        return default_omit(store, job_id)
    return [str(n) for n in names if str(n).strip()] if isinstance(names, list) else []


def save_omit(store: Store, job_id: int, names: list[str]) -> list[str]:
    """Remember which stages to leave out of this job's status updates. Empty means every stage is shown."""
    import json

    names = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    _put_setting(store, _omit_key(job_id), json.dumps(names))
    return names


def _omitted(store: Store, job_id: int, omit_stages: list[str] | None) -> set[str]:
    """Stage keys to leave out: omit_stages, or the job's remembered choice when None. Names may be partial
    ('rej'); ones the job doesn't have are ignored."""
    out = set()
    for name in saved_omit(store, job_id) if omit_stages is None else omit_stages:
        try:
            out.add(_stage_key(store.resolve_stage(job_id, name)["name"]))
        except ValueError:
            continue
    return out


def status_numbers(store: Store, job_id: int, omit_stages: list[str] | None = None) -> dict[str, Any]:
    """job_numbers for the people a status update shows (its left-out stages don't count)."""
    return job_numbers(store, job_id, _omitted(store, job_id, omit_stages))


def omitted_stages(store: Store, job_id: int, omit_stages: list[str] | None = None) -> list[str]:
    """The names of the stages a status update for this job leaves out, in stage order."""
    keys = _omitted(store, job_id, omit_stages)
    return [s["name"] for s in store.job_stages(job_id) if _stage_key(s["name"]) in keys]


def recent_changes(store: Store, job_id: int, now: datetime | None = None, days: int = 7,
                   omit: set[str] | None = None) -> list[str]:
    """Who was added, moved, hired or let go on this job in the last `days` days, one line per kind."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    rows = store._rows(
        """SELECT t.candidate_id, max(t.at) AS at, c.name, a.stage, a.created_at, s.outcome
           FROM timeline t JOIN candidates c ON c.id = t.candidate_id
           JOIN applications a ON a.candidate_id = t.candidate_id AND a.job_id = t.job_id
           LEFT JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
           WHERE t.job_id = ? AND t.kind = 'stage' AND t.at >= ?
           GROUP BY t.candidate_id ORDER BY at""", (job_id, cutoff))
    kinds: dict[str, list[str]] = {"hired": [], "out": [], "moved": [], "new": []}
    word = hire_word(store, job_id)
    for r in rows:
        if omit and _stage_key(r["stage"]) in omit:
            continue
        outcome = r["outcome"] or "open"
        if outcome == "hired":
            kinds["hired"].append(r["name"])
        elif outcome == "lost":
            kinds["out"].append(f"{r['name']} ({r['stage']})")
        elif r["created_at"] >= cutoff:
            kinds["new"].append(f"{r['name']} in {r['stage']}")
        else:
            kinds["moved"].append(f"{r['name']} to {r['stage']}")
    labels = {"hired": word.capitalize(), "out": "Out", "moved": "Moved", "new": "New"}
    return [f"- {labels[k]}: {_capped(v)}" for k, v in kinds.items() if v]


def _capped(names: list[str], limit: int = NAMES_PER_STAGE) -> str:
    """'Ana, Bo' or 'Ana, Bo, ... and 233 more'."""
    more = len(names) - limit
    return ", ".join(names[:limit]) + (f", and {more} more" if more > 0 else "")


def job_status_text(store: Store, job_id: int, now: datetime | None = None, *,
                    omit_stages: list[str] | None = None, dated: bool = True) -> str:
    """A plain-text pipeline summary to paste to a hiring manager or client. No ids, local times.

    omit_stages leaves those stages' people out (Sourced, Rejected...), from the lists and the counts.
    None means the job's remembered Leave out choice, which starts as its internal stages (saved_omit).
    The app's Copy status update and the AI's job_pipeline both use this text."""
    now = now or datetime.now(timezone.utc)
    job = store.get_job(job_id)
    groups = store.pipeline_by_stage(job_id)
    omit = _omitted(store, job_id, omit_stages)
    nums = job_numbers(store, job_id, omit, groups)
    head = job_label(job["title"], job.get("company"))
    if job.get("location"):
        head += f" · {job['location']}"
    lines = [head]
    if dated:
        lines.append(f"Status update for {local_time(now.isoformat(), '%a %b %-d, %Y')}")
    if job.get("hiring_manager"):
        lines.append(f"Hiring manager: {job['hiring_manager']}")
    if job.get("target_date"):
        lines.append(f"Target date: {local_time(job['target_date'] + 'T12:00:00', '%a %b %-d, %Y')}")
    lines += [headline(nums, job["status"]), ""]
    total = shown = 0
    for g in groups:
        total += len(g["candidates"])
        if not g["candidates"] or _stage_key(g["name"]) in omit:
            continue
        shown += len(g["candidates"])
        people = [f"{a['candidate_name']} ({_in_stage(_days_ago(a['stage_changed_at'], now))})"
                  for a in sorted(g["candidates"], key=lambda a: a["stage_changed_at"])]
        lines.append(f"{g['name']} ({len(g['candidates'])}): {_capped(people)}")
    if not shown:
        lines.append("No one to share yet." if total else "No one in the pipeline yet.")
    changes = recent_changes(store, job_id, now, omit=omit)
    lines += ["", "Changes since last week:", *changes] if changes else ["", "Changes since last week: none."]
    names = {a["candidate_id"]: a["candidate_name"] for g in groups
             if g["outcome"] == "open" and _stage_key(g["name"]) not in omit for a in g["candidates"]}
    upcoming = sorted((at, cid) for cid, at in next_interviews(store, job_id, now).items() if cid in names)
    if upcoming:
        lines += ["", "Next interviews:"]
        lines += [f"- {names[cid]}: {local_time(at, '%a %b %-d, %-I:%M %p %Z').strip()}" for at, cid in upcoming]
    return "\n".join(lines)


def match_companies(names: list[str | None], query: str | None) -> set[str]:
    """The lowercased company names a status_digest company filter means: the exact name (any case), else
    every name starting with it ('lumen' for Lumen Health), else the closest spelling ('Acme Fintek')."""
    q = re.sub(r"\s+", " ", (query or "").strip().lower())
    known = sorted({n.strip().lower() for n in names if n and n.strip()})
    if not q:
        return set()
    if q in known:
        return {q}
    starts = {n for n in known if n.startswith(q)} or {n for n in known if any(w.startswith(q) for w in n.split())}
    if starts:
        return starts
    close = difflib.get_close_matches(q, known, n=1, cutoff=0.75)
    if close:
        return set(close)
    firsts = {n.split()[0]: n for n in known}
    close = difflib.get_close_matches(q, list(firsts), n=1, cutoff=0.75)
    return {firsts[close[0]]} if close else set()


def _company_names(store: Store) -> list[str | None]:
    return [r["company"] for r in store._rows("SELECT DISTINCT company FROM jobs WHERE status IN ('open', 'on_hold')")]


def company_rollup(store: Store, company: str | None = None, omit_stages: list[str] | None = None,
                   ) -> list[dict[str, Any]]:
    """[{company, jobs, active, hired, openings, filled, word}] for open jobs, one row per company (None for no
    company). Counts cover the people each job's status update shows. company matches as in match_companies."""
    wanted = match_companies(_company_names(store), company) if company is not None else None
    rows: dict[str, dict[str, Any]] = {}
    for j in store.list_jobs("open"):
        co = j.get("company") or ""
        if wanted is not None and co.lower() not in wanted:
            continue
        nums = job_numbers(store, j["id"], _omitted(store, j["id"], omit_stages))
        r = rows.setdefault(co.lower(), {"company": co or None, "jobs": [], "active": 0, "hired": 0, "openings": 0,
                                         "filled": 0})
        r["jobs"].append(j)
        r["active"] += nums["active"]
        r["hired"] += nums["hired"]
        r["openings"] += nums["openings"]
        r["filled"] += nums["filled_seats"]
        r.setdefault("words", set()).add(nums["word"])
    for r in rows.values():
        r["word"] = "placed" if r.pop("words") == {"placed"} else "hired"  # the jobs' own word, when they agree
        r["filled_text"] = filled_text(r["filled"], r["openings"])
    return sorted(rows.values(), key=lambda r: (r["company"] is None, (r["company"] or "").lower()))


def held_jobs(store: Store, company: str | None = None) -> list[dict[str, Any]]:
    """On-hold jobs, for one company (matched as in match_companies) or all."""
    wanted = match_companies(_company_names(store), company) if company is not None else None
    return [j for j in store.list_jobs("on_hold") if wanted is None or (j.get("company") or "").lower() in wanted]


def status_digest(store: Store, company: str | None = None, now: datetime | None = None, *,
                  omit_stages: list[str] | None = None) -> str:
    """One paste-ready update for every open job (or one company's), with a roll-up per company first.

    company matches any case, by the start of the name, or by a close spelling. omit_stages applies to every
    job; when None, each job uses its own Leave out choice. On-hold jobs are listed one line each at the end."""
    now = now or datetime.now(timezone.utc)
    groups = company_rollup(store, company, omit_stages)
    held = held_jobs(store, company)
    if not groups and not held:
        return f"No open jobs{' for ' + company.strip() if company and company.strip() else ''}."
    found = sorted({g["company"] for g in groups if g["company"]} | {j["company"] for j in held if j.get("company")},
                   key=str.lower)
    who = " and ".join(found) if company and company.strip() and found else "all open jobs"
    lines = [f"Status update for {who}, {local_time(now.isoformat(), '%a %b %-d, %Y')}"]

    def summary(g: dict[str, Any]) -> str:
        n = len(g["jobs"])
        return f"{n} open job{'' if n == 1 else 's'}, {g['active']} in process, {g['filled_text']}."

    if len(groups) > 1:
        lines += [f"- {g['company'] or 'No company'}: {summary(g)}" for g in groups]
    elif groups and len(groups[0]["jobs"]) > 1:  # one company: its total, without repeating its name
        lines.append(summary(groups[0]))
    for g in groups:
        for j in g["jobs"]:
            lines += ["", job_status_text(store, j["id"], now, omit_stages=omit_stages, dated=False)]
    if held:
        lines.append("")
        lines += [f"{job_label(j['title'], j.get('company'))}: on hold." for j in held]
    return "\n".join(lines)


# ----------------------------------------------------------- new jobs
def with_out_stages(stages: list[Any] | None) -> tuple[list[Any] | None, str | None]:
    """Custom stages with no Out stage get Rejected and Withdrawn (as Out) at the end, so every job can say no.
    Returns (stages, a note saying so or None). No stages means the default set, which has them."""
    if not stages:
        return stages, None
    plan = _stage_plan(stages)
    if any(outcome == "lost" for _, outcome in plan):
        return stages, None
    have = {n.lower() for n, _ in plan}
    extra = [n for n in OUT_STAGES if n.lower() not in have]
    if not extra:
        return stages, None
    return ([*stages, *({"name": n, "outcome": "lost"} for n in extra)],
            f"Added {' and '.join(extra)} as Out stages, so you can mark people who are no longer in the running.")


def stage_set(key: str | None) -> list[str] | None:
    """The stage names of a stage set (agency, inhouse), or None for the default."""
    found = STAGE_SETS.get((key or "").strip().lower().replace("-", ""))
    return list(found[1]) if found and found[1] else None


# ----------------------------------------------------- moves and close-out
def upcoming_for(store: Store, candidate_id: int, job_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Scheduled interviews still ahead for this person on this job, or with no job when this is their only
    open pipeline. Cancelled ones and ones with an outcome don't count."""
    now = now or datetime.now(timezone.utc)
    return store._rows(
        f"""SELECT t.id, t.at, t.body, t.job_id FROM timeline t
            WHERE t.candidate_id = ? AND t.kind = 'interview' AND t.at >= ? AND {LIVE_INTERVIEW}
              AND (t.job_id = ? OR (t.job_id IS NULL AND NOT EXISTS (
                   SELECT 1 FROM applications a JOIN jobs j ON j.id = a.job_id
                   JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
                   WHERE a.candidate_id = t.candidate_id AND a.job_id <> ? AND s.outcome = 'open'
                     AND j.status = 'open')))
            ORDER BY t.at""", (candidate_id, now.replace(microsecond=0).isoformat(), job_id, job_id))


def _days_list(ivs: list[dict[str, Any]]) -> str:
    days = [local_time(i["at"], "%a %b %-d") for i in ivs]
    return days[0] if len(days) == 1 else ", ".join(days[:-1]) + " and " + days[-1]


def exit_interviews(store: Store, candidate_id: int, job_id: int) -> dict[str, Any]:
    """After a move to an Out or Hired stage: the interviews still scheduled for this person on this job.

    {} when the stage is in play or nothing is scheduled; otherwise {'candidate_id', 'name', 'stage',
    'upcoming_interviews': [{interview_id, at, at_local, summary}], 'question', 'warning'}."""
    app = store._row(
        """SELECT a.stage, s.outcome, c.name FROM applications a JOIN candidates c ON c.id = a.candidate_id
           LEFT JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
           WHERE a.candidate_id = ? AND a.job_id = ?""", (candidate_id, job_id))
    if not app or (app["outcome"] or "open") == "open":
        return {}
    ivs = upcoming_for(store, candidate_id, job_id)
    if not ivs:
        return {}
    n = len(ivs)
    what = f"an interview {_days_list(ivs)}" if n == 1 else f"{n} interviews coming up ({_days_list(ivs)})"
    it = "it" if n == 1 else "them"
    return {
        "candidate_id": candidate_id, "name": app["name"], "stage": app["stage"],
        "upcoming_interviews": [{"interview_id": i["id"], "at": i["at"],
                                 "at_local": local_time(i["at"], "%a %b %-d, %-I:%M %p %Z").strip(),
                                 "summary": i["body"]} for i in ivs],
        "question": f"{app['name']} has {what}. Cancel {it}?",
        "warning": (f"{app['name']} is now in {app['stage']} but still has {what}. Ask the recruiter whether to "
                    f"cancel {it} with update_interview (cancel), so nobody shows up to a call that was dropped."),
    }


def in_play(store: Store, job_id: int) -> list[dict[str, Any]]:
    """Everyone still in an in-play stage of this job, in stage order."""
    return [a for g in store.pipeline_by_stage(job_id) if g["outcome"] == "open" for a in g["candidates"]]


def out_stage_name(store: Store, job_id: int) -> str:
    """The stage a close-out moves people to: Rejected if the job has it as Out, else its first Out stage,
    else 'Rejected' (added when the close-out runs)."""
    lost = [s["name"] for s in store.job_stages(job_id) if s["outcome"] == "lost"]
    return next((n for n in lost if _stage_key(n) == "rejected"), lost[0] if lost else "Rejected")


def close_out_plan(store: Store, job_id: int) -> dict[str, Any] | None:
    """When a job is filled or closed and people are still in play: who, where they'd go, and how many of their
    interviews would be cancelled. None otherwise."""
    job = store.get_job(job_id)
    nums = job_numbers(store, job_id)
    if job["status"] == "on_hold" or (job["status"] == "open" and not nums["filled"]):
        return None
    people = in_play(store, job_id)
    if not people:
        return None
    ivs = sum(len(upcoming_for(store, a["candidate_id"], job_id)) for a in people)
    return {"people": people, "count": len(people), "stage": out_stage_name(store, job_id), "interviews": ivs,
            "names": _capped([a["candidate_name"] for a in people]), "closed": job["status"] == "closed"}


def close_out(store: Store, job_id: int, by: str = "app") -> dict[str, Any]:
    """Move everyone still in play to Rejected (Position filled) and cancel their upcoming interviews.
    The app asks first; the AI never runs this (close_out_hint tells it to ask the recruiter)."""
    stage = out_stage_name(store, job_id)
    if not any(s["outcome"] == "lost" for s in store.job_stages(job_id)):
        store.add_stage(job_id, stage, outcome="lost")
    moved = cancelled = 0
    for a in in_play(store, job_id):
        ivs = upcoming_for(store, a["candidate_id"], job_id)
        store.move_stage(a["candidate_id"], job_id, stage, notes=CLOSE_REASON, by=by)
        for iv in ivs:
            store.update_interview(iv["id"], cancel=True, by=by)
        moved += 1
        cancelled += len(ivs)
    return {"moved": moved, "cancelled": cancelled, "stage": stage}


def close_out_hint(store: Store, job_id: int) -> dict[str, Any]:
    """For the AI's update_job: who is still in play on a filled or closed job, and a hint to ask the recruiter
    before moving anyone. {} when there's nobody."""
    plan = close_out_plan(store, job_id)
    if not plan:
        return {}
    n = plan["count"]
    state = "closed" if plan["closed"] else "filled"
    return {"still_in_play": [{"candidate_id": a["candidate_id"], "name": a["candidate_name"], "stage": a["stage"]}
                              for a in plan["people"]],
            "close_out_hint": (
                f"{n} {'person is' if n == 1 else 'people are'} still in play on this {state} job: {plan['names']}. "
                f"Ask the recruiter whether to move {'them' if n > 1 else 'this person'} to {plan['stage']} "
                "(Position filled) and cancel their upcoming interviews. The job page in the app does it in one "
                "click. Don't move anyone unless the recruiter says so.")}


# ------------------------------------------------------------- wording
def inhouse(store: Store) -> bool:
    """The recruiter hires for their own company (in-house wording instead of agency wording)."""
    return store.setting(INHOUSE_KEY) == "1"


def set_inhouse(store: Store, on: bool) -> None:
    if on:
        _put_setting(store, INHOUSE_KEY, "1")
    else:
        _drop_setting(store, INHOUSE_KEY)


def reasons(store: Store) -> list[tuple[str, str]]:
    """The reason picker for an Out move: (label, stage-like names it duplicates, '|'-separated)."""
    passed = "Hiring team passed" if inhouse(store) else "Client passed"
    return [("Not a fit", "not a fit"), ("Withdrew", "withdrew|withdrawn"),
            ("Took another offer", "took another offer"), ("Compensation", "compensation"),
            ("No response", "no response|ghosted"),
            (passed, "client passed|hiring team passed|rejected by client|client rejected"), ("Other", "other")]


__all__ = ["FLAGS", "THRESHOLDS", "NotFound", "action_items", "add_followup", "close_out", "close_out_hint",
           "close_out_plan", "company_rollup", "complete_followup", "due_text", "exit_interviews", "headline",
           "job_label", "job_numbers", "job_status_text", "local_time", "match_companies", "next_interviews",
           "parse_day", "recent_changes", "schedule_followup", "status_digest", "twin_hints", "with_out_stages"]
