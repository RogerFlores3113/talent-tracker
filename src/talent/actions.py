"""Today's action items, computed on read from the database.

Nothing here is stored except snoozes, AI suggestions and follow-up dates. Each
kind of item can be switched off in Settings, and its threshold changed.
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable


from .store import LIVE_INTERVIEW, NotFound, Store, _stage_key, _stage_plan, parse_when, to_dt
from .store import interview_label as result_label, interview_result, interview_verdict

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
# Applied is not here: applicants are real pipeline the client expects to hear about.
INTERNAL_STAGE_WORDS = frozenset({"sourced", "contacted", "new", "lead", "leads", "prospect", "prospects"})
# A stage named like this is where people are first shown to the client; in-play stages before it are internal.
SUBMIT_STAGE_WORDS = frozenset({"submitted", "submittal", "submittals", "presented", "shortlist", "shortlisted"})
# A stage named like this is past the client's door (an interview or an offer), wherever it sits in the list.
LATE_STAGE_WORDS = frozenset({"offer", "interview", "interviews", "interviewing", "interviewed", "onsite", "panel",
                              "final"})
OUT_STAGES = ("Rejected", "Withdrawn")
CLOSE_REASON = "Reason: Position filled"
INHOUSE_KEY = "inhouse_wording"
HIRING_FOR_ANSWERED_KEY = "hiring_for_answered"  # set once they've said own company or clients
STAGE_SETS: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "default": ("Default", None),
    "agency": ("Agency", ("Sourced", "Screened", "Submitted", "Client interview", "Offer", "Placed", "Client passed",
                          "Withdrawn")),
    "inhouse": ("In-house", ("Applied", "Recruiter screen", "Hiring manager screen", "Interviews", "Offer", "Hired",
                             "Rejected", "Withdrawn")),
}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def local_days(then: datetime, now: datetime) -> int:
    """Calendar days from `then` to `now` on this computer's clock: 11 PM yesterday is 1 day ago at 1 AM."""
    return (now.astimezone().date() - then.astimezone().date()).days


def _days_ago(iso: str | None, now: datetime) -> int:
    dt = to_dt(iso)
    return local_days(dt, now) if dt else 0


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


def day_text(d: Any, fmt: str = "%a %b %-d") -> str:
    """strftime that also takes %-d on Windows (which spells it %#d). For dates and datetimes not in local time."""
    try:
        return d.strftime(fmt)
    except ValueError:  # Windows strftime has no %-d
        return d.strftime(fmt.replace("%-", "%#"))


def _in_stage(days: int, new: str = "new today") -> str:
    """How long someone has been in a stage: 'new today' (so it doesn't read as the day they applied), '3 days'."""
    return new if days <= 0 else "1 day" if days == 1 else f"{days} days"


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


IV_TIME = "%a %b %-d, %-I:%M %p %Z"  # interview times say their zone: 'Fri Nov 6, 10:00 PM CST'


def _iv_data(t: dict[str, Any]) -> dict[str, Any]:
    d = t.get("data")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = None
    return d if isinstance(d, dict) else {}


def interview_name(t: dict[str, Any], limit: int = 60) -> str:
    """What an interview is, from its summary's first line ('Onsite: system design'), or ''."""
    line = ((t.get("body") or "").strip().splitlines() or [""])[0].strip()
    return line if len(line) <= limit else line[:limit - 3].rstrip() + "..."


def interviewer_of(t: dict[str, Any]) -> str:
    return str(_iv_data(t).get("interviewer") or "").strip()


def interview_label(t: dict[str, Any]) -> str:
    """'Onsite: system design, with Priya Raman', 'with Priya Raman', or ''."""
    who = interviewer_of(t)
    return ", ".join(x for x in (interview_name(t), f"with {who}" if who else "") if x)


def _local_day(iso: str) -> date:
    return to_dt(iso).astimezone().date()


def loop_day(t: dict[str, Any]) -> date:
    """The day an interview was first booked for (a session moved to Thursday still belongs to Tuesday's
    onsite), else its own day."""
    first = next((x for x in _iv_data(t).get("rescheduled_from") or [] if isinstance(x, str) and x), None)
    try:
        return _local_day(first) if first else _local_day(t["at"])
    except ValueError:
        return _local_day(t["at"])


def group_loops(rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any] | None = None
                ) -> list[list[dict[str, Any]]]:
    """Interviews grouped into loops: one person's sessions for one job on one day, where a session moved to
    another day stays with the loop it was booked in. `key` says what must match besides the day (default:
    candidate and job). Groups come in the order of their first row, and keep the rows' order."""
    key = key or (lambda r: (r["candidate_id"], r.get("job_id")))
    parent: dict[Any, Any] = {}

    def find(k: Any) -> Any:
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    marks = []
    for r in rows:
        base = key(r)
        a, b = (base, _local_day(r["at"])), (base, loop_day(r))
        parent[find(a)] = find(b)
        marks.append(a)
    groups: dict[Any, list[dict[str, Any]]] = {}
    for r, a in zip(rows, marks):
        groups.setdefault(find(a), []).append(r)
    return list(groups.values())


def _session(t: dict[str, Any], now_iso: str) -> dict[str, Any]:
    """One session of a loop, as a Today card lists it."""
    status = t.get("status") or ("cancelled" if _iv_data(t).get("status") == "cancelled"
                                 else interview_result(_iv_data(t)) or "scheduled")
    return {"interview_id": t["id"], "at": t["at"], "name": interview_name(t) or "Interview",
            "interviewer": interviewer_of(t) or None, "status": status, "result": result_label(_iv_data(t)),
            "started": t["at"] <= now_iso,
            "href": f"/candidates/{t['candidate_id']}?iv={t['id']}#iv-{t['id']}"}


def _loop_key(kind: str, g: list[dict[str, Any]]) -> str:
    first = g[0]
    return f"{kind}:{first['candidate_id']}:{first.get('job_id') or 0}:{loop_day(first).isoformat()}"


OUTCOME_ASK_DAYS = 30  # how far back Today asks how an interview with no outcome went


def _outcome_asks(store: Store, now: datetime, jobs: dict[int, dict[str, Any]], asked: set[int],
                  label: Callable[..., str]) -> list[dict[str, Any]]:
    """'How did Ben Okafor's interview on Tue Nov 3 go?': interviews more than two hours past, in the last 30 days,
    still scheduled with no outcome, for people still in that job. One item per interview, with outcome buttons.
    An onsite loop is one item ("How did Jordan's interviews on Tue Nov 10 go?") once its last session is over,
    with each session and its outcome buttons inside."""
    end = (now - timedelta(hours=2)).replace(microsecond=0).isoformat()
    start = (now - timedelta(days=OUTCOME_ASK_DAYS)).replace(microsecond=0).isoformat()
    rows = store._rows(
        f"""SELECT t.*, c.name AS candidate_name, j.title AS job_title FROM timeline t
           JOIN candidates c ON c.id = t.candidate_id LEFT JOIN jobs j ON j.id = t.job_id
           WHERE t.kind = 'interview' AND t.at >= ? AND t.at < ? AND {LIVE_INTERVIEW} ORDER BY t.at""", (start, end))
    rows = _live_interviews(store, [r for r in rows if r["id"] not in asked], jobs)
    # The rest of each loop: sessions already recorded, and ones still to come (ask once the last one is over).
    later = (now + timedelta(days=1)).replace(microsecond=0).isoformat()
    pairs = {(r["candidate_id"], r.get("job_id")) for r in rows}
    near = [r for r in store._rows(
        """SELECT t.*, c.name AS candidate_name, j.title AS job_title FROM timeline t
           JOIN candidates c ON c.id = t.candidate_id LEFT JOIN jobs j ON j.id = t.job_id
           WHERE t.kind = 'interview' AND t.at >= ? AND t.at < ?
             AND coalesce(json_extract(t.data, '$.status'), 'scheduled') <> 'cancelled' ORDER BY t.at, t.id""",
        (start, later)) if (r["candidate_id"], r.get("job_id")) in pairs] if pairs else []
    pending = {r["id"] for r in rows}
    out = []
    stamp = now.replace(microsecond=0).isoformat()
    for g in group_loops(near):
        todo = [t for t in g if t["id"] in pending]
        if not todo:
            continue
        if len(g) > 1:
            if any(t["id"] not in pending and not _iv_data(t).get("outcome") for t in g):
                continue  # a session of this loop is still ahead: ask after the last one
            first = g[0]
            out.append({
                "key": _loop_key("interview_outcome_loop", g), "kind": "interview_outcome", "rank": 1,
                "when": first["at"], "loop": True,
                "title": f"How did {first['candidate_name']}'s interviews on {local_time(first['at'], '%a %b %-d')} go?",
                "detail": " · ".join(x for x in (label(first.get("job_id"), first.get("job_title")),
                                                 f"{len(g)} sessions") if x),
                "interview_id": todo[0]["id"], "interview_ids": [t["id"] for t in todo],
                "sessions": [{**_session(t, stamp), "status": "scheduled" if t["id"] in pending else
                              _iv_data(t).get("outcome") or "scheduled"} for t in g],
                "candidate_id": first["candidate_id"], "job_id": first.get("job_id"),
                "href": f"/candidates/{first['candidate_id']}#timeline",
            })
            continue
        t = todo[0]
        who = interviewer_of(t)
        bits = [label(t.get("job_id"), t.get("job_title")), interview_name(t), f"with {who}" if who else None]
        out.append({
            "key": f"interview_outcome:{t['id']}", "kind": "interview_outcome", "rank": 1, "when": t["at"],
            "title": f"How did {t['candidate_name']}'s interview on {local_time(t['at'], '%a %b %-d')} go?",
            "detail": " · ".join(x for x in bits if x), "interview_id": t["id"], "interviewer": who or None,
            "candidate_id": t["candidate_id"], "job_id": t.get("job_id"),
            "href": f"/candidates/{t['candidate_id']}?iv={t['id']}#iv-{t['id']}",
        })
    return out


