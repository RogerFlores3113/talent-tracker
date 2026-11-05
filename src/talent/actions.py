"""Today's action items, computed on read from the database.

Nothing here is stored except snoozes, AI suggestions and follow-up dates. Each
kind of item can be switched off in Settings, and its threshold changed.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any


from .store import LIVE_INTERVIEW, NotFound, Store, _stage_key, parse_when, to_dt

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
    ("thin_min", "A job needs more candidates below this many"),
    ("stale_days", "A profile may be out of date after this many days"),
)

STALE_NAMES = 3
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


# ------------------------------------------------------------------ jobs
def _offer_like(job: dict[str, Any]) -> bool:
    """Someone sits in an in-play stage named like Offer (Offer, Offer out, Verbal offer)."""
    return any(s["count"] and s["outcome"] == "open" and "offer" in _stage_key(s["name"]).split()
               for s in job.get("stage_counts", []))


def hire_word(store: Store, job_id: int) -> str:
    """'placed' when the job's hire stage is called Placed, else 'hired'."""
    won = [s["name"] for s in store.job_stages(job_id) if s["outcome"] == "hired"]
    return "placed" if won and _stage_key(won[0]) == "placed" else "hired"


def job_numbers(store: Store, job_id: int) -> dict[str, Any]:
    """In play, hired and out counts, openings, and whether the role is filled."""
    job = store.get_job(job_id)
    groups = store.pipeline_by_stage(job_id)
    n = {o: sum(len(g["candidates"]) for g in groups if g["outcome"] == o) for o in ("open", "hired", "lost")}
    openings = job.get("openings") or 1
    return {"active": n["open"], "hired": n["hired"], "out": n["lost"], "openings": openings,
            "filled": n["hired"] >= openings, "word": hire_word(store, job_id)}


def headline(nums: dict[str, Any], status: str = "open") -> str:
    """'2 candidates in process, none placed yet. 1 out.' or '1 candidate in process, 1 of 2 hired.'"""
    a, h, word = nums["active"], nums["hired"], nums["word"]
    parts = [f"{a} candidate{'' if a == 1 else 's'} in process" if a else "No one in process"]
    if nums["openings"] > 1:
        parts.append(f"{h} of {nums['openings']} {word}")
    else:
        parts.append(f"{h} {word}" if h else f"none {word} yet")
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
    """{'items': [...], 'counts': {...}, 'hidden': n, 'scheduled': n, 'stale_total': n, 'next_week': [...]}.

    Items are ordered most urgent first: interviews, then follow-ups, then the rest.
    Interviews 8 to 14 days out are listed apart, under next_week, and don't count as things to do.
    Jobs that need more candidates fold into one row, and so do out-of-date profiles.
    """
    now = now or datetime.now(timezone.utc)
    cfg = store.settings()
    on = lambda k: cfg.get(k, "1") == "1"  # noqa: E731
    snoozed = store.snoozed_keys()
    jobs = {j["id"]: j for j in store.list_jobs()}
    items: list[dict[str, Any]] = []
    hidden = scheduled = 0

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
        for s in store.open_suggestions():
            due = s.get("due_on") or ""
            if due > today:
                scheduled += 1
                continue
            mine = (s.get("source") or "") != "AI app"
            detail = [s.get("note"), label(s.get("job_id"), s.get("job_title"))]
            if not mine:
                detail.append("Suggested by your AI app")
            items.append({
                "key": f"suggestion:{s['id']}", "kind": "suggestion", "rank": 1,
                "when": (due + "T00:00:00+00:00") if due else s["created_at"],
                "title": s["text"], "detail": " · ".join(x for x in detail if x),
                "note": s.get("note"), "due_on": due or None, "overdue": bool(due and due < today),
                "due_text": due_text(due, today),
                "candidate_id": s.get("candidate_id"), "job_id": s.get("job_id"), "suggestion_id": s["id"],
                "href": f"/candidates/{s['candidate_id']}" if s.get("candidate_id")
                else f"/jobs/{s['job_id']}" if s.get("job_id") else "/",
            })

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
            minimum = j.get("min_active") or default_min
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
    return {"items": visible, "counts": counts, "hidden": hidden, "scheduled": scheduled,
            "stale_total": shown_stale, "stale_more": 0, "next_week": next_week}


# ---------------------------------------------------------- status update
def _omitted(store: Store, job_id: int, omit_stages: list[str] | None) -> set[str]:
    """Stage keys to leave out. Names may be partial ('rej'); ones the job doesn't have are ignored."""
    out = set()
    for name in omit_stages or []:
        try:
            out.add(_stage_key(store.resolve_stage(job_id, name)["name"]))
        except ValueError:
            continue
    return out


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
    return [f"- {labels[k]}: {', '.join(v)}" for k, v in kinds.items() if v]


