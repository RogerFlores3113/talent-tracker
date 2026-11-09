"""The Interviews week: every session booked that week across all jobs (Saturday and Sunday show when something is
booked), grouped into loops, with each interviewer's load and the clashes worth fixing (an interviewer double-booked,
a candidate in two sessions at once), and who still owes an outcome or feedback.

Also each interviewer's own verdict on a joint or panel session (kept in the interview's data as `verdicts`, the
session's outcome rolled up from them), and the feedback chase: who still owes feedback a day after a session, with
a request they can paste or send from their own mail. The app sends nothing."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from urllib.parse import quote

from . import actions
from .store import (CHANGE_SOURCES, INTERVIEW_VERDICTS, VERDICT_LABELS, Store, _legacy_verdict, as_local, fold_text,
                    interview_note, now_iso, quoted, shown_zone, to_dt, zone_named)

SESSION_MINUTES = 45  # interviews have no length saved; two starting closer than this overlap


_LENGTH = re.compile(r"(?<![\w.])(\d{2,3})\s*(?:min|mins|minutes|m)\b", re.I)


def session_minutes(t: dict[str, Any]) -> int:
    """How long a session runs: the length saved with it (`duration`), else '(30 min)' or '30 minutes' in its
    summary, else SESSION_MINUTES."""
    saved = _data(t).get("minutes") if isinstance(t.get("data"), (dict, str)) else None
    if isinstance(saved, int) and not isinstance(saved, bool):
        return min(480, max(10, saved))
    m = _LENGTH.search(str(t.get("body") or ""))
    return min(480, max(10, int(m.group(1)))) if m else SESSION_MINUTES


MIN_YEAR, MAX_YEAR = 1970, 2200  # days outside this are refused (a plain message) rather than overflowing


def _in_range(day: date) -> date:
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise ValueError(f"Pick a day between {MIN_YEAR} and {MAX_YEAR}.")
    return day


def week_start(day: date | None = None) -> date:
    """The Monday on or before `day`, so a Saturday or Sunday session shows in the week it belongs to. With no day
    (today), a weekend opens on the coming Monday instead."""
    if day is None:
        day = as_local(datetime.now(timezone.utc)).date()
        if day.weekday() >= 5:
            day += timedelta(days=7 - day.weekday())
    return day - timedelta(days=day.weekday())


def interviewers(text: str | None) -> list[str]:
    """'Sam Rivera, Alex Chen' or 'Priya and Ravi' -> each name."""
    parts = re.split(r"\s*(?:,|;|&|/|\band\b|\+)\s*", text or "")
    return [p.strip() for p in parts if p.strip()]


def _midnight(day: date) -> str:
    """The start of `day` on the recruiter's clock (Settings > Your time zone, else this computer's), in UTC."""
    zone = shown_zone()
    at = datetime.combine(day, time.min, zone) if zone is not None else datetime.combine(day, time.min).astimezone()
    return at.astimezone(timezone.utc).isoformat()


def week_day(value: str | None, today: date | None = None) -> date | None:
    """A day in the week to show: '2026-11-09', a time, or 'this' / 'next' / 'last' (week). None means this week."""
    v = re.sub(r"\s+", " ", (value or "").strip().lower())
    if not v:
        return None
    today = today or as_local(datetime.now(timezone.utc)).date()
    words = {"this": 0, "this week": 0, "current": 0, "now": 0, "next": 7, "next week": 7, "last": -7,
             "last week": -7, "previous": -7, "previous week": -7}
    if v in words:
        if not words[v]:
            return None
        base = today + timedelta(days=7 - today.weekday()) if today.weekday() >= 5 else today
        return week_start(base) + timedelta(days=words[v])
    try:
        return _in_range(as_local(to_dt(v)).date() if "t" in v else date.fromisoformat(v))
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ValueError("week is a day like 2026-11-09, or this or next.") from None


def _session_rows(store: Store, lo: str, hi: str, cancelled: bool = False) -> list[dict[str, Any]]:
    """Interviews starting from `lo` up to (not including) `hi` (UTC ISO times), in time order, with the candidate's
    name and the job's title and company. Cancelled ones are left out unless asked for."""
    skip = "" if cancelled else " AND coalesce(json_extract(t.data, '$.status'), 'scheduled') <> 'cancelled'"
    return store._rows(
        """SELECT t.*, c.name AS candidate_name, j.title AS job_title, j.company AS job_company,
                  pz.time_zone AS cand_zone FROM timeline t
           JOIN candidates c ON c.id = t.candidate_id LEFT JOIN jobs j ON j.id = t.job_id
           LEFT JOIN person_zones pz ON pz.candidate_id = t.candidate_id
           WHERE t.kind = 'interview' AND t.at >= ? AND t.at < ?""" + skip + " ORDER BY t.at, t.id", (lo, hi))


def week(store: Store, start: date | None = None) -> dict[str, Any]:
    """{start, end, prev, next, days: [{day, label, loops: [...]}], load: [{name, count}], clashes: [...],
    total, loops, owed}. Each loop is one person's sessions for one job that day, in time order. Monday to
    Friday always show; Saturday and Sunday only when something is booked then."""
    monday = week_start(_in_range(start) if start else None)
    lo, hi = _midnight(monday), _midnight(monday + timedelta(days=7))
    rows = _session_rows(store, lo, hi)
    book = store.interviewer_book()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    today = as_local(datetime.now(timezone.utc)).date()
    days = [{"day": monday + timedelta(days=i), "label": actions.day_text(monday + timedelta(days=i)),
             "today": monday + timedelta(days=i) == today, "loops": []} for i in range(7)]
    loops = []
    chase = _chase_pairs(store, {(t["candidate_id"], t.get("job_id")) for t in rows})
    for g in actions.group_loops(rows):
        by_day: dict[date, list[dict[str, Any]]] = {}
        for t in g:  # a session moved to another day shows on that day, still marked as part of its loop
            by_day.setdefault(actions._local_day(t["at"]), []).append(t)
        first = g[0]
        loop = {"candidate_id": first["candidate_id"], "name": first["candidate_name"], "job_id": first.get("job_id"),
                "job": actions.job_label(first.get("job_title"), first.get("job_company")) if first.get("job_title")
                else None, "size": len(g), "sessions": [_owing(t, now, chase) for t in g]}
        loops.append(loop)
        for d, ts in by_day.items():
            col = next((c for c in days if c["day"] == d), None)
            if col is not None:
                col["loops"].append({**loop, "sessions": [_owing(t, now, chase) for t in ts],
                                     "split": len(ts) < len(g)})
    load: dict[str, dict[str, Any]] = {}
    for t in rows:
        for who in interviewers(actions.interviewer_of(t)):
            e = load.setdefault(who.casefold(), {"name": who, "count": 0, "ids": []})
            e["count"] += 1
            e["ids"].append(t["id"])
    clashes = _clashes(rows)
    flagged = {i for c in clashes for i in c["ids"]}
    for col in days:
        for lp in col["loops"]:
            for x in lp["sessions"]:
                x["clash"] = x["interview_id"] in flagged
    days = [d for d in days if d["day"].weekday() < 5 or d["loops"]]
    owed = [{"name": lp["name"], "candidate_id": lp["candidate_id"], "interview_id": x["interview_id"],
             "session": x["name"], "at": x["at"], "outcome": x["needs_outcome"], "feedback": x["owed"]}
            for lp in loops for x in lp["sessions"] if x["needs_outcome"] or x["owed"]]
    by_id = {t["id"]: t for t in rows}
    entries = []
    for o in owed:
        t = by_id[o["interview_id"]]
        for who in o["feedback"]:
            email = next((email_for(n, book) for n in named(t) if display_name(n) == who and email_for(n, book)), None)
            entries.append({"who": who, "email": email, "candidate_id": o["candidate_id"], "name": o["name"],
                            "job_id": t.get("job_id"), "job": None, "interview_id": o["interview_id"],
                            "session": o["session"], "at": o["at"], "day": actions.local_time(o["at"], "%a %b %-d")})
    first_month = monday.replace(day=1)
    last_month = (monday + timedelta(days=6)).replace(day=1)
    months = [first_month] if first_month == last_month else [first_month, last_month]
    return {"start": monday, "end": days[-1]["day"], "prev": monday - timedelta(days=7),
            "months": [{"first": m.isoformat()[:7], "label": actions.day_text(m, "%B %Y")} for m in months],
            "next": monday + timedelta(days=7), "days": days, "loops": loops, "total": len(rows), "clashes": clashes,
            "load": sorted(load.values(), key=lambda e: (-e["count"], e["name"].casefold())), "owed": owed,
            "owed_people": group_owed(entries)}