RESULT_DAYS = 14  # how long a recorded outcome offers its next step on Today
RESULT_WORDS = {"passed": "passed", "not_passed": "did not pass", "no_show": "did not show for",
                "mixed": "got mixed feedback on"}


def pass_stage(store: Store, job_id: int) -> tuple[str, str | None]:
    """Where someone goes after a Not passed: (stage, reason). A Client passed or Hiring team passed stage when
    the job has one (no reason needed), else its Rejected or first Out stage with that reason."""
    word = "Hiring team passed" if inhouse(store) else "Client passed"
    lost = [s["name"] for s in store.job_stages(job_id) if s["outcome"] == "lost"]
    named = next((n for n in lost if _stage_key(n) in _CLIENT_EXITS or _stage_key(n) == "hiring team passed"), None)
    return (named, None) if named else (out_stage_name(store, job_id), f"Reason: {word}")


def interview_results(store: Store, job_id: int | None = None, now: datetime | None = None,
                      days: int = RESULT_DAYS) -> list[dict[str, Any]]:
    """How recent interviews went, one row per interview or onsite loop with an outcome recorded in the last
    `days` days, oldest first: {candidate_id, name, job_id, job_title, sessions, counts, pending, verdict,
    outcome_at, day, what, line}. verdict is not_passed when any session was not passed, pending while a session
    of the loop has no outcome yet, else passed (or no_show when nobody showed). `line` is the status update's
    wording: '- Mei Lin Zhao passed Tech screen (Tue Nov 3)'. Reports can list these as Interview results."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    early = (now - timedelta(days=days + OUTCOME_ASK_DAYS)).replace(microsecond=0).isoformat()
    late = (now + timedelta(days=1)).replace(microsecond=0).isoformat()
    where = "AND t.job_id = ?" if job_id is not None else "AND t.job_id IS NOT NULL"
    rows = store._rows(
        f"""SELECT t.*, c.name AS candidate_name, j.title AS job_title, j.company AS job_company FROM timeline t
            JOIN candidates c ON c.id = t.candidate_id JOIN jobs j ON j.id = t.job_id
            WHERE t.kind = 'interview' AND t.at >= ? AND t.at < ? {where}
              AND coalesce(json_extract(t.data, '$.status'), 'scheduled') <> 'cancelled' ORDER BY t.at, t.id""",
        (early, late, *([job_id] if job_id is not None else [])))
    out = []
    for g in group_loops(rows):
        done = [t for t in g if _iv_data(t).get("outcome")]
        if not done or max(str(_iv_data(t).get("outcome_at") or "") for t in done) < cutoff:
            continue
        counts: dict[str, int] = {}
        for t in done:
            o = interview_result(_iv_data(t)) or "passed"
            counts[o] = counts.get(o, 0) + 1
        pending = len(g) - len(done)
        verdict = ("not_passed" if counts.get("not_passed") else "pending" if pending
                   else "mixed" if counts.get("mixed") else "passed" if counts.get("passed") else "no_show")
        first = g[0]
        day = local_time(first["at"], "%a %b %-d")
        name = first["candidate_name"]
        if len(g) == 1:
            what = interview_name(first) or "the interview"
            line = f"- {name} {RESULT_WORDS[verdict]} {what} ({day})"
        else:
            what = f"the interviews on {day}"
            tally = ", ".join(f"{n} {INTERVIEW_WORDS_LOWER[o]}" for o, n in counts.items())
            line = (f"- {name}: {len(done)} of {len(g)} interviews on {day} recorded so far ({tally})" if pending
                    else f"- {name} {RESULT_WORDS[verdict]} {what} ({tally})")
        out.append({"candidate_id": first["candidate_id"], "name": name, "job_id": first["job_id"],
                    "job_title": first["job_title"], "job_company": first.get("job_company"),
                    "sessions": [_session(t, now.replace(microsecond=0).isoformat()) for t in g], "counts": counts,
                    "pending": pending, "verdict": verdict, "day": day, "what": what, "line": line,
                    "label": (result_label(_iv_data(first)) if len(g) == 1 and interview_verdict(_iv_data(first))
                              else None),  # the interviewer's word: Strong yes
                    "outcome_at": max(str(_iv_data(t).get("outcome_at") or "") for t in done)})
    return out


INTERVIEW_WORDS_LOWER = {"passed": "passed", "not_passed": "not passed", "no_show": "no show", "mixed": "mixed"}


def _result_items(store: Store, now: datetime, jobs: dict[int, dict[str, Any]],
                  label: Callable[..., str]) -> list[dict[str, Any]]:
    """After an outcome: 'Tomas Weber did not pass HM screen' with Move to Rejected, or 'Mei Lin passed Tech
    screen' with Move to the next stage. Only while they're still in that stage of an open job, with no
    interview booked there since."""
    results = [r for r in interview_results(store, now=now) if r["verdict"] in ("passed", "not_passed", "mixed")]
    if not results:
        return []
    apps = {(a["candidate_id"], a["job_id"]): a for a in store.open_applications()}
    stamp = now.replace(microsecond=0).isoformat()
    out = []
    for r in results:
        app = apps.get((r["candidate_id"], r["job_id"]))
        if not app or (app["stage_changed_at"] or "") > r["outcome_at"]:
            continue
        if upcoming_for(store, r["candidate_id"], r["job_id"], now):
            continue
        if r["verdict"] == "passed":
            to, notes = store.next_stage(r["job_id"], app["stage"]), None
            if not to:
                continue
        elif r["verdict"] == "mixed":  # neither on nor out: the recruiter weighs it on Decide
            to, notes = None, None
        else:
            to, notes = pass_stage(store, r["job_id"])
        out.append({
            "key": f"interview_result:{r['candidate_id']}:{r['job_id']}:{r['outcome_at']}", "kind": "interview_result",
            "rank": 1, "when": r["outcome_at"], "verdict": r["verdict"],
            "title": f"{r['name']} {RESULT_WORDS[r['verdict']]} {r['what']}" + (
                f" ({r['label']})" if r.get("label") and r["verdict"] != "mixed" else ""),
            "detail": " · ".join(x for x in (label(r["job_id"], r["job_title"]), f"now in {app['stage']}") if x),
            "candidate_id": r["candidate_id"], "job_id": r["job_id"], "stage": app["stage"], "next_stage": to,
            "notes": notes, "href": (f"/jobs/{r['job_id']}/decide" if r["verdict"] == "mixed"
                                     else f"/candidates/{r['candidate_id']}#timeline"),
        })
    return out


def exit_prompts(store: Store, now: datetime | None = None, days: int = 90) -> list[dict[str, Any]]:
    """Interviews still scheduled after their candidate left the job (an Out stage), was hired, or the job
    closed, grouped per person and job: [{candidate_id, name, job_id, job, stage, why, interviews: [...]}].
    Ones the recruiter chose to keep (since that move) are left out."""
    now = now or datetime.now(timezone.utc)
    rows = [r for r in store.upcoming_interviews(days=days, now=now) if r.get("job_id")]
    if not rows:
        return []
    jobs = {j["id"]: j for j in store.list_jobs()}
    apps = {(a["candidate_id"], a["job_id"]): a for a in store._rows(
        """SELECT a.candidate_id, a.job_id, a.stage, a.stage_changed_at, s.outcome FROM applications a
           LEFT JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
           WHERE a.candidate_id IN ({})""".format(",".join("?" * len({r["candidate_id"] for r in rows}))),
        sorted({r["candidate_id"] for r in rows}))}
    groups: dict[tuple[int, int], dict[str, Any]] = {}
    for r in rows:
        job = jobs.get(r["job_id"])
        app = apps.get((r["candidate_id"], r["job_id"]))
        outcome = (app or {}).get("outcome") or "open"
        closed = bool(job and job["status"] == "closed")
        if not job or (outcome == "open" and not closed):
            continue
        kept = str(_iv_data(r).get("kept_at") or "")
        since = (app or {}).get("stage_changed_at") or "" if outcome != "open" else job.get("updated_at") or ""
        if kept and kept >= since:
            continue
        why = "hired" if outcome == "hired" else "out" if outcome == "lost" else "closed"
        g = groups.setdefault((r["candidate_id"], r["job_id"]), {
            "candidate_id": r["candidate_id"], "name": r["candidate_name"], "job_id": r["job_id"], "job": job,
            "stage": (app or {}).get("stage"), "why": why, "interviews": []})
        g["interviews"].append(r)
    return sorted(groups.values(), key=lambda g: g["interviews"][0]["at"])


def _exit_title(store: Store, g: dict[str, Any]) -> str:
    """'Dana Okafor is out of Backend Engineer but still has an interview Fri Nov 6'."""
    ivs = g["interviews"]
    days = list(dict.fromkeys(local_time(i["at"], "%a %b %-d") for i in ivs))
    when = days[0] if len(days) == 1 else ", ".join(days[:-1]) + " and " + days[-1]
    what = f"an interview {when}" if len(ivs) == 1 else f"{len(ivs)} interviews ({when})"
    title = g["job"]["title"]
    if g["why"] == "closed":
        return f"{title} is closed but {g['name']} still has {what}"
    if g["why"] == "hired":
        return f"{g['name']} was {hire_word(store, g['job_id'])} for {title} but still has {what}"
    return f"{g['name']} is out of {title} but still has {what}"


def next_interview_days(store: Store, job_id: int, now: datetime | None = None) -> dict[int, list[dict[str, Any]]]:
    """{candidate_id: [interviews]}: everything scheduled on the day of each in-the-running candidate's next
    interview for this job (or with no job), so an onsite loop shows as its sessions, in time order."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).replace(microsecond=0).isoformat()
    rows = store._rows(
        f"""SELECT t.id, t.candidate_id, t.at, t.body, t.data FROM timeline t
           JOIN applications a ON a.candidate_id = t.candidate_id AND a.job_id = ?
           JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
           WHERE t.kind = 'interview' AND t.at >= ? AND (t.job_id = ? OR t.job_id IS NULL)
             AND s.outcome = 'open' AND {LIVE_INTERVIEW}
           ORDER BY t.at, t.id""", (job_id, start, job_id))
    out: dict[int, list[dict[str, Any]]] = {}
    for g in group_loops(rows, key=lambda r: r["candidate_id"]):  # a session moved to a later day stays in
        out.setdefault(g[0]["candidate_id"], g)
    return out