def job_status_text(store: Store, job_id: int, now: datetime | None = None, *,
                    omit_stages: list[str] | None = None, dated: bool = True) -> str:
    """A plain-text pipeline summary to paste to a hiring manager or client. No ids, local times.

    omit_stages leaves those stages' people out of the lists (Sourced, Rejected...); the counts stay.
    The app's Copy status update and the AI's job_pipeline both use this text."""
    now = now or datetime.now(timezone.utc)
    job = store.get_job(job_id)
    groups = store.pipeline_by_stage(job_id)
    omit = _omitted(store, job_id, omit_stages)
    nums = job_numbers(store, job_id)
    head = job_label(job["title"], job.get("company"))
    if job.get("location"):
        head += f" ({job['location']})"
    lines = [head]
    if dated:
        lines.append(f"Status update for {local_time(now.isoformat(), '%a %b %-d, %Y')}")
    if job.get("hiring_manager"):
        lines.append(f"Hiring manager: {job['hiring_manager']}")
    if job.get("target_date"):
        lines.append(f"Target date: {local_time(job['target_date'] + 'T12:00:00', '%a %b %-d, %Y')}")
    lines += [headline(nums, job["status"]), ""]
    total = 0
    for g in groups:
        total += len(g["candidates"])
        if not g["candidates"] or _stage_key(g["name"]) in omit:
            continue
        people = ", ".join(f"{a['candidate_name']} ({_in_stage(_days_ago(a['stage_changed_at'], now))})"
                           for a in sorted(g["candidates"], key=lambda a: a["stage_changed_at"]))
        lines.append(f"{g['name']} ({len(g['candidates'])}): {people}")
    if not total:
        lines.append("No one in the pipeline yet.")
    changes = recent_changes(store, job_id, now, omit=omit)
    lines += ["", "Changes since last week:", *changes] if changes else ["", "Changes since last week: none."]
    names = {a["candidate_id"]: a["candidate_name"] for g in groups
             if g["outcome"] == "open" and _stage_key(g["name"]) not in omit for a in g["candidates"]}
    upcoming = sorted((at, cid) for cid, at in next_interviews(store, job_id, now).items() if cid in names)
    if upcoming:
        lines += ["", "Next interviews:"]
        lines += [f"- {names[cid]}: {local_time(at, '%a %b %-d, %-I:%M %p %Z').strip()}" for at, cid in upcoming]
    return "\n".join(lines)


def company_rollup(store: Store, company: str | None = None) -> list[dict[str, Any]]:
    """[{company, jobs, active, hired, openings}] for open jobs, one row per company (None for no company)."""
    rows: dict[str, dict[str, Any]] = {}
    for j in store.list_jobs("open"):
        co = j.get("company") or ""
        if company is not None and co.lower() != company.strip().lower():
            continue
        r = rows.setdefault(co.lower(), {"company": co or None, "jobs": [], "active": 0, "hired": 0, "openings": 0})
        r["jobs"].append(j)
        r["active"] += j["active_count"]
        r["hired"] += j["hired_count"]
        r["openings"] += j.get("openings") or 1
    return sorted(rows.values(), key=lambda r: (r["company"] is None, (r["company"] or "").lower()))


def status_digest(store: Store, company: str | None = None, now: datetime | None = None, *,
                  omit_stages: list[str] | None = None) -> str:
    """One paste-ready update for every open job (or one company's), with a roll-up per company first."""
    now = now or datetime.now(timezone.utc)
    groups = company_rollup(store, company)
    if not groups:
        return f"No open jobs{' for ' + company.strip() if company and company.strip() else ''}."
    who = groups[0]["company"] if company and groups[0]["company"] else "all open jobs"
    lines = [f"Status update for {who}, {local_time(now.isoformat(), '%a %b %-d, %Y')}"]
    for g in groups:
        n = len(g["jobs"])
        lines.append(f"- {g['company'] or 'No company'}: {n} open job{'' if n == 1 else 's'}, "
                     f"{g['active']} in process, {g['hired']} of {g['openings']} hired.")
    for g in groups:
        for j in g["jobs"]:
            lines += ["", job_status_text(store, j["id"], now, omit_stages=omit_stages, dated=False)]
    return "\n".join(lines)


__all__ = ["FLAGS", "THRESHOLDS", "NotFound", "action_items", "add_followup", "company_rollup", "complete_followup",
           "due_text", "headline", "job_label", "job_numbers", "job_status_text", "local_time", "next_interviews",
           "parse_day", "recent_changes", "schedule_followup", "status_digest", "twin_hints"]