def _chase_pairs(store: Store, pairs: set[tuple[int, Any]]) -> set[tuple[int, Any]]:
    """Of these (candidate, job) pairs, the ones whose feedback is still worth asking for: the person is in play
    on an open job, or the session names no job. A closed req or someone hired or out owes nobody feedback."""
    out = {p for p in pairs if p[1] is None}
    jobs = {p[1] for p in pairs if p[1] is not None}
    if not jobs:
        return out
    marks = ", ".join("?" * len(jobs))
    live = {(r["candidate_id"], r["job_id"]) for r in store._rows(
        f"""SELECT a.candidate_id, a.job_id FROM applications a JOIN jobs j ON j.id = a.job_id
            JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
            WHERE a.job_id IN ({marks}) AND j.status = 'open' AND s.outcome = 'open'""", tuple(jobs))}
    return out | (pairs & live)


def _owing(t: dict[str, Any], now_iso: str, chase: set[tuple[int, Any]] | None = None) -> dict[str, Any]:
    """A session as the week lists it, with needs_outcome (it has taken place and nobody said how it went) and
    owed (the named interviewers who have given no feedback or verdict on it yet, while it's still worth asking:
    see _chase_pairs)."""
    x = actions._session(t, now_iso)
    past = x["started"] and x["status"] == "scheduled"
    asks = chase is None or (t["candidate_id"], t.get("job_id")) in chase
    owed = owed_by(t) if asks and x["started"] and x["status"] != "cancelled" else []
    mins = session_minutes(t)
    out = {**x, "needs_outcome": past, "owed": owed, "minutes": mins,
           "end": (to_dt(t["at"]) + timedelta(minutes=mins)).replace(microsecond=0).isoformat(),
           "candidate_tz": candidate_zone(t), "interviewers": interviewers(actions.interviewer_of(t))}
    if owed:  # feedback is due a day after the session ends
        due = to_dt(t["at"]) + timedelta(minutes=session_minutes(t) + CHASE_HOURS * 60)
        out["feedback_due"] = due.replace(microsecond=0).isoformat()
        out["feedback_overdue"] = due < to_dt(now_iso)
    return out


def _clashes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two sessions that overlap (each runs session_minutes long, 45 unless its summary says '(30 min)') with the
    same interviewer, or with the same candidate. Back-to-back sessions are not a clash."""
    out = []
    for i, a in enumerate(rows):
        end = to_dt(a["at"]) + timedelta(minutes=session_minutes(a))
        for b in rows[i + 1:]:
            if to_dt(b["at"]) >= end:
                break
            when = actions.local_time(a["at"], actions.IV_TIME)
            shared = {w.casefold() for w in interviewers(actions.interviewer_of(a))} & \
                     {w.casefold() for w in interviewers(actions.interviewer_of(b))}
            if shared:
                who = next(w for w in interviewers(actions.interviewer_of(a)) if w.casefold() in shared)
                sa, sb = actions.interview_name(a) or "Interview", actions.interview_name(b) or "Interview"
                if a["candidate_id"] != b["candidate_id"]:
                    what = f"{a['candidate_name']} and {b['candidate_name']}"
                elif sa.casefold() != sb.casefold():  # one candidate: name them once, and the two sessions
                    what = f"{a['candidate_name']}'s {sa} and {sb}"
                else:
                    what = f"two sessions with {a['candidate_name']}"
                out.append({"kind": "interviewer", "ids": [a["id"], b["id"]],
                            "text": f"{who} is booked twice around {when}: {what}"})
            if a["candidate_id"] == b["candidate_id"]:
                out.append({"kind": "candidate", "ids": [a["id"], b["id"]],
                            "text": f"{a['candidate_name']} has two sessions around {when}"})
    return out


def week_text(w: dict[str, Any]) -> str:
    """The week as plain text (for the AI): each day's loops, then each interviewer's load and any clashes."""
    lines = [f"Interviews {actions.day_text(w['start'])} to {actions.day_text(w['end'])}: {w['total']} "
             f"session{'' if w['total'] == 1 else 's'}."]
    for d in w["days"]:
        if not d["loops"]:
            continue
        lines.append(d["label"] + ":")
        for lp in d["loops"]:
            parts = [f"{actions.local_time(x['at'], '%-I:%M %p %Z').strip()} {x['name']}"
                     + (f" (with {x['interviewer']})" if x["interviewer"] else "") for x in lp["sessions"]]
            lines.append(f"- {lp['name']}" + (f", {lp['job']}" if lp["job"] else "") + ": " + "; ".join(parts))
    if w["load"]:
        lines.append("Interviewer load: " + ", ".join(f"{e['name']} {e['count']}" for e in w["load"]) + ".")
    lines += [f"Clash: {c['text']}." for c in w["clashes"]]
    for p in w.get("owed_people") or []:
        lines.append(f"Feedback owed: {p['summary']}.")
    for o in w.get("owed") or []:
        bits = (["needs an outcome"] if o["outcome"] else []) + (
            [f"feedback owed by {', '.join(o['feedback'])}"] if o["feedback"] else [])
        lines.append(f"Still open: {o['name']}, {o['session']} ({actions.local_time(o['at'], '%a %b %-d')}): "
                     + "; ".join(bits) + ".")
    return "\n".join(lines)


# ------------------------------------------------------------ verdicts per interviewer
VERDICT_SCORE = {"strong_yes": 3, "yes": 2, "lean_yes": 1, "mixed": 0, "lean_no": -1, "no": -2, "strong_no": -3}
VERDICT_CHOICES = (("strong yes", "Strong yes"), ("yes", "Yes"), ("mixed", "Mixed"), ("no", "No"),
                   ("strong no", "Strong no"))


def _data(t: dict[str, Any]) -> dict[str, Any]:
    return actions._iv_data(t)


def display_name(who: str) -> str:
    """'Alex Ruiz <alex@lumen.com>' -> 'Alex Ruiz'."""
    return re.sub(r"\s*<[^>]*>\s*", " ", who).strip() or who.strip()


def email_of(who: str) -> str | None:
    m = re.search(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", who or "")
    return m.group(0) if m else None


def lookup(book: dict[str, dict[str, Any]] | None, who: str) -> dict[str, Any] | None:
    """The saved interviewer ({name, email, time_zone}) a name means: an exact name, else the one saved person
    whose name it fits ('Dana' -> 'Dana Lee' when no other Dana is saved)."""
    if not book:
        return None
    name = display_name(who or "")
    hit = book.get(fold_text(name))
    if hit:
        return hit
    fits = [r for r in book.values() if name_matches(name, r["name"])]
    return fits[0] if len(fits) == 1 else None


def email_for(who: str, book: dict[str, dict[str, Any]] | None = None) -> str | None:
    """An interviewer's email: the one typed with the name ('Dana <dana@x.com>'), else the saved one."""
    return email_of(who) or ((lookup(book, who) or {}).get("email"))