def interview_line(name: str, ivs: list[dict[str, Any]]) -> str:
    """'- Tomasz: Fri Nov 6, 4:30 PM CST (Onsite: system design, with Priya)', or for a loop
    '- Tomasz: Fri Nov 6, 3 interviews: 9:00 AM CST System design (with Priya); 10:00 AM CST Coding'."""
    if len(ivs) == 1:
        label = interview_label(ivs[0])
        return f"- {name}: {local_time(ivs[0]['at'], IV_TIME).strip()}" + (f" ({label})" if label else "")
    parts = []
    days = {local_time(i["at"], "%a %b %-d") for i in ivs}
    for i in ivs:
        bits = [interview_name(i) or "Interview", *([f"(with {interviewer_of(i)})"] if interviewer_of(i) else [])]
        day = f"{local_time(i['at'], '%a %b %-d')}, " if len(days) > 1 else ""
        parts.append(f"{day}{local_time(i['at'], '%-I:%M %p %Z').strip()} {' '.join(bits)}")
    if len(days) > 1:  # a session moved to another day is still part of the loop
        return f"- {name}: {len(ivs)} interviews: " + "; ".join(parts)
    return f"- {name}: {local_time(ivs[0]['at'], '%a %b %-d')}, {len(ivs)} interviews: " + "; ".join(parts)


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


def ai_clear_cap(store: Store) -> list[dict[str, Any]]:
    """A Today item while the AI app has hit its hourly limit on clearing saved values across everyone, so
    the recruiter looks at what it emptied (and more clears wait until the hour has passed)."""
    from .store import AI_CLEAR_CAP

    got = store.ai_clears_this_hour()
    if got["count"] < AI_CLEAR_CAP:
        return []
    people = f"{got['people']} {'person' if got['people'] == 1 else 'people'}"
    return [{"key": f"aiclears:{got['last']}", "kind": "ai_change", "rank": 0, "when": got["last"],
             "title": f"Your AI app cleared {got['count']} saved values on {people} in the last hour. Review or undo.",
             "detail": "It can't clear more until the hour has passed.", "href": "/changes?who=ai&since=1"}]


def ai_overwrite_alert(store: Store) -> list[dict[str, Any]]:
    """A Today item once the AI app has replaced or cleared many saved values in the last hour, across everyone
    (AI_OVERWRITE_ALERT), so a run of injected edits doesn't sit unseen on Changes."""
    from .store import AI_OVERWRITE_ALERT, AI_OVERWRITE_CAP

    got = store.ai_overwrites_this_hour()
    if got["count"] < AI_OVERWRITE_ALERT:
        return []
    where = ([f"{got['people']} {'person' if got['people'] == 1 else 'people'}"] if got["people"] else []) + (
        [f"{got['jobs']} job{'' if got['jobs'] == 1 else 's'}"] if got["jobs"] else [])
    people = " and ".join(where) or "your records"
    left = AI_OVERWRITE_CAP - got["count"]
    return [{"key": f"aioverwrites:{got['last']}", "kind": "ai_change", "rank": 0, "when": got["last"],
             "title": f"Your AI app changed {got['count']} saved values on {people} in the last hour. "
                      "Review or undo all.",
             "detail": (f"It can change {left} more this hour." if left > 0
                        else "It can't change more until the hour has passed."),
             "href": "/changes?who=ai&since=1"}]


def _offer_name(name: str | None) -> bool:
    return "offer" in _stage_key(name).split()


def ai_pipeline_changes(store: Store, now: datetime | None = None, days: int = 7) -> list[dict[str, Any]]:
    """Today items for what the AI app did to pipelines lately: people it moved out of a job, people it
    put in a hire stage or an Offer-like one, and jobs it closed or put on hold. A move drops off once it
    was undone, or once the person is no longer where the AI put them (the recruiter moved them since)."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    out_by_job: dict[int, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    for r in store._rows(
            """SELECT a.id, a.job_id, a.at, a.confirmed_at, j.title, j.company, s.name AS stage, s.outcome,
                      c.name AS candidate_name
               FROM ai_actions a JOIN jobs j ON j.id = a.job_id
               JOIN timeline t ON t.id = a.target_id
               JOIN candidates c ON c.id = t.candidate_id
               JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(t.body)
               JOIN applications p ON p.candidate_id = t.candidate_id AND p.job_id = a.job_id
                                  AND lower(p.stage) = lower(t.body)
               WHERE a.kind IN ('stage_move', 'pipeline_add') AND a.undone_at IS NULL AND a.at >= ?
                 AND NOT EXISTS (SELECT 1 FROM timeline l WHERE l.candidate_id = t.candidate_id
                                   AND l.job_id = a.job_id AND l.kind = 'stage' AND l.id > t.id)
               ORDER BY a.id DESC""", (since,)):
        if r["outcome"] == "lost":
            item = out_by_job.setdefault(r["job_id"], {"id": r["id"], "at": r["at"], "title": r["title"], "n": 0})
            item["n"] += 1
        elif r["outcome"] == "hired":  # a hire feeds reports: the recruiter says Yes or Undo
            if r["confirmed_at"]:
                continue
            word = "placed" if _stage_key(r["stage"]) == "placed" else "hired"
            items.append({"key": f"aihire:{r['id']}", "kind": "ai_change", "rank": 1, "when": r["at"],
                          "job_id": r["job_id"], "confirm_id": r["id"],
                          "title": f"Your AI app marked {r['candidate_name']} {word} on "
                                   f"{r['title']}{' (' + r['company'] + ')' if r['company'] else ''}. Right?",
                          "detail": f"They are in {r['stage']} now. Say Yes to keep it, or Undo.",
                          "href": "/changes?who=ai&since=7"})
        elif r["outcome"] == "open" and _offer_name(r["stage"]):
            items.append({"key": f"aihire:{r['id']}", "kind": "ai_change", "rank": 1, "when": r["at"],
                          "job_id": r["job_id"],
                          "title": f"Your AI app moved {r['candidate_name']} to {r['stage']} on "
                                   f"{r['title']}{' (' + r['company'] + ')' if r['company'] else ''}. Review or undo.",
                          "detail": "Check it's right before anyone hears about it.", "href": "/changes?who=ai&since=7"})
    items = [{"key": f"aimove:{i['id']}", "kind": "ai_change", "rank": 1, "when": i["at"], "job_id": job_id,
              "title": f"Your AI app moved {i['n']} {'person' if i['n'] == 1 else 'people'} out of {i['title']}. "
                       "Review or undo.", "detail": "", "href": "/changes?who=ai&since=7"}
             for job_id, i in out_by_job.items()] + items
    words = {"closed": "closed", "on_hold": "put {} on hold"}
    for job in store._rows("SELECT id, title FROM jobs WHERE status IN ('closed', 'on_hold') AND updated_at >= ?",
                           (since,)):
        change = store.ai_status_change(job["id"])
        if change and change["at"] >= since and not change.get("confirmed_at"):
            verb = words[change["status"]]
            what = verb.format(job["title"]) if "{}" in verb else f"{verb} {job['title']}"
            items.append({"key": f"aistatus:{job['id']}:{change['at']}", "kind": "ai_change", "rank": 1,
                          "when": change["at"], "job_id": job["id"], "title": f"Your AI app {what}.",
                          "detail": "Right? Say Yes to keep it, or Undo.", "confirm_id": change.get("id"),
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
    """'2 candidates in process, none placed yet. 1 out.', '1 candidate in process, 1 hired, 2 openings.' or,
    once every opening is taken, '3 candidates in process, 1 of 1 opening filled.' / '1 of 1 opening filled.'"""
    a, h, word, o = nums["active"], nums["hired"], nums["word"], nums["openings"]
    if nums["filled"]:
        seats = filled_text(nums["filled_seats"], o)
        hires = f"{h} {word}, {seats}" if h > nums["filled_seats"] else seats
    else:
        hires = hired_text(h, o, word)
    if a:
        text = f"{a} candidate{'' if a == 1 else 's'} in process, {hires}."
    else:
        text = f"{hires}." if nums["filled"] else f"No one in process, {hires}."
    if nums["out"]:
        text += f" {nums['out']} out."
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
        prompts = exit_prompts(store, now)
        asked = {i["id"] for g in prompts for i in g["interviews"]}
        for g in prompts:  # still on someone's calendar: ask, whatever the day, so nobody joins a dropped call
            ivs = g["interviews"]
            bits = [label(g["job_id"]), *(interview_label(i) for i in ivs[:3])]
            items.append({
                "key": f"interview_out:{g['candidate_id']}:{g['job_id']}", "kind": "interview_out", "rank": 0,
                "when": ivs[0]["at"], "title": _exit_title(store, g), "detail": " · ".join(x for x in bits if x),
                "candidate_id": g["candidate_id"], "job_id": g["job_id"], "interview_id": ivs[0]["id"],
                "interview_ids": [i["id"] for i in ivs], "stage": g["stage"],
                "href": f"/candidates/{g['candidate_id']}#timeline",
            })
        live = [t for t in store.upcoming_interviews(days=14, now=now) if t["id"] not in asked]
        kept = [t for t in live if _iv_data(t).get("kept_at")]  # kept after a move out: a real interview
        shown = {t["id"] for t in _live_interviews(store, live, jobs)} | {t["id"] for t in kept}
        stamp = now.replace(microsecond=0).isoformat()
        for g in group_loops([t for t in live if t["id"] in shown]):
            t = g[0]
            job = jobs.get(t.get("job_id")) if t.get("job_id") else None
            who = interviewer_of(t)
            if len(g) > 1:  # an onsite loop: one card, its sessions inside
                held = bool(job and job["status"] == "on_hold")
                row = {
                    "key": _loop_key("interview_loop", g), "kind": "interview", "rank": 0, "when": t["at"],
                    "loop": True, "title": f"Interview loop with {t['candidate_name']}: {len(g)} sessions",
                    "interview_id": t["id"], "interview_ids": [i["id"] for i in g],
                    "sessions": [_session(i, stamp) for i in g],
                    "detail": " · ".join(x for x in (label(t.get("job_id"), t.get("job_title")),
                                                     "job on hold" if held else None) if x),
                    "on_hold": held, "candidate_id": t["candidate_id"], "job_id": t.get("job_id"),
                    "interviewer": None, "started": t["at"] <= stamp,
                    "href": f"/candidates/{t['candidate_id']}#timeline",
                }
                if t["at"] <= week_end:
                    items.append(row)
                elif row["key"] in snoozed:
                    hidden += 1
                else:
                    next_week.append(row)
                continue
            bits = [label(t.get("job_id"), t.get("job_title")), t.get("body"), f"with {who}" if who else None,
                    "job on hold" if job and job["status"] == "on_hold" else None]
            row = {
                "key": f"interview:{t['id']}", "kind": "interview", "rank": 0, "when": t["at"],
                "title": f"Interview with {t['candidate_name']}", "interview_id": t["id"],
                "detail": " · ".join(x for x in bits if x), "on_hold": bool(job and job["status"] == "on_hold"),
                "candidate_id": t["candidate_id"], "job_id": t.get("job_id"), "interviewer": who or None,
                "started": t["at"] <= now.replace(microsecond=0).isoformat(),
                "href": f"/candidates/{t['candidate_id']}?iv={t['id']}#iv-{t['id']}",
            }
            if t["at"] > week_end:
                if row["key"] in snoozed:
                    hidden += 1
                else:
                    next_week.append(row)
            else:
                items.append(row)
        items += _outcome_asks(store, now, jobs, asked, label)
        items += _result_items(store, now, jobs, label)

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

    items += ai_identity_changes(store, now) + ai_pipeline_changes(store, now) + ai_clear_cap(store)
    items += ai_overwrite_alert(store)
    from .review import today_items as review_items
    items += review_items(store)
    from .shortlist import today_items as shortlist_items  # here: shortlist imports this module
    items += shortlist_items(store)
    from .reports import today_items as update_items  # here: reports imports this module
    items += update_items(store, now)  # client (or hiring manager) updates due today or earlier this week
    stuck_more = 0
    if on("flag_stuck"):
        rows, n_hidden, stuck_more = _stuck_items(store, now, _int(cfg, "stuck_days", 7), snoozed, label)
        items += rows
        hidden += n_hidden
    openings = _openings_item(store)
    if openings:
        items.append(openings)

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
            "scheduled_later": scheduled_later, "stale_total": shown_stale, "stale_more": 0, "next_week": next_week,
            "stuck_more": stuck_more}


STUCK_GROUP = 3  # a job with more people stuck than this shows them as one row
STUCK_CAP = 50  # stuck rows on Today, then "and N more"
STUCK_SNOOZE_DAYS = 30  # "Snooze all from this import"


def _stuck_items(store: Store, now: datetime, limit: int, snoozed: set[str],
                 label: Callable[..., str]) -> tuple[list[dict[str, Any]], int, int]:
    """(Today's stuck rows, how many are snoozed, how many more than STUCK_CAP). A stuck clock never starts
    from a date an import or a sync brought in: it starts when the app got that stage, so a fresh export
    doesn't fill Today. A job with many stuck people is one row. "Move to" never points back up the funnel."""
    from .importers import file_entries, stage_rank

    clocks = file_entries(store)
    stuck: list[dict[str, Any]] = []
    hidden = 0
    for a in store.open_applications():
        clock = clocks.get((a["candidate_id"], a["job_id"]))
        since = max(a["stage_changed_at"] or "", clock["created_at"] or "") if clock else a["stage_changed_at"]
        days = _days_ago(since, now)
        if days < limit:
            continue
        if f"stuck:{a['id']}" in snoozed:
            hidden += 1
            continue
        stuck.append({"app": a, "since": since, "days": days, "run": clock.get("run") if clock else None})
    by_job: dict[int, list[dict[str, Any]]] = {}
    for x in stuck:
        by_job.setdefault(x["app"]["job_id"], []).append(x)
    rows: list[dict[str, Any]] = []
    for job_id, xs in by_job.items():
        xs.sort(key=lambda x: (-x["days"], x["app"]["candidate_name"]))
        name = label(job_id, xs[0]["app"]["job_title"])
        if len(xs) > STUCK_GROUP:
            key = f"stuck_job:{job_id}"
            if key in snoozed:
                hidden += 1
                continue
            runs = sorted({x["run"] for x in xs if x["run"]})
            rows.append({
                "key": key, "kind": "stuck", "rank": 2, "when": min(x["since"] for x in xs),
                "title": f"{len(xs)} people on {name} haven't moved in {limit} days or more",
                "detail": "Longest: " + ", ".join(f"{x['app']['candidate_name']} ({x['app']['stage']}, "
                                                  f"{x['days']} days)" for x in xs[:3]),
                "job_id": job_id, "count": len(xs), "import_run": runs[-1] if runs else None,
                "href": f"/jobs/{job_id}"})
            continue
        stages = store.job_stages(job_id)
        for x in xs:
            a = x["app"]
            nxt = store.next_stage(job_id, a["stage"])
            here = next((s for s in stages if s["name"].lower() == a["stage"].lower()), None)
            there = next((s for s in stages if s["name"] == nxt), None)
            r1 = stage_rank(a["stage"], here and here["outcome"])
            r2 = stage_rank(nxt, there and there["outcome"]) if nxt else None
            if r1 is not None and r2 is not None and r2 < r1:
                nxt = None  # the job's stages are out of funnel order here: never offer a move back
            rows.append({
                "key": f"stuck:{a['id']}", "kind": "stuck", "rank": 2, "when": x["since"],
                "title": f"{a['candidate_name']} has been in {a['stage']} for {x['days']} days",
                "detail": name, "candidate_id": a["candidate_id"], "job_id": job_id, "stage": a["stage"],
                "next_stage": nxt, "days": x["days"], "import_run": x["run"],
                "href": f"/candidates/{a['candidate_id']}"})
    rows.sort(key=lambda i: i["when"] or "")
    return rows[:STUCK_CAP], hidden, max(0, len(rows) - STUCK_CAP)


def snooze_import(store: Store, run: str, days: int = STUCK_SNOOZE_DAYS) -> int:
    """Snooze every stuck reminder about people whose stage came from this one import or sync."""
    from .importers import file_entries

    pairs = {k for k, v in file_entries(store).items() if v.get("run") == run}
    if not pairs:
        return 0
    n = 0
    jobs: set[int] = set()
    for a in store.open_applications():
        if (a["candidate_id"], a["job_id"]) in pairs:
            store.snooze(f"stuck:{a['id']}", days)
            jobs.add(a["job_id"])
            n += 1
    for job_id in jobs:
        store.snooze(f"stuck_job:{job_id}", days)
    return n


def _openings_item(store: Store) -> dict[str, Any] | None:
    """Jobs an import made ask once how many openings they have."""
    from .importers import openings_to_ask

    ids = openings_to_ask(store)
    if not ids:
        return None
    live = [j for j in store.list_jobs() if j["id"] in set(ids)]
    if not live:
        return None
    names = ", ".join(job_label(j["title"], j.get("company")) for j in live[:3])
    more = f" and {len(live) - 3} more" if len(live) > 3 else ""
    return {"key": "openings:import", "kind": "openings", "rank": 3, "when": max(j["created_at"] for j in live),
            "title": (f"How many openings does {names} have?" if len(live) == 1
                      else f"How many openings do {len(live)} jobs from an import have?"),
            "detail": "" if len(live) == 1 else f"{names}{more}. Client updates count hires against openings.",
            "href": "/import/openings", "jobs": live}


# ---------------------------------------------------------- status update
def _omit_key(job_id: int) -> str:
    return f"status_omit:{job_id}"


def internal_like(name: str | None) -> bool:
    """A stage named like Sourced, Contacted, New, Lead or Applied: people not presented to anyone yet."""
    key = _stage_key(name)
    return bool(key) and (key in INTERNAL_STAGE_WORDS or key.split()[0] in INTERNAL_STAGE_WORDS)


def submit_like(name: str | None) -> bool:
    """A stage named like Submitted, Presented or Shortlist: where people are first shown to the client."""
    return bool(set(_stage_key(name).split()) & SUBMIT_STAGE_WORDS)


def late_like(name: str | None) -> bool:
    """A stage named like an interview, an onsite, an HM screen or an offer: the client has seen these people."""
    words = _stage_key(name).split()
    return bool(set(words) & LATE_STAGE_WORDS) or words[:1] == ["hm"] or words[:2] == ["hiring", "manager"]


def default_omit(store: Store, job_id: int) -> list[str]:
    """The job's in-play stages named like internal ones, and its screening stages when it has a Submitted-like
    stage (Screened, in the agency set), left out of status updates until the recruiter chooses. Decided by what
    each stage counts as, never by where it sits: imported stages can land anywhere (reports.name_hidden)."""
    from .reports import name_hidden  # here: reports imports this module

    return name_hidden(store, job_id)


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


def booked_hidden(store: Store, job_id: int, omit: set[str], groups: list[dict[str, Any]] | None = None,
                  now: datetime | None = None) -> dict[int, str]:
    """{candidate_id: stage} for people in a left-out stage who have an interview booked for this job. A status
    update lists them anyway: a booked interview is never hidden from the client or hiring manager."""
    if not omit:
        return {}
    groups = groups if groups is not None else store.pipeline_by_stage(job_id)
    if not any(g["candidates"] and g["outcome"] == "open" and _stage_key(g["name"]) in omit for g in groups):
        return {}
    booked = next_interview_days(store, job_id, now)
    return {a["candidate_id"]: g["name"] for g in groups if g["outcome"] == "open" and _stage_key(g["name"]) in omit
            for a in g["candidates"] if a["candidate_id"] in booked}


def shown_numbers(store: Store, job_id: int, omit: set[str], groups: list[dict[str, Any]] | None = None,
                  now: datetime | None = None) -> dict[str, Any]:
    """job_numbers for the people a status update shows: not the left-out stages, but people there with an
    interview booked do count."""
    groups = groups if groups is not None else store.pipeline_by_stage(job_id)
    nums = job_numbers(store, job_id, omit, groups)
    nums["active"] += len(booked_hidden(store, job_id, omit, groups, now))
    return nums


def status_numbers(store: Store, job_id: int, omit_stages: list[str] | None = None) -> dict[str, Any]:
    """job_numbers for the people a status update shows (its left-out stages don't count, except people there
    with an interview booked)."""
    return shown_numbers(store, job_id, _omitted(store, job_id, omit_stages))


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
        """SELECT t.candidate_id, max(t.at) AS at, c.name, a.stage, a.created_at, a.notes, s.outcome
           FROM timeline t JOIN candidates c ON c.id = t.candidate_id
           JOIN applications a ON a.candidate_id = t.candidate_id AND a.job_id = t.job_id
           LEFT JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
           WHERE t.job_id = ? AND t.kind = 'stage' AND t.at >= ?
             AND coalesce(json_extract(CASE WHEN json_valid(t.data) THEN t.data END, '$.imported'), 0) = 0
           GROUP BY t.candidate_id ORDER BY at""", (job_id, cutoff))
    kinds: dict[str, list[str]] = {"hired": [], "out": [], "moved": [], "new": []}
    word = hire_word(store, job_id)
    own = inhouse(store)
    for r in rows:
        if omit and _stage_key(r["stage"]) in omit:
            continue
        outcome = r["outcome"] or "open"
        if outcome == "hired":
            kinds["hired"].append(r["name"])
        elif outcome == "lost":
            kinds["out"].append(f"{r['name']} ({_out_reason(r, own)})")
        elif r["created_at"] >= cutoff:
            kinds["new"].append(f"{r['name']} in {r['stage']}")
        else:
            kinds["moved"].append(f"{r['name']} to {r['stage']}")
    labels = {"hired": word.capitalize(), "out": "Out", "moved": "Moved", "new": "New"}
    return [f"- {labels[k]}: {_capped(v)}" for k, v in kinds.items() if v]