def interviewer_zone(t: dict[str, Any], who: str, book: dict[str, dict[str, Any]] | None = None) -> str | None:
    """The time zone an interviewer is in for this session: set on the session, else saved with their name."""
    own = _data(t).get("interviewer_tz")
    if isinstance(own, dict):
        hit = own.get(display_name(who))
        if hit is None:
            hit = next((z for n, z in own.items() if name_matches(display_name(who), n)), None)
        if hit:
            return hit
    return (lookup(book, who) or {}).get("time_zone")


def candidate_zone(t: dict[str, Any]) -> str | None:
    """The candidate's own time zone for this session: set on the session, else the one saved for them."""
    return _data(t).get("candidate_tz") or t.get("cand_zone") or None


def zone_label(dt: datetime) -> str:
    """'PST', 'IST', 'UTC-3' (a zone with no letters of its own reads as an offset)."""
    n = dt.tzname() or ""
    if n[:1] in ("+", "-"):
        digits = n[1:].replace(":", "")
        h, m = int(digits[:2] or 0), int(digits[2:4] or 0)
        return f"UTC{n[0]}{h}" + (f":{m:02d}" if m else "")
    return n


def _fmt(dt: datetime, fmt: str) -> str:
    return actions.day_text(dt, fmt)


def when_for(at: str, zone: str | None = None, all_day: bool = False) -> str:
    """A session time for one reader: in their zone first, the recruiter's in brackets when it differs ('Tue Nov
    10, 12:00 PM EST (9:00 AM PST)'). With no zone, or the same clock, just the recruiter's ('Tue Nov 10, 9:00 AM
    PST'). An all-day entry says so."""
    dt = to_dt(at)
    rec = as_local(dt)
    if all_day:
        return _fmt(rec, "%a %b %-d") + ", all day"
    full = "%a %b %-d, %-I:%M %p"
    z = zone_named(zone) if zone else None
    if z is None:
        return f"{_fmt(rec, full)} {zone_label(rec)}"
    other = dt.astimezone(z)
    if other.utcoffset() == rec.utcoffset() and zone_label(other) == zone_label(rec):
        return f"{_fmt(rec, full)} {zone_label(rec)}"
    mine = _fmt(rec, "%-I:%M %p" if rec.date() == other.date() else full)
    return f"{_fmt(other, full)} {zone_label(other)} ({mine} {zone_label(rec)})"


_EMAIL_FULL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def _zone_field(value: Any, label: str) -> str | None:
    from .store import zone_key  # here to keep the import list short
    text = str(value or "").strip()
    if not text:
        return None
    key = zone_key(text)
    if key is None:
        raise ValueError(f"I don't know the {label} time zone {text!r}. Pick one from the list.")
    return key


SUMMARY_LIMIT = 500  # characters in an interview's note or session name on the web form


def form_fields(form: Any) -> dict[str, Any]:
    """The interview extras of the web log form, checked: {interviewer, interviewer_email, interviewer_tz,
    candidate_tz, minutes}. Raises ValueError with a plain message, before anything is saved."""
    who = " ".join(str(form.get("interviewer") or "").split()) or None
    email = str(form.get("interviewer_email") or "").strip() or None
    if email and not _EMAIL_FULL.fullmatch(email):
        raise ValueError(f"{email!r} doesn't look like an email address. Nothing was saved.")
    if email and not who:
        raise ValueError("Add the interviewer's name too, so their email can be saved with it. Nothing was saved.")
    if email and len(interviewers(who)) != 1:
        raise ValueError("One email goes with one interviewer. Put one name in the interviewer box, or type each "
                         "as Name <email>. Nothing was saved.")
    raw = str(form.get("minutes") or "").strip()
    minutes = None
    if raw:
        if not raw.isdigit() or not 10 <= int(raw) <= 480:
            raise ValueError("Length is a number of minutes from 10 to 480. Nothing was saved.")
        minutes = int(raw)
    hours = " ".join(str(form.get("interviewer_hours") or "").split()) or None
    if hours:
        from .loops import parse_hours  # here: loops imports this module
        try:
            parse_hours(hours)
        except ValueError:
            raise ValueError("The interviewer's working hours look like 9-17 (start hour, then end hour, 24-hour "
                             "clock). Nothing was saved.") from None
        if not who or len(interviewers(who)) != 1:
            raise ValueError("Working hours go with one interviewer. Put one name in the interviewer box. "
                             "Nothing was saved.")
    return {"interviewer": who, "interviewer_email": email, "minutes": minutes,
            "interviewer_tz": _zone_field(form.get("interviewer_tz"), "interviewer's"),
            "interviewer_hours": hours,
            "candidate_tz": _zone_field(form.get("candidate_tz"), "candidate's")}


def with_length(body: str, minutes: int | None) -> str:
    """The summary with its length written the way sessions remember it: 'Onsite: coding (45 min)'."""
    if not minutes or _LENGTH.search(body or ""):
        return body
    return f"{(body or '').strip() or 'Interview'} ({minutes} min)"