# Out stages that name the client, said as the hiring team's call in in-house wording.
_CLIENT_EXITS = frozenset({"client passed", "client pass", "client declined", "client rejected", "rejected by client"})


def _out_reason(app: dict[str, Any], inhouse_words: bool = False) -> str:
    """Why someone is out, as a status update says it: 'position filled' after a close-out (the client never
    turned them down), else their Out stage ('Hiring team passed' for a client exit in in-house wording)."""
    if (app.get("notes") or "").strip() == CLOSE_REASON:
        return "position filled"
    if inhouse_words and _stage_key(app["stage"]) in _CLIENT_EXITS:
        return "Hiring team passed"
    return app["stage"]


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
    booked = booked_hidden(store, job_id, omit, groups, now)
    nums = shown_numbers(store, job_id, omit, groups, now)
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
    from .importers import imported_placements  # placed by an import with no stage date: "(imported)"
    imported = imported_placements(store, job_id)
    total = shown = 0
    own = inhouse(store)
    for g in groups:
        total += len(g["candidates"])
        # A left-out stage still lists the people in it who have an interview booked.
        cands = ([a for a in g["candidates"] if a["candidate_id"] in booked] if _stage_key(g["name"]) in omit
                 else g["candidates"])
        if not cands:
            continue
        shown += len(cands)
        lost = g["outcome"] == "lost"
        people = [f"{a['candidate_name']} ("
                  + ("position filled, " if lost and _out_reason(a) == "position filled" else "")
                  + ("imported" if a["candidate_id"] in imported else _in_stage(_days_ago(a['stage_changed_at'], now),
                                                                             "today" if lost else "new today"))
                  + ")"
                  for a in sorted(cands, key=lambda a: a["stage_changed_at"])]
        label = "Hiring team passed" if own and lost and _stage_key(g["name"]) in _CLIENT_EXITS else g["name"]
        lines.append(f"{label} ({len(cands)}): {_capped(people)}")
    if not shown:
        lines.append("No one to share yet." if total else "No one in the pipeline yet.")
    changes = recent_changes(store, job_id, now, omit=omit)
    lines += ["", "Changes since last week:", *changes] if changes else ["", "Changes since last week: none."]
    results = interview_result_lines(store, job_id, now, omit=omit, groups=groups, booked=booked)
    if results:
        lines += ["", "Interview results:", *results]
    names = {a["candidate_id"]: a["candidate_name"] for g in groups if g["outcome"] == "open"
             for a in g["candidates"] if _stage_key(g["name"]) not in omit or a["candidate_id"] in booked}
    upcoming = sorted((ivs[0]["at"], cid, ivs) for cid, ivs in next_interview_days(store, job_id, now).items()
                      if cid in names)
    if upcoming:
        lines += ["", "Next interviews:"]
        lines += [interview_line(names[cid], ivs) for _, cid, ivs in upcoming]
    return "\n".join(lines)


def interview_result_lines(store: Store, job_id: int, now: datetime | None = None, days: int = 7, *,
                           omit: set[str] | None = None, groups: list[dict[str, Any]] | None = None,
                           booked: dict[int, str] | None = None) -> list[str]:
    """The status update's Interview results: how this job's interviews went in the last `days` days, for people
    the update shows (not in a left-out stage, unless an interview is booked)."""
    groups = groups if groups is not None else store.pipeline_by_stage(job_id)
    stage = {a["candidate_id"]: g["name"] for g in groups for a in g["candidates"]}
    return [r["line"] for r in interview_results(store, job_id, now, days)
            if r["candidate_id"] in stage and (not omit or _stage_key(stage[r["candidate_id"]]) not in omit
                                               or r["candidate_id"] in (booked or {}))]


def hidden_note(store: Store, job_id: int, omit_stages: list[str] | None = None) -> str:
    """'Not shown: Sourced (3), Contacted (1)': who the status update leaves out, for the recruiter's eyes only.
    It sits outside the text they copy, so the client never reads it. Empty when nothing is left out."""
    omit = _omitted(store, job_id, omit_stages)
    groups = store.pipeline_by_stage(job_id)
    booked = booked_hidden(store, job_id, omit, groups)
    hidden = [f"{g['name']} ({n})" for g in groups if _stage_key(g["name"]) in omit
              and (n := sum(1 for a in g["candidates"] if a["candidate_id"] not in booked))]
    parts = ["Not shown: " + ", ".join(hidden)] if hidden else []
    if booked:
        names = {a["candidate_id"]: a["candidate_name"] for g in groups for a in g["candidates"]}
        parts.append("listed though their stage is left out, since an interview is booked: "
                     + _capped([f"{names[cid]} ({stage})" for cid, stage in booked.items()]))
    out = "; ".join(parts)
    return out[:1].upper() + out[1:]


def digest_hidden_notes(store: Store, company: str | None = None,
                        omit_stages: list[str] | None = None) -> list[str]:
    """hidden_note for each job a status_digest covers, as 'Backend (Acme): not shown: Sourced (3)'."""
    out = []
    for g in company_rollup(store, company, omit_stages):
        for j in g["jobs"]:
            note = hidden_note(store, j["id"], omit_stages)
            if note:
                out.append(f"{job_label(j['title'], j.get('company'))}: {note[0].lower()}{note[1:]}")
    return out


_CO_SUFFIXES = frozenset({"inc", "incorporated", "llc", "llp", "ltd", "limited", "corp", "corporation", "co", "company",
                          "technologies", "technology", "plc", "gmbh", "sa", "ag", "bv", "pty", "holdings", "group"})


def company_key(name: str | None) -> str:
    """A company name for matching: lowercase, no punctuation, no trailing Inc, LLC, Ltd, Corp, Co or
    Technologies ('Plaid, Inc.' and 'plaid' are both 'plaid'). Never strips a name down to nothing."""
    words = re.sub(r"[^\w\s]|_", " ", (name or "").lower()).split()
    while len(words) > 1 and words[-1] in _CO_SUFFIXES:
        words.pop()
    return " ".join(words)


def match_companies(names: list[str | None], query: str | None) -> set[str]:
    """The lowercased company names a status_digest company filter means. Case, punctuation and suffixes like
    Inc or LLC don't count. The same name, else every name starting with it ('lumen' for Lumen Health), else
    the closest spelling ('Acme Fintek')."""
    q = company_key(query)
    by_key: dict[str, set[str]] = {}
    for n in names:
        if n and n.strip():
            by_key.setdefault(company_key(n), set()).add(n.strip().lower())
    if not q:
        return set()

    def names_of(keys: Any) -> set[str]:
        return {n for k in keys for n in by_key[k]}

    if q in by_key:
        return names_of([q])
    starts = ([k for k in by_key if k.startswith(q)]
              or [k for k in by_key if any(w.startswith(q) for w in k.split())])
    if starts:
        return names_of(starts)
    close = difflib.get_close_matches(q, list(by_key), n=1, cutoff=0.75)
    if close:
        return names_of(close)
    firsts = {k.split()[0]: k for k in sorted(by_key)}
    close = difflib.get_close_matches(q, list(firsts), n=1, cutoff=0.75)
    return names_of([firsts[close[0]]]) if close else set()


def _company_names(store: Store) -> list[str | None]:
    return [r["company"] for r in store._rows("SELECT DISTINCT company FROM jobs WHERE status IN ('open', 'on_hold')")]


def company_rollup(store: Store, company: str | None = None, omit_stages: list[str] | None = None,
                   ) -> list[dict[str, Any]]:
    """[{company, jobs, active, hired, openings, filled, word}] for open jobs, one row per company (None for no
    company). Counts cover the people each job's status update shows, each person once even when they're in two
    of the company's jobs. company matches as in match_companies."""
    wanted = match_companies(_company_names(store), company) if company is not None else None
    rows: dict[str, dict[str, Any]] = {}
    for j in store.list_jobs("open"):
        co = j.get("company") or ""
        if wanted is not None and co.strip().lower() not in wanted:
            continue
        omit = _omitted(store, j["id"], omit_stages)
        groups = store.pipeline_by_stage(j["id"])
        nums = job_numbers(store, j["id"], omit, groups)
        booked = booked_hidden(store, j["id"], omit, groups)
        r = rows.setdefault(company_key(co), {"company": co or None, "jobs": [], "active": 0, "hired": 0,
                                              "openings": 0, "filled": 0, "_people": set(), "_hired": set()})
        r["jobs"].append(j)
        for g in groups:  # people, not applications: one person in two of a client's jobs counts once
            if _stage_key(g["name"]) not in omit and g["outcome"] in ("open", "hired"):
                r["_people" if g["outcome"] == "open" else "_hired"].update(a["candidate_id"] for a in g["candidates"])
        r["_people"].update(booked)  # left out by stage, but an interview is booked
        r["active"] = len(r["_people"])
        r["hired"] = len(r["_hired"])
        r["openings"] += nums["openings"]
        r["filled"] += nums["filled_seats"]
        r.setdefault("words", set()).add(nums["word"])
    for r in rows.values():
        r.pop("_people"), r.pop("_hired")
        r["word"] = "placed" if r.pop("words") == {"placed"} else "hired"  # the jobs' own word, when they agree
        r["filled_text"] = filled_text(r["filled"], r["openings"])
    return sorted(rows.values(), key=lambda r: (r["company"] is None, (r["company"] or "").lower()))


def _manager_names(store: Store) -> list[str | None]:
    return [r["hiring_manager"] for r in store._rows(
        "SELECT DISTINCT hiring_manager FROM jobs WHERE status IN ('open', 'on_hold')")]


def _job_filter(store: Store, company: str | None, manager: str | None) -> Any:
    """A test for jobs matching the company and hiring manager filters (each matched as in match_companies:
    any case, the start of a name or one of its words, or a close spelling, so 'priya' finds Priya Raman)."""
    cos = match_companies(_company_names(store), company) if company is not None else None
    hms = match_companies(_manager_names(store), manager) if manager is not None else None
    return lambda j: ((cos is None or (j.get("company") or "").strip().lower() in cos)
                      and (hms is None or (j.get("hiring_manager") or "").strip().lower() in hms))


def by_manager(store: Store) -> bool:
    """Roll status updates up by hiring manager: in-house, once an open job names one."""
    return inhouse(store) and any((m or "").strip() for m in _manager_names(store))


def manager_rollup(store: Store, manager: str | None = None, omit_stages: list[str] | None = None,
                   company: str | None = None) -> list[dict[str, Any]]:
    """Like company_rollup, one row per hiring manager instead: [{manager, label, jobs, active, hired, openings,
    filled, word, filled_text}] for open jobs (manager None for jobs with no hiring manager)."""
    keep = _job_filter(store, company, manager)
    rows: dict[str, dict[str, Any]] = {}
    for j in store.list_jobs("open"):
        if not keep(j):
            continue
        hm = (j.get("hiring_manager") or "").strip()
        nums = shown_numbers(store, j["id"], _omitted(store, j["id"], omit_stages))
        r = rows.setdefault(re.sub(r"\s+", " ", hm.lower()), {
            "manager": hm or None, "label": hm or None, "jobs": [], "active": 0, "hired": 0, "openings": 0,
            "filled": 0, "words": set()})
        r["jobs"].append(j)
        for k, n in (("active", "active"), ("hired", "hired"), ("openings", "openings"), ("filled", "filled_seats")):
            r[k] += nums[n]
        r["words"].add(nums["word"])
    for r in rows.values():
        r["word"] = "placed" if r.pop("words") == {"placed"} else "hired"
        r["filled_text"] = filled_text(r["filled"], r["openings"])
    return sorted(rows.values(), key=lambda r: (r["manager"] is None, (r["manager"] or "").lower()))