def save_form_people(store: Store, fields: dict[str, Any]) -> None:
    """Keep the interviewer's email from the log form (their zone is kept when the interview is logged)."""
    if fields.get("interviewer_email") and fields.get("interviewer"):
        store.save_interviewer(display_name(fields["interviewer"]), fields["interviewer_email"])
    if fields.get("interviewer_hours") and fields.get("interviewer"):
        from .loops import save_person_hours
        save_person_hours(store, display_name(fields["interviewer"]), fields["interviewer_hours"])


def named(t: dict[str, Any]) -> list[str]:
    """The interviewers named on a session, each once, as written."""
    seen: dict[str, str] = {}
    for w in interviewers(actions.interviewer_of(t)):
        seen.setdefault(display_name(w).casefold(), w)
    return list(seen.values())


def _tokens(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", display_name(name or "").casefold())


def name_matches(given: str, full: str) -> bool:
    """'Priya' or 'Tia B.' loosely matches 'Priya Nair' or 'Tia Brandt': every word of the shorter name starts
    a word of the longer one, in any case."""
    a, b = _tokens(given), _tokens(full)
    if not a or not b:
        return False
    if len(a) > len(b):
        a, b = b, a
    return all(any(w.startswith(x) for w in b) for x in a)


def resolve_name(given: str, names: list[str]) -> tuple[str | None, list[str]]:
    """(the one name it means, []) or (None, the names it could be) or (None, []). An exact match (any case) wins."""
    exact = [n for n in names if display_name(n).casefold() == display_name(given).casefold()]
    if exact:
        return display_name(exact[0]), []
    hits = list(dict.fromkeys(display_name(n) for n in names if name_matches(given, n)))
    return (hits[0], []) if len(hits) == 1 else (None, hits)


def pick_interviewer(t: dict[str, Any], who: str | None) -> str | None:
    """The named interviewer of this session that `who` means ('Priya' -> 'Priya Nair'). A name that matches
    nobody on the session is kept as typed (someone who stood in); one that fits two raises, listing them."""
    who = " ".join((who or "").split())
    if not who:
        return None
    hit, many = resolve_name(who, named(t))
    if hit:
        return hit
    if many:
        raise ValueError(f"{quoted(who)} could be {', '.join(many[:-1])} or {many[-1]}. Say which one by their "
                         "full name. Nothing was saved.")
    return display_name(who)


def _key_for(by: str, names: list[str]) -> str:
    hit, _ = resolve_name(by, names)
    return (hit or display_name(by)).casefold()


def is_unlisted(t: dict[str, Any], by: str | None) -> bool:
    """Whether `by` is someone other than the interviewers named on this session. A session that names nobody
    has no one to be "not on" it, and feedback with no name is never unlisted."""
    names = named(t)
    who = " ".join((by or "").split())
    if not names or not who:
        return False
    hit, many = resolve_name(who, [display_name(n) for n in names])
    return hit is None and not many


UNLISTED_LABEL = "from someone not on this interview"


def verdicts(t: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{folded name: {by, verdict, at, source}}: each interviewer's latest verdict on this session ('' when
    nobody was named). A first name or short form ('Priya') counts as the named interviewer it fits."""
    names = [display_name(n) for n in named(t)]
    out: dict[str, dict[str, Any]] = {}
    for v in _data(t).get("verdicts") or []:
        if isinstance(v, dict) and v.get("verdict") in VERDICT_SCORE and not is_unlisted(t, v.get("by")):
            out[_key_for(v.get("by") or "", names)] = v
    return out


def rollup(words: list[str]) -> str | None:
    """The session's verdict from each interviewer's: all on one side gives the least sure of them (Strong yes and
    Yes is Yes), a split or any Mixed gives Mixed."""
    scores = [VERDICT_SCORE[w] for w in words if w in VERDICT_SCORE]
    if not scores:
        return None
    if all(x > 0 for x in scores):
        best = min(scores)
    elif all(x < 0 for x in scores):
        best = max(scores)
    else:
        return "mixed"
    return next(w for w, x in VERDICT_SCORE.items() if x == best)


def owed_by(t: dict[str, Any]) -> list[str]:
    """Named interviewers on this session with no feedback and no verdict of their own yet."""
    d = _data(t)
    if d.get("status") == "cancelled":
        return []
    names = [display_name(n) for n in named(t)]
    gave = {_key_for(f.get("by") or "", names) for f in d.get("feedback") or [] if isinstance(f, dict)}
    gave |= set(verdicts(t))
    return [display_name(w) for w in named(t) if display_name(w).casefold() not in gave]


def _verdict_key(value: str) -> str:
    key = re.sub(r"[\s_-]+", " ", (value or "").strip().lower()).strip(" .!")
    if key not in INTERVIEW_VERDICTS:
        raise ValueError("A verdict is one of: strong yes, yes, mixed, no, strong no.")
    return key


def give_verdict(store: Store, interview_id: int, verdict: str, interviewer: str | None = None,
                 note: str | None = None, *, candidate_id: int | None = None, by: str = "app") -> dict[str, Any]:
    """One interviewer's own verdict on a session (and their notes, saved as their feedback). The session's outcome
    becomes the roll-up of everyone's latest verdict, so a later change of mind updates it. Nothing is removed:
    an earlier verdict stays in the list. An AI app's verdict is on Changes, where Undo puts the session back."""
    key = _verdict_key(verdict)
    iv = store.get_interview(interview_id, candidate_id)
    if iv["status"] == "cancelled":
        raise ValueError("That interview was cancelled, so it has no verdict. Put it back first if it happened.")
    ts = now_iso()
    if iv["at"] > ts:
        raise ValueError("That interview hasn't happened yet, so it can't have a verdict. Record it after it takes "
                         "place, or change its time first.")
    names = named(iv)
    who = " ".join((interviewer or "").split()) or None
    if who and len(who) > 200:
        raise ValueError("The interviewer can be up to 200 characters. Name the person, not the whole invite.")
    if not who and len(names) == 1:
        who = names[0]
    if not who and len(names) > 1:
        raise ValueError("Say whose verdict this is: " + ", ".join(display_name(n) for n in names) + ".")
    if who:
        who = pick_interviewer(iv, who)
    data = dict(iv["data"])
    word = key.replace(" ", "_")
    body = (note or "").strip() or None
    current = verdicts(iv).get((who or "").casefold())
    if current and current["verdict"] == word and not body:
        return iv
    source = CHANGE_SOURCES.get(by, by)
    data["verdicts"] = [*(v for v in data.get("verdicts") or [] if isinstance(v, dict)),
                        {"by": who, "verdict": word, "at": ts, "source": source}]
    if body:
        if len(body) > 4000:
            raise ValueError(f"That feedback is too long ({len(body):,} characters). The limit is 4,000, so nothing "
                             "was saved.")
        notes = [f for f in data.get("feedback") or [] if isinstance(f, dict)]
        if not any((f.get("by") or "").casefold() == (who or "").casefold() and f.get("text") == body for f in notes):
            data["feedback"] = [*notes, {"by": who, "text": body, "at": ts, "source": source}]
    if data.get("verdict") is None and _legacy_verdict(data):
        data["outcome_note"] = interview_note(data)
    if is_unlisted(iv, who):  # kept, shown as from someone not on the interview, but not counted in the result
        combined = None
    else:
        combined = rollup([v["verdict"] for v in verdicts({**iv, "data": data}).values()]) or word
        data["verdict"], data["outcome"], data["outcome_at"] = combined, \
            INTERVIEW_VERDICTS[combined.replace("_", " ")], ts
    data["updated_by"] = source
    with store._write():
        store._conn.execute("UPDATE timeline SET data = ? WHERE id = ?", (json.dumps(data), interview_id))
        if by == "ai":
            store._log_ai("interview_outcome", candidate_id=iv["candidate_id"], job_id=iv.get("job_id"),
                          target_id=interview_id, old={"at": iv["at"], "data": iv["data"]},
                          new={"at": iv["at"], "data": data, "summary": iv.get("body")})
        store._conn.execute("UPDATE candidates SET last_contacted_at = ? WHERE id = ?"
                            " AND (last_contacted_at IS NULL OR last_contacted_at < ?)",
                            (iv["at"], iv["candidate_id"], iv["at"]))
    return store.get_interview(interview_id)


def owed_after(iv: dict[str, Any]) -> str:
    """' Still owed on that session: Tia Brandt.' or ' Nobody else owes feedback on that session.' ('' when it
    names no interviewers or hasn't happened yet)."""
    if not named(iv) or iv.get("at", "") > now_iso():
        return ""
    left = owed_by(iv)
    return (" Still owed on that session: " + ", ".join(left) + ".") if left else \
        " Nobody else owes feedback on that session."


def verdict_rows(t: dict[str, Any]) -> list[dict[str, Any]]:
    """Each named interviewer (and anyone else who gave a verdict) with their latest verdict label, or None."""
    vs = verdicts(t)
    rows = [{"name": display_name(n), "verdict": (vs.get(display_name(n).casefold()) or {}).get("verdict")}
            for n in named(t)]
    have = {r["name"].casefold() for r in rows}
    rows += [{"name": v.get("by") or "", "verdict": v["verdict"]} for k, v in vs.items() if k not in have]
    return [{**r, "label": VERDICT_LABELS.get(r["verdict"] or "")} for r in rows]


def verdicts_missing(t: dict[str, Any]) -> int:
    """Named interviewers who gave no verdict on a session that has others' verdicts (0 when nobody gave one)."""
    vs = verdicts(t)
    return sum(1 for n in named(t) if display_name(n).casefold() not in vs) if vs else 0


def tally(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """A finalist's roll-up across sessions that have taken place: {counts: [(label, n)], missing: n, text}.
    Per interviewer where they gave their own verdict, else the session's outcome; 'Strong yes 1, Mixed 1,
    1 not recorded'. sessions are Decide's interviews (with data)."""
    counts: dict[str, int] = {}
    missing = 0
    for s in sessions:
        if s.get("upcoming"):
            continue
        t = {"data": s.get("data") or {}}
        vs = verdicts(t)
        if vs:
            for v in vs.values():
                lab = VERDICT_LABELS[v["verdict"]]
                counts[lab] = counts.get(lab, 0) + 1
            missing += sum(1 for n in named(t) if display_name(n).casefold() not in vs)
        elif s.get("status") != "scheduled":
            counts[s["outcome"]] = counts.get(s["outcome"], 0) + 1
        else:
            missing += max(1, len(named(t)))
    order = {VERDICT_LABELS[w]: -x for w, x in VERDICT_SCORE.items()}
    pairs = sorted(counts.items(), key=lambda p: order.get(p[0], 10))
    text = ", ".join([f"{lab} {n}" for lab, n in pairs] + ([f"{missing} not recorded"] if missing else []))
    return {"counts": pairs, "missing": missing, "text": text}


# ------------------------------------------------------------ the feedback chase
CHASE_HOURS = 24  # a day after a session, Today asks for the feedback still owed
CHASE_DAYS = 14  # and stops asking two weeks after it


def request_text(who: str, candidate: str, session: str, day: str) -> tuple[str, str]:
    """(subject, body) of a short feedback request to one interviewer, in the recruiter's voice."""
    first = display_name(who).split(" ")[0] or "there"
    what = session if session and session != "Interview" else "interview"
    subject = f"Feedback on {candidate} ({what}, {day})"
    body = (f"Hi {first},\n\nCould you send me your feedback on {candidate} from the {what} on {day}? A few lines "
            "on how it went and your verdict (strong yes, yes, mixed, no or strong no) is plenty.\n\nThanks!")
    return subject, body


def mailto(who: str, subject: str, body: str) -> str:
    """A mailto link that opens the recruiter's own mail app with the request filled in. Nothing is sent here."""
    return f"mailto:{quote(email_of(who) or '', safe='@')}?subject={quote(subject)}&body={quote(body)}"


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def person_request(who: str, items: list[dict[str, Any]]) -> tuple[str, str]:
    """(subject, body) of ONE feedback request to one interviewer covering all the sessions they owe."""
    if len(items) == 1:
        i = items[0]
        return request_text(who, i["name"], i["session"], i["day"])
    first = display_name(who).split(" ")[0] or "there"
    names = list(dict.fromkeys(i["name"] for i in items))
    lines = "\n".join(f"- {i['name']}: {i['session']}, {i['day']}" for i in items)
    subject = "Feedback on " + _and(names)
    body = (f"Hi {first},\n\nCould you send me your feedback on these interviews? A few lines on how each went and "
            f"your verdict (strong yes, yes, mixed, no or strong no) is plenty.\n\n{lines}\n\nThanks!")
    return subject, body


def group_owed(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per interviewer from per-session entries {who, email, candidate_id, name, job_id, job,
    interview_id, session, at, day, chased}: {name, email, count, candidates, items, summary, subject, body,
    mailto, chased}. 'Priya' and 'Priya Shah' are one person when only one name fits. Most owed first."""
    known: list[str] = []
    canon: dict[str, str] = {}
    for n in sorted({e["who"] for e in entries}, key=lambda n: (-len(n), n.casefold())):
        hit, _ = resolve_name(n, known)
        canon[n] = hit or n
        if not hit:
            known.append(n)
    people: dict[str, list[dict[str, Any]]] = {}
    for e in sorted(entries, key=lambda e: (e["at"], e["interview_id"])):
        people.setdefault(canon[e["who"]], []).append(e)
    out = []
    for who, items in people.items():
        names = list(dict.fromkeys(i["name"] for i in items))
        subject, body = person_request(who, items)
        email = next((i["email"] for i in items if i.get("email")), None)
        out.append({"name": who, "email": email, "count": len(items), "candidates": names, "items": items,
                    "summary": f"{who} owes {len(items)}: " + ", ".join(names), "subject": subject, "body": body,
                    "mailto": mailto(f"{who} <{email}>" if email else who, subject, body),
                    "chased": any(i.get("chased", True) for i in items)})
    return sorted(out, key=lambda p: (-p["count"], p["name"].casefold()))


def _days_overdue(at: str, now: datetime | None = None) -> int:
    """Whole days past the point feedback was due (a day after the session), never below 0."""
    now = now or datetime.now(timezone.utc)
    return max(0, (now - (to_dt(at) + timedelta(hours=CHASE_HOURS))).days)


def owed_people(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """feedback_owed's loops, regrouped by the interviewer who owes: 'Priya Shah owes 3: Dana, Omar, Lena'."""
    return group_owed([{"who": o["name"], "email": o.get("email"), "candidate_id": r["candidate_id"], "name": r["name"],
                        "job_id": r["job_id"], "job": r["job"], "interview_id": o["interview_id"],
                        "session": o["session"], "at": o.get("at") or r["at"], "day": r["day"],
                        "days_overdue": _days_overdue(o.get("at") or r["at"]), "chased": o.get("chased", True)}
                       for r in rows for o in r["owed"]])


def feedback_owed(store: Store, now: datetime | None = None, job_id: int | None = None,
                  hours: int = CHASE_HOURS, days: int = CHASE_DAYS, recent: bool = False) -> list[dict[str, Any]]:
    """Loops with feedback still owed: sessions at least `hours` ago and at most `days` ago, for people still in
    play on an open job, where a named interviewer has given no feedback or verdict. One row per loop:
    {key, candidate_id, name, job_id, job, day, at, owed: [{name, email, session, interview_id, subject, body,
    mailto, chased}]}, oldest first. recent=True also lists sessions that took place under `hours` ago, as Decide
    does, each owed entry with chased False (Today doesn't ask for those yet)."""
    now = now or datetime.now(timezone.utc)
    book = store.interviewer_book()
    due = (now - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    hi = now.replace(microsecond=0).isoformat() if recent else due
    lo = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    where = "AND t.job_id = ?" if job_id is not None else "AND t.job_id IS NOT NULL"
    rows = store._rows(
        f"""SELECT t.*, c.name AS candidate_name, j.title AS job_title, j.company AS job_company FROM timeline t
            JOIN candidates c ON c.id = t.candidate_id JOIN jobs j ON j.id = t.job_id
            JOIN applications a ON a.candidate_id = t.candidate_id AND a.job_id = t.job_id
            JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
            WHERE t.kind = 'interview' AND t.at >= ? AND t.at <= ? AND j.status = 'open' AND s.outcome = 'open'
              AND json_extract(t.data, '$.interviewer') IS NOT NULL {where}
              AND coalesce(json_extract(t.data, '$.status'), 'scheduled') <> 'cancelled' ORDER BY t.at, t.id""",
        (lo, hi, *([job_id] if job_id is not None else [])))
    out = []
    for g in actions.group_loops(rows):
        first = g[0]
        day = actions.local_time(first["at"], "%a %b %-d")
        owed = []
        for t in g:
            session = actions.interview_name(t) or "Interview"
            for who in named(t):
                if display_name(who) not in owed_by(t):
                    continue
                subject, body = request_text(who, first["candidate_name"], session, day)
                email = email_for(who, book)
                owed.append({"name": display_name(who), "email": email, "session": session,
                             "interview_id": t["id"], "at": t["at"], "subject": subject, "body": body,
                             "mailto": mailto(f"{display_name(who)} <{email}>" if email else who, subject, body),
                             "chased": t["at"] <= due})
        if owed:
            out.append({"key": f"feedback_owed:{first['candidate_id']}:{first['job_id']}:"
                               f"{actions.loop_day(first).isoformat()}",
                        "candidate_id": first["candidate_id"], "name": first["candidate_name"],
                        "job_id": first["job_id"], "job": actions.job_label(first["job_title"], first.get("job_company")),
                        "day": day, "at": first["at"], "owed": owed})
    return out


def today_items(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """Today's feedback cards. Each interviewer gets ONE request covering everything they owe, and each
    candidate shows up once: people who owe on the same candidates (or one person across several) are one card.
    'Priya Shah owes feedback on 3 interviews', or 'Kenji Example is waiting on feedback from Dana Lee, Priya
    Nair and Sam Roe'. Each person on a card has their own request to send (subject, body, mailto)."""
    rows = feedback_owed(store, now)
    people = owed_people(rows)
    names = [p["name"] for p in people]
    by_name = {p["name"]: p for p in people}
    parent: dict[str, str] = {}

    def find(k: str) -> str:
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for p in people:  # a person and each candidate they owe on belong to one card
        for i in p["items"]:
            parent[find(f"p:{p['name']}")] = find(f"c:{i['candidate_id']}:{i['job_id']}")
    cards: dict[str, list[dict[str, Any]]] = {}
    for p in people:
        cards.setdefault(find(f"p:{p['name']}"), []).append(p)
    out = []
    for group in cards.values():
        group.sort(key=lambda p: (-p["count"], p["name"].casefold()))
        items = sorted((i for p in group for i in p["items"]), key=lambda i: (i["at"], i["interview_id"]))
        first = items[0]
        cands = list(dict.fromkeys(i["name"] for i in items))
        jobs = list(dict.fromkeys(i["job"] for i in items))
        one = len(cands) == 1 and len({i["job_id"] for i in items}) == 1
        who = [p["name"] for p in group]
        if len(group) == 1:
            p = group[0]
            title = f"{p['name']} owes feedback on {p['count']} interview{'' if p['count'] == 1 else 's'}"
            key = "feedback_owed:" + re.sub(r"\W+", "-", p["name"].casefold()).strip("-")
        else:
            title = (f"{cands[0]} is waiting on feedback from {_and(who)}" if len(cands) == 1 else
                     f"{len(who)} people owe feedback on {len(cands)} candidates")
            key = "feedback_owed:" + "+".join(re.sub(r"\W+", "-", n.casefold()).strip("-") for n in sorted(who))
        out.append({"key": key, "kind": "feedback_owed", "rank": 1, "when": first["at"], "who": ", ".join(who),
                    "title": title,
                    "detail": " · ".join(x for x in (", ".join(jobs), "On " + _and(cands)) if x),
                    "people": [{"name": p["name"], "email": p["email"], "subject": p["subject"], "body": p["body"],
                                "mailto": p["mailto"], "count": p["count"],
                                "items": [{"name": i["name"], "session": i["session"], "day": i["day"],
                                           "interview_id": i["interview_id"], "candidate_id": i["candidate_id"]}
                                          for i in p["items"]]} for p in group],
                    "owed": [{"name": i["name"], "session": i["session"], "day": i["day"], "who": i["who"],
                              "interview_id": i["interview_id"], "candidate_id": i["candidate_id"]} for i in items],
                    "subject": group[0]["subject"] if len(group) == 1 else None,
                    "body": group[0]["body"] if len(group) == 1 else None,
                    "mailto": group[0]["mailto"] if len(group) == 1 else None,
                    "email": group[0]["email"] if len(group) == 1 else None,
                    "candidate_id": first["candidate_id"] if one else None, "job_id": first["job_id"] if one else None,
                    "href": (f"/jobs/{first['job_id']}/decide#c{first['candidate_id']}" if one else "/interviews")})
    return sorted(out, key=lambda i: i["when"])


def feedback_owed_text(rows: list[dict[str, Any]]) -> str:
    """list_feedback_owed's text: each loop and who still owes feedback, with the request to send."""
    rule = (f"This covers sessions from the last {CHASE_DAYS} days on open jobs. Today asks for feedback "
            f"{CHASE_HOURS} hours after a session; one newer than that is listed as not chased yet.")
    if not rows:
        return "Nobody owes interview feedback right now. " + rule
    n = sum(len(r["owed"]) for r in rows)
    fresh = sum(1 for r in rows for o in r["owed"] if not o.get("chased", True))
    people = owed_people(rows)
    lines = [f"{n} feedback request{'' if n == 1 else 's'} owed across {len(rows)} loop{'' if len(rows) == 1 else 's'}"
             f", from {len(people)} {'person' if len(people) == 1 else 'people'}"
             + (f" ({fresh} not chased yet)" if fresh else "") + ". Draft ONE nudge per interviewer covering all "
             "their candidates (not one per candidate) for the recruiter to send from their own mail. Nothing is "
             "sent by Talent Search. " + rule]
    lines += ["By interviewer:"] + [f"- {p['summary']}" + (f" <{p['email']}>" if p["email"] else "")
                                    + f". Suggested subject: {p['subject']}" for p in people]
    for r in rows:
        lines.append(f"## {r['name']} (candidate id {r['candidate_id']}), {r['job']}, {r['day']}")
        lines += [f"- {o['name']}" + (f" <{o['email']}>" if o["email"] else "") + f" owes feedback on {o['session']} "
                  f"(interview id {o['interview_id']})"
                  + ("" if o.get("chased", True) else f", not chased yet (under {CHASE_HOURS} hours ago)")
                  for o in r["owed"]]
    return "\n".join(lines)


def future_note(at: str | None, kind: str, now: datetime | None = None) -> str | None:
    """A warning for a call, email or note dated later than now (an interview may be booked ahead): it moves
    'last contacted' into the future."""
    dt = to_dt(at) if at else None
    now = now or datetime.now(timezone.utc)
    if not dt or kind == "interview" or dt <= now + timedelta(minutes=5):
        return None
    return (f"That {kind} is dated {actions.local_time(at, actions.IV_TIME)}, which is later than now. If it hasn't "
            "happened yet, log it after it does, or log it as an interview to book it.")


__all__ = ["form_fields", "with_length", "save_form_people", "email_for", "interviewer_zone", "candidate_zone", "when_for", "zone_label", "lookup", "ics_for_week",
           "CHASE_DAYS", "CHASE_HOURS", "VERDICT_CHOICES", "feedback_owed", "feedback_owed_text", "future_note",
           "give_verdict", "group_owed", "interviewers", "name_matches", "named", "owed_by", "owed_people",
           "pick_interviewer", "resolve_name", "session_minutes", "verdicts_missing", "rollup", "tally", "today_items", "verdict_rows",
           "verdicts", "week", "week_day", "week_start", "week_text"]


# ------------------------------------------------------------ the month
MONTH_CHIPS = 3  # sessions listed in a day's box before "+N more"
_DONE = ("passed", "not_passed", "no_show", "mixed")


def month_first(value: str | None, today: date | None = None) -> date:
    """The first day of the month to show: '2026-11' (or any day in it), else this month."""
    today = today or as_local(datetime.now(timezone.utc)).date()
    v = (value or "").strip()
    if not v:
        return today.replace(day=1)
    try:
        d = date.fromisoformat(v + "-01") if re.fullmatch(r"\d{4}-\d{2}", v) else date.fromisoformat(v)
    except (ValueError, OverflowError):
        raise ValueError("month is like 2026-11.") from None
    return _in_range(d.replace(day=1))


def _month_shift(first: date, by: int) -> date:
    n = first.year * 12 + first.month - 1 + by
    return date(n // 12, n % 12 + 1, 1)


def _chip_state(x: dict[str, Any]) -> str:
    if x["status"] == "cancelled":
        return "cancelled"
    if x["status"] in _DONE:
        return "done"
    if x["needs_outcome"] or x["owed"]:
        return "owes"
    return "upcoming"


def month(store: Store, first_day: date | None = None, today: date | None = None) -> dict[str, Any]:
    """{first, label, prev, next, today, weeks: [[day]], total, clashes}. A day is {day, in_month, today, weekend,
    week, sessions, shown, more}: every session that starts that day on the recruiter's clock (cancelled ones
    too, marked), in time order, `shown` the first MONTH_CHIPS of them and `more` how many are left. Each
    session is {interview_id, at, name, candidate_id, candidate, job, state, clash, href, ...} with state one of
    upcoming, done, owes (it needs an outcome or feedback) or cancelled. Weeks run Monday to Sunday and cover
    whole weeks, so the first and last rows can hold days of the neighbouring months."""
    today = today or as_local(datetime.now(timezone.utc)).date()
    first = _in_range((first_day or today).replace(day=1))
    grid_start = first - timedelta(days=first.weekday())
    nxt = _month_shift(first, 1)
    grid_end = nxt + timedelta(days=(7 - nxt.weekday()) % 7)  # the Monday after the last row
    rows = _session_rows(store, _midnight(grid_start), _midnight(grid_end), cancelled=True)
    live = [t for t in rows if actions._iv_data(t).get("status") != "cancelled"]
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    chase = _chase_pairs(store, {(t["candidate_id"], t.get("job_id")) for t in live})
    clashes = _clashes(live)
    flagged = {i for c in clashes for i in c["ids"]}
    by_day: dict[date, list[dict[str, Any]]] = {}
    for t in rows:
        x = _owing(t, now, chase)
        x.update({"candidate_id": t["candidate_id"], "candidate": t["candidate_name"],
                  "job": actions.job_label(t.get("job_title"), t.get("job_company")) if t.get("job_title") else None,
                  "clash": x["interview_id"] in flagged})
        x["state"] = _chip_state(x)
        by_day.setdefault(actions._local_day(t["at"]), []).append(x)
    for ss in by_day.values():  # the same person twice in a day: say which session each chip is
        counts: dict[int, int] = {}
        for x in ss:
            counts[x["candidate_id"]] = counts.get(x["candidate_id"], 0) + 1
        for x in ss:
            x["tag"] = (x["interviewer"] or x["name"]) if counts[x["candidate_id"]] > 1 else ""
    weeks = []
    day = grid_start
    while day < grid_end:
        row = []
        for _ in range(7):
            ss = by_day.get(day, [])
            row.append({"day": day, "in_month": day.month == first.month, "today": day == today,
                        "weekend": day.weekday() >= 5, "week": (day - timedelta(days=day.weekday())).isoformat(),
                        "week_label": actions.day_text(day - timedelta(days=day.weekday()), "%b %-d"),
                        "sessions": ss, "shown": ss[:MONTH_CHIPS], "more": max(0, len(ss) - MONTH_CHIPS)})
            day += timedelta(days=1)
        weeks.append(row)
    inside = [x for w in weeks for d in w if d["in_month"] for x in d["sessions"] if x["state"] != "cancelled"]
    ids = {x["interview_id"] for x in inside}
    return {"first": first, "label": actions.day_text(first, "%B %Y"), "prev": _month_shift(first, -1),
            "next": _month_shift(first, 1), "today": today,
            "prev_label": actions.day_text(_month_shift(first, -1), "%B %Y"),
            "next_label": actions.day_text(_month_shift(first, 1), "%B %Y"), "weeks": weeks, "total": len(inside),
            "clashes": [c for c in clashes if set(c["ids"]) & ids], "this_month": first == today.replace(day=1)}


# ------------------------------------------------------------ .ics for the recruiter's own calendar
_FOREVER = "9999-12-31T00:00:00+00:00"


def _ics_text(value: Any) -> str:
    """Text for an iCalendar value: backslash, semicolon, comma and line breaks escaped, other control
    characters dropped (RFC 5545 section 3.3.11)."""
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line: str) -> list[str]:
    """Lines over 75 bytes continue on the next line after a space (never inside a multi-byte character)."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return [out[0], *(" " + p for p in out[1:])]


def _ics_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_LEN_SUFFIX = re.compile(r"\s*\(\s*\d{1,3}\s*(?:min|mins|minutes|m)\s*\)\s*$", re.I)


def ics_summary(t: dict[str, Any]) -> str:
    """'System design: Grace Example (Dana Lee)': the session, the candidate and who runs it, so every session
    on a day has a title of its own."""
    session = _LEN_SUFFIX.sub("", actions.interview_name(t) or "").strip() or "Interview"
    who = actions.interviewer_of(t)
    return f"{session}: {t['candidate_name']}" + (f" ({who})" if who else "")


def ics_event(t: dict[str, Any], stamp: datetime, book: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """One VEVENT for an interview row (from _session_rows): UID stable per interview, times in UTC, as long as
    the session runs; an all-day entry uses DATE values. The summary names the session, candidate and
    interviewer. The description has the job, the interviewer and each person's own local time. Never notes."""
    start = to_dt(t["at"])
    d = actions._iv_data(t)
    all_day = bool(d.get("all_day"))
    job = actions.job_label(t.get("job_title"), t.get("job_company")) if t.get("job_title") else ""
    who = actions.interviewer_of(t)
    notes = [f"Job: {job}" if job else "", f"Interviewer: {who}" if who else ""]
    if not all_day:
        cz = candidate_zone(t)
        if cz:
            notes.append(f"{t['candidate_name']}'s time: {when_for(t['at'], cz)}")
        for w in interviewers(who):
            z = interviewer_zone(t, w, book)
            if z:
                notes.append(f"{display_name(w)}'s time: {when_for(t['at'], z)}")
    desc = "\n".join(x for x in notes if x)
    # DTSTAMP and LAST-MODIFIED come from the interview itself (when it was last changed, else when it was
    # logged) and SEQUENCE is its edit count, so an unchanged interview downloads the same every time and a
    # calendar app only sees a change when there was one.
    seq = int(d.get("edits") or 0)
    made = to_dt(d.get("updated_at") or t.get("created_at")) or stamp
    stamp = made
    if all_day:
        day = date.fromisoformat(d.get("all_day_date") or as_local(start).date().isoformat())
        when = [f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}"]
    else:
        when = [f"DTSTART:{_ics_time(start)}", f"DTEND:{_ics_time(start + timedelta(minutes=session_minutes(t)))}"]
    lines = ["BEGIN:VEVENT", f"UID:interview-{t['id']}@talent-search.local", f"DTSTAMP:{_ics_time(stamp)}",
             f"LAST-MODIFIED:{_ics_time(stamp)}", f"SEQUENCE:{seq}", *when, "SUMMARY:" + _ics_text(ics_summary(t))]
    if desc:
        lines.append("DESCRIPTION:" + _ics_text(desc))
    lines.append("STATUS:" + ("CANCELLED" if d.get("status") == "cancelled" else "CONFIRMED"))
    lines.append("END:VEVENT")
    return lines


def ics_calendar(rows: list[dict[str, Any]], now: datetime | None = None,
                 book: dict[str, dict[str, Any]] | None = None) -> str:
    """An iCalendar file (CRLF line ends) holding these interview rows."""
    stamp = now or datetime.now(timezone.utc)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Talent Search//Interviews//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Talent Search interviews"]
    for t in rows:
        lines += ics_event(t, stamp, book)
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in _ics_fold(line)) + "\r\n"


def ics_for_month(store: Store, first: date | None, cancelled: bool | None = None) -> str:
    """The interviews that start in that month (on the recruiter's clock, cancelled ones marked so a calendar app
    drops them), or every upcoming one when first is None (cancelled left out, and sessions already over today,
    unless `cancelled` is True)."""
    if first is not None:
        lo, hi = _midnight(first), _midnight(_month_shift(_in_range(first), 1))
        return ics_calendar(_session_rows(store, lo, hi, cancelled=True if cancelled is None else cancelled),
                            book=store.interviewer_book())
    now = datetime.now(timezone.utc)
    rows = _session_rows(store, _midnight(as_local(now).date()), _FOREVER, cancelled=bool(cancelled))
    return ics_calendar([t for t in rows if to_dt(t["at"]) + timedelta(minutes=session_minutes(t)) > now],
                        book=store.interviewer_book())


def ics_for_week(store: Store, day: date) -> str:
    """The interviews in the week (Monday to Sunday, on the recruiter's clock) that holds `day`, cancelled ones
    marked."""
    monday = week_start(_in_range(day))
    rows = _session_rows(store, _midnight(monday), _midnight(monday + timedelta(days=7)), cancelled=True)
    return ics_calendar(rows, book=store.interviewer_book())


def ics_for_interview(store: Store, candidate_id: int, entry_id: int) -> str:
    """One interview as a calendar file; LookupError when it isn't on that profile."""
    t = store._row(
        """SELECT t.*, c.name AS candidate_name, j.title AS job_title, j.company AS job_company,
                  pz.time_zone AS cand_zone FROM timeline t
           JOIN candidates c ON c.id = t.candidate_id LEFT JOIN jobs j ON j.id = t.job_id
           LEFT JOIN person_zones pz ON pz.candidate_id = t.candidate_id
           WHERE t.kind = 'interview' AND t.id = ? AND t.candidate_id = ?""", (entry_id, candidate_id))
    if t is None:
        raise LookupError(f"No interview with id {entry_id} on this profile")
    return ics_calendar([t], book=store.interviewer_book())