def held_jobs(store: Store, company: str | None = None, manager: str | None = None) -> list[dict[str, Any]]:
    """On-hold jobs, for one company (matched as in match_companies) or all."""
    keep = _job_filter(store, company, manager)
    return [j for j in store.list_jobs("on_hold") if keep(j)]


def status_digest(store: Store, company: str | None = None, now: datetime | None = None, *,
                  omit_stages: list[str] | None = None, hiring_manager: str | None = None,
                  group_by_manager: bool | None = None) -> str:
    """One paste-ready update for every open job (or one company's, or one hiring manager's), with a roll-up
    per company first (per hiring manager with a hiring_manager filter, or in-house once jobs name one).

    company and hiring_manager match any case, by the start of the name, or by a close spelling. omit_stages
    applies to every job; when None, each job uses its own Leave out choice. On-hold jobs are listed one line
    each at the end."""
    now = now or datetime.now(timezone.utc)
    if group_by_manager is None:
        group_by_manager = hiring_manager is not None or (company is None and by_manager(store))
    if group_by_manager:
        groups = [{**g, "company": g["manager"]} for g in manager_rollup(store, hiring_manager, omit_stages, company)]
    else:
        groups = company_rollup(store, company, omit_stages)
    held = held_jobs(store, company, hiring_manager)
    if not groups and not held:
        named = " and ".join(x.strip().rstrip(".") for x in (company, hiring_manager) if x and x.strip())
        return f"No open jobs{' for ' + named if named else ''}."
    if hiring_manager and hiring_manager.strip():
        found = sorted({g["company"] for g in groups if g["company"]}
                       | {j["hiring_manager"] for j in held if j.get("hiring_manager")}, key=str.lower)
    else:
        found = sorted({g["company"] for g in groups if g["company"]}
                       | {j["company"] for j in held if j.get("company")}, key=str.lower)
    named = (company and company.strip()) or (hiring_manager and hiring_manager.strip())
    who = " and ".join(found) if named and found else "all open jobs"
    word = "Hiring update" if inhouse(store) else "Status update"
    lines = [f"{word} for {who}, {local_time(now.isoformat(), '%a %b %-d, %Y')}"]

    def summary(g: dict[str, Any]) -> str:
        n = len(g["jobs"])
        return f"{n} open job{'' if n == 1 else 's'}, {g['active']} in process, {g['filled_text']}."

    if len(groups) > 1:
        none = "No hiring manager" if group_by_manager else "No company"
        lines += [f"- {g['company'] or none}: {summary(g)}" for g in groups]
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
    found = STAGE_SETS.get(re.sub(r"[\s_-]+", "", (key or "").strip().lower()))
    return list(found[1]) if found and found[1] else None


# What a built-in set's stage counts as in reports where its name says otherwise: in-house, the hiring manager's
# screen is still screening (an agency's client HM screen is an interview, so this goes by set, not by name).
SET_GROUPS: dict[str, dict[str, str]] = {"inhouse": {"Hiring manager screen": "screening"}}


def default_stage_set(store: Store) -> str:
    """The stage set a new job starts with when none is picked: in-house for someone hiring for their own company."""
    return "inhouse" if inhouse(store) else "default"


def apply_set_groups(store: Store, job_id: int, set_name: str | None) -> None:
    """After a job is made from a built-in set, save what its stages count as (SET_GROUPS), as Edit job shows."""
    from . import reports  # reports imports this module
    for stage, group in SET_GROUPS.get(re.sub(r"[\s_-]+", "", (set_name or "").strip().lower()), {}).items():
        try:
            reports.set_stage_group(store, job_id, stage, group)
        except (NotFound, ValueError):
            continue  # renamed or not there: nothing to set


def copied_stages(store: Store, job_id: int) -> list[dict[str, str]]:
    """Another job's stages, with what each counts as, for a new job that copies them."""
    store.get_job(job_id)  # NotFound when there's no such job
    return [{"name": s["name"], "outcome": s["outcome"]} for s in store.job_stages(job_id)]


def new_job_stages(store: Store, stages: list[Any] | None = None, set_name: str | None = None,
                   from_job_id: int | None = None) -> list[Any] | None:
    """The stages a new job starts with: its own list, a copy of another job's, or a built-in stage set
    (default, agency, inhouse). None means the default set. Raises ValueError when more than one is given
    or the set or job isn't known."""
    given = [n for n, v in (("stages", stages), ("stages_from_job_id", from_job_id), ("stage_set", set_name))
             if v not in (None, "", [])]
    if len(given) > 1:
        raise ValueError(f"Give only one of {' or '.join(given)}.")
    if from_job_id not in (None, ""):
        try:
            return copied_stages(store, int(from_job_id))
        except (NotFound, ValueError, TypeError):
            raise ValueError(f"No job with id {from_job_id} to copy stages from. list_jobs shows them.") from None
    if set_name not in (None, ""):
        key = re.sub(r"[\s_-]+", "", str(set_name).strip().lower())
        if key not in STAGE_SETS:
            raise ValueError(f"No stage set called {set_name!r}. Stage sets: {', '.join(STAGE_SETS)}.")
        return stage_set(key)
    return stages or None


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


CLOSE_OUT_PREFIX = "close_out:"  # settings key of one web close-out, so Changes can undo it as one


def close_out(store: Store, job_id: int, by: str = "app", close_job: bool = False) -> dict[str, Any]:
    """Move everyone still in play to Rejected (Position filled) and cancel their upcoming interviews, and with
    close_job also close the job. The app asks first; the AI never runs this (close_out_hint tells it to ask
    the recruiter). The whole close-out is recorded as one batch (a setting), so undo_close_out puts it back."""
    import json

    job = store.get_job(job_id)
    stage = out_stage_name(store, job_id)
    if not any(s["outcome"] == "lost" for s in store.job_stages(job_id)):
        store.add_stage(job_id, stage, outcome="lost")
    moves: list[dict[str, Any]] = []
    interviews: list[int] = []
    for a in in_play(store, job_id):
        ivs = upcoming_for(store, a["candidate_id"], job_id)
        store.move_stage(a["candidate_id"], job_id, stage, notes=CLOSE_REASON, by=by)
        entry = store._row("SELECT id FROM timeline WHERE candidate_id = ? AND job_id = ? AND kind = 'stage'"
                           " ORDER BY id DESC LIMIT 1", (a["candidate_id"], job_id))
        moves.append({"candidate_id": a["candidate_id"], "from": a["stage"], "notes": a.get("notes"),
                      "timeline_id": entry["id"] if entry else None})
        for iv in ivs:
            store.update_interview(iv["id"], cancel=True, by=by)
            interviews.append(iv["id"])
    closed = close_job and job["status"] != "closed"
    if closed:
        store.update_job(job_id, status="closed", by=by)
    batch_id = None
    if moves or closed:
        rows = store._rows(f"SELECT key FROM settings WHERE key LIKE '{CLOSE_OUT_PREFIX}%'")
        batch_id = 1 + max((int(r["key"][len(CLOSE_OUT_PREFIX):]) for r in rows
                            if r["key"][len(CLOSE_OUT_PREFIX):].isdigit()), default=0)
        _put_setting(store, f"{CLOSE_OUT_PREFIX}{batch_id}", json.dumps({
            "job_id": job_id, "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "stage": stage,
            "moves": moves, "interviews": interviews, "closed_job": closed, "old_status": job["status"]}))
    return {"moved": len(moves), "cancelled": len(interviews), "stage": stage, "closed_job": closed,
            "batch_id": batch_id}


def close_out_batches(store: Store, since: str | None = None) -> list[dict[str, Any]]:
    """Web close-outs, newest first, each with a Changes line ('Closed out Backend (Acme): moved 3 people to
    Rejected'), whether it can still be undone, and its id. Ones whose job is gone are left out."""
    import json

    out = []
    for r in store._rows(f"SELECT key, value FROM settings WHERE key LIKE '{CLOSE_OUT_PREFIX}%'"):
        bid = r["key"][len(CLOSE_OUT_PREFIX):]
        try:
            b = json.loads(r["value"])
        except ValueError:
            continue
        if not bid.isdigit() or not isinstance(b, dict) or (since and (b.get("at") or "") < since):
            continue
        job = store._row("SELECT title, company FROM jobs WHERE id = ?", (b.get("job_id"),))
        if not job:
            continue
        n, m = len(b.get("moves") or []), len(b.get("interviews") or [])
        did = [f"moved {n} {'person' if n == 1 else 'people'} to {b.get('stage')}",
               *([f"cancelled {m} interview{'' if m == 1 else 's'}"] if m else []),
               *(["closed the job"] if b.get("closed_job") else [])]
        text = f"Closed out {_job_name(job)}: " + (
            ", ".join(did[:-1]) + " and " + did[-1] if len(did) > 1 else did[0])
        out.append({**b, "id": int(bid), "job_title": job["title"], "job_company": job["company"], "text": text,
                    "undoable": not b.get("undone_at")})
    return sorted(out, key=lambda b: (b["at"], b["id"]), reverse=True)


def undo_close_out(store: Store, batch_id: int, by: str = "app") -> str:
    """Put a web close-out back: everyone it moved goes back to the stage they were in (unless they were moved
    again since), their cancelled interviews are scheduled again, and a job it closed is reopened.
    Returns the notice. Raises NotFound or ValueError (already undone)."""
    import json

    key = f"{CLOSE_OUT_PREFIX}{batch_id}"
    raw = store.setting(key)
    if not raw:
        raise NotFound("That close-out is gone. Reload the page.")
    b = json.loads(raw)
    if b.get("undone_at"):
        raise ValueError("That close-out was already undone.")
    back = stayed = restored = 0
    for m in b.get("moves") or []:
        try:
            if not m.get("timeline_id"):
                raise ValueError("no entry")
            store.undo_change(m["timeline_id"], by=by)
        except (ValueError, NotFound):
            stayed += 1
            continue
        back += 1
        with store._lock, store._conn:  # the close-out's reason goes with it, so a later move isn't "position filled"
            store._conn.execute("UPDATE applications SET notes = ? WHERE candidate_id = ? AND job_id = ? AND notes = ?",
                                (m.get("notes"), m["candidate_id"], b["job_id"], CLOSE_REASON))
    for iv in b.get("interviews") or []:
        try:
            if store.get_interview(iv)["data"].get("status") == "cancelled":
                store.update_interview(iv, restore=True, by=by)
                restored += 1
        except (ValueError, NotFound):
            continue
    reopened = False
    job = store._row("SELECT status FROM jobs WHERE id = ?", (b["job_id"],))
    if b.get("closed_job") and job and job["status"] == "closed":
        store.update_job(b["job_id"], status=b.get("old_status") or "open", by=by)
        reopened = True
    b["undone_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    _put_setting(store, key, json.dumps(b))
    notice = f"Undid the close-out. Moved {back} {'person' if back == 1 else 'people'} back to the stage they were in"
    if restored:
        notice += f" and put back {restored} interview{'' if restored == 1 else 's'}"
    notice += "."
    if reopened:
        notice += " The job is open again."
    if stayed:
        notice += f" {stayed} {'person was' if stayed == 1 else 'people were'} moved again since, so they stay put."
    return notice


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


# ------------------------------------------------- a stage's "Counts as"
STAGE_OUTCOME_PREFIX = "stage_outcome:"  # settings key of one app change to what a stage counts as
OUTCOME_WORDS = {"open": "In play", "hired": "Hired", "lost": "Out"}


def change_stage_outcome(store: Store, job_id: int, stage: str, outcome: str) -> dict[str, Any]:
    """Set what a stage counts as from the app, log it on Changes (undoable), and say what it means for the
    people in it: {'changed', 'stage', 'old', 'new', 'notice', 'asks'}, where asks holds exit_interviews for each
    person in the stage who still has an interview coming up. Raises ValueError for an unknown stage or outcome."""
    import json

    from .store import normalize_outcome

    found = store.resolve_stage(job_id, stage)
    new = normalize_outcome(outcome) or ""
    store.set_stage_outcome(job_id, found["name"], outcome)
    res: dict[str, Any] = {"changed": found["outcome"] != new, "stage": found["name"], "old": found["outcome"],
                           "new": new, "asks": [], "notice": ""}
    if not res["changed"]:
        return res
    people = next((g["candidates"] for g in store.pipeline_by_stage(job_id) if g["name"] == found["name"]), [])
    rows = store._rows(f"SELECT key FROM settings WHERE key LIKE '{STAGE_OUTCOME_PREFIX}%'")
    cid = 1 + max((int(r["key"][len(STAGE_OUTCOME_PREFIX):]) for r in rows
                   if r["key"][len(STAGE_OUTCOME_PREFIX):].isdigit()), default=0)
    _put_setting(store, f"{STAGE_OUTCOME_PREFIX}{cid}", json.dumps({
        "job_id": job_id, "stage": found["name"], "old": found["outcome"], "new": new, "people": len(people),
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}))
    n = len(people)
    who = f"{n} {'person' if n == 1 else 'people'}" if n else "Nobody"
    effect = {"lost": "out of the running", "hired": "counted as hired", "open": "in play"}[new]
    res["notice"] = (f"{found['name']} now counts as {OUTCOME_WORDS[new]}. {who} in it {'is' if n <= 1 else 'are'} "
                     f"{effect} now. You can undo this on Changes.")
    if new != "open":
        res["asks"] = [a for a in (exit_interviews(store, p["candidate_id"], job_id) for p in people) if a]
    if res["asks"]:
        names = [a["name"] for a in res["asks"]]
        res["notice"] += (f" {' and '.join(names) if len(names) < 3 else ', '.join(names[:-1]) + ' and ' + names[-1]}"
                          f" still {'has' if len(names) == 1 else 'have'} an interview coming up.")
    return res


def stage_outcome_changes(store: Store, since: str | None = None) -> list[dict[str, Any]]:
    """App changes to what a stage counts as, newest first, each with a Changes line and whether it can be undone."""
    import json

    out = []
    for r in store._rows(f"SELECT key, value FROM settings WHERE key LIKE '{STAGE_OUTCOME_PREFIX}%'"):
        cid = r["key"][len(STAGE_OUTCOME_PREFIX):]
        try:
            c = json.loads(r["value"])
        except ValueError:
            continue
        if not cid.isdigit() or not isinstance(c, dict) or (since and (c.get("at") or "") < since):
            continue
        job = store._row("SELECT title, company FROM jobs WHERE id = ?", (c.get("job_id"),))
        if not job:
            continue
        text = (f"{c.get('stage')} on {_job_name(job)} counts as {OUTCOME_WORDS.get(c.get('new'), c.get('new'))} "
                f"(was {OUTCOME_WORDS.get(c.get('old'), c.get('old'))})")
        out.append({**c, "id": int(cid), "job_title": job["title"], "text": text, "undoable": not c.get("undone_at")})
    return sorted(out, key=lambda c: (c["at"], c["id"]), reverse=True)


def undo_stage_outcome(store: Store, change_id: int) -> str:
    """Put a stage back to what it counted as before an app change. Returns the notice. Raises NotFound, or
    ValueError when it was already undone or the stage has changed since."""
    import json

    key = f"{STAGE_OUTCOME_PREFIX}{change_id}"
    raw = store.setting(key)
    if not raw:
        raise NotFound("That change is gone. Reload the page.")
    c = json.loads(raw)
    if c.get("undone_at"):
        raise ValueError("That change was already undone.")
    stage = next((s for s in store.job_stages(c["job_id"]) if s["name"].lower() == str(c["stage"]).lower()), None)
    if not stage or stage["outcome"] != c["new"]:
        raise ValueError(f"Couldn't undo that: {c['stage']} was renamed, removed or changed again since.")
    store.set_stage_outcome(c["job_id"], stage["name"], c["old"])
    c["undone_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    _put_setting(store, key, json.dumps(c))
    return f"Undid: {stage['name']} counts as {OUTCOME_WORDS[c['old']]} again."


# ------------------------------------------------------------- wording
def inhouse(store: Store) -> bool:
    """The recruiter hires for their own company (in-house wording instead of agency wording)."""
    return store.setting(INHOUSE_KEY) == "1"


def hiring_for_answered(store: Store) -> bool:
    """They've said whether they hire for their own company or for clients, so the welcome stops asking."""
    return inhouse(store) or store.setting(HIRING_FOR_ANSWERED_KEY) == "1"


def set_inhouse(store: Store, on: bool) -> None:
    _put_setting(store, HIRING_FOR_ANSWERED_KEY, "1")
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


__all__ = ["FLAGS", "THRESHOLDS", "NotFound", "action_items", "add_followup", "close_out", "close_out_batches",
           "close_out_hint", "close_out_plan", "company_key", "undo_close_out", "company_rollup", "complete_followup", "due_text", "exit_interviews", "headline",
           "job_label", "job_numbers", "job_status_text", "local_time", "match_companies", "next_interviews",
           "exit_prompts", "interview_label", "interview_line", "local_days", "manager_rollup", "next_interview_days",
           "parse_day", "recent_changes", "interview_results", "interview_result_lines", "group_loops", "pass_stage", "default_stage_set", "apply_set_groups", "schedule_followup", "status_digest", "twin_hints", "with_out_stages"]
