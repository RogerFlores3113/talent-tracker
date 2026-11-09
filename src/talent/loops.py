"""The loop planner: for a job, a template of the sessions an onsite loop needs (system design, pairing,
behavioral) and who may run each, saved in settings. For a candidate it proposes a slot for every session over the
next working days, inside the recruiter's working hours, avoiding what the interviewers (and the candidate) already
have booked, with warnings for load, swap-in suggestions when someone is busy or out, and invite text to paste.

Planning changes nothing and writes to no calendar. Booking logs the sessions as interviews, the same way
log_activity does (an AI app's bookings sit on Changes, where each can be undone)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from . import actions, interviews
from .store import NotFound, Store, _zone_or_error, as_local, now_iso, shown_zone, to_dt, zone_named

TEMPLATE_PREFIX = "loop_template:"  # settings key per job
MAX_SESSIONS = 8
MAX_WHO = 8
DEFAULT_HOURS = (9, 17)
STEP = 30  # slots start on the half hour
GAP = 15  # a short break between one session and the next for the candidate
MAX_PER_DAY = 3  # more sessions than this in a day is flagged as heavy for an interviewer
HORIZON_DAYS = 10  # working days searched


# ------------------------------------------------------------ the template
def parse_hours(text: str | None) -> tuple[int, int]:
    """'9-17' or '9 to 17' -> (9, 17). Empty is the default 9 to 5."""
    t = (text or "").strip()
    if not t:
        return DEFAULT_HOURS
    m = re.fullmatch(r"(\d{1,2})\s*(?:-|to|until)\s*(\d{1,2})", t.lower())
    if not m or not (0 <= int(m.group(1)) < int(m.group(2)) <= 24):
        raise ValueError("Working hours look like 9-17 (start hour, then end hour, 24-hour clock).")
    return int(m.group(1)), int(m.group(2))


PERSON_HOURS_KEY = "interviewer_hours"  # settings row: {folded name: {"name": ..., "hours": [start, end]}}
INTERVIEWER_DEFAULT_HOURS = (9, 17)  # an interviewer's own working hours, on their clock, until set


def person_hours_book(store: Store) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(store.setting(PERSON_HOURS_KEY) or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict) and isinstance(v.get("hours"), list)} \
        if isinstance(raw, dict) else {}


def person_hours(book: dict[str, dict[str, Any]], who: str | None) -> tuple[int, int] | None:
    """The working hours saved for an interviewer ('Dana' finds 'Dana Lee' when only one fits), or None."""
    if not who:
        return None
    from .store import fold_text
    name = interviews.display_name(who)
    hit = book.get(fold_text(name))
    if hit is None:
        fits = [r for r in book.values() if interviews.name_matches(name, r.get("name") or "")]
        hit = fits[0] if len(fits) == 1 else None
    if not hit:
        return None
    try:
        return int(hit["hours"][0]), int(hit["hours"][1])
    except (ValueError, TypeError, IndexError):
        return None


def save_person_hours(store: Store, who: str, text: str | None) -> tuple[int, int] | None:
    """Keep an interviewer's working hours ('8-16'). Empty clears them (back to 9-17). Raises on a bad value."""
    from .store import fold_text
    name = interviews.display_name(who or "")
    if not name:
        raise ValueError("Give the interviewer's name to save their working hours.")
    book = person_hours_book(store)
    key = fold_text(name)
    if not (text or "").strip():
        if book.pop(key, None) is None:
            return None
        hours = None
    else:
        hours = parse_hours(text)
        book[key] = {"name": name, "hours": list(hours)}
    actions._put_setting(store, PERSON_HOURS_KEY, json.dumps(book))
    return hours


def parse_template(text: str) -> list[dict[str, Any]]:
    """One session per line: 'System design | 60 | Priya Nair, Sam Okoye' (minutes and the names are optional)."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0]
        if not name or len(name) > 60:
            raise ValueError("Each session needs a name of up to 60 characters, like System design.")
        minutes = interviews.SESSION_MINUTES
        who_part = parts[1] if len(parts) > 1 else ""
        if len(parts) > 1 and re.fullmatch(r"\d{1,3}\s*(?:min|mins|minutes|m)?", parts[1], re.I):
            minutes = int(re.match(r"\d+", parts[1]).group(0))
            who_part = parts[2] if len(parts) > 2 else ""
        if not 15 <= minutes <= 240:
            raise ValueError(f"{name}: a session runs 15 to 240 minutes.")
        who = [w for w in interviews.interviewers(who_part)]
        if len(who) > MAX_WHO or any(len(w) > 80 for w in who):
            raise ValueError(f"{name}: list up to {MAX_WHO} interviewers, one short name each.")
        if any(s["name"].casefold() == name.casefold() for s in out):
            raise ValueError(f"{name} is listed twice. Give each session its own name.")
        out.append({"name": name, "minutes": minutes, "interviewers": who})
    if len(out) > MAX_SESSIONS:
        raise ValueError(f"A loop template can have up to {MAX_SESSIONS} sessions.")
    return out


def template_text(sessions: list[dict[str, Any]]) -> str:
    return "\n".join(f"{s['name']} | {s['minutes']} | {', '.join(s['interviewers'])}".rstrip(" |") for s in sessions)


def get_template(store: Store, job_id: int) -> dict[str, Any] | None:
    """{sessions: [{name, minutes, interviewers}], hours: [start, end]} or None when the job has none."""
    raw = store.setting(f"{TEMPLATE_PREFIX}{job_id}")
    try:
        t = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(t, dict) or not t.get("sessions"):
        return None
    return {"sessions": t["sessions"], "hours": list(t.get("hours") or DEFAULT_HOURS)}


def save_template(store: Store, job_id: int, text: str, hours: str | None = None) -> dict[str, Any]:
    store.get_job(job_id)
    sessions = parse_template(text)
    start, end = parse_hours(hours)
    if not sessions:
        actions._drop_setting(store, f"{TEMPLATE_PREFIX}{job_id}")
        return {"sessions": [], "hours": [start, end]}
    actions._put_setting(store, f"{TEMPLATE_PREFIX}{job_id}", json.dumps({"sessions": sessions, "hours": [start, end]}))
    return {"sessions": sessions, "hours": [start, end]}


def clean_sessions(sessions: Any) -> list[dict[str, Any]]:
    """Sessions an AI app sends ([{label, minutes, interviewers}], or the template's own text) as saved sessions."""
    if isinstance(sessions, str):
        return parse_template(sessions)
    if not isinstance(sessions, (list, tuple)) or not sessions:
        raise ValueError("Send at least one session, like {\"label\": \"System design\", \"minutes\": 60, "
                         "\"interviewers\": [\"Priya Nair\", \"Sam Okoye\"]}.")
    lines = []
    for x in sessions:
        if isinstance(x, str):
            lines.append(x)
            continue
        if not isinstance(x, dict):
            raise ValueError("Each session is an object with a label, minutes and interviewers.")
        name = str(x.get("label") or x.get("name") or x.get("session") or "").strip()
        who = x.get("interviewers") if x.get("interviewers") is not None else x.get("interviewer")
        who = ", ".join(str(w).strip() for w in who if str(w).strip()) if isinstance(who, (list, tuple)) else str(who or "")
        if "|" in name or "|" in who:
            raise ValueError("Leave the | character out of session names and interviewer names.")
        mins = x.get("minutes") if x.get("minutes") is not None else x.get("duration")
        try:
            mins = int(float(mins)) if mins not in (None, "") else interviews.SESSION_MINUTES
        except (TypeError, ValueError):
            raise ValueError(f"{name or 'A session'}: minutes is a number between 15 and 240.") from None
        lines.append(f"{name} | {mins} | {who}")
    return parse_template("\n".join(lines))


def ai_set_template(store: Store, job_id: int, sessions: Any, hours: str | None = None) -> dict[str, Any]:
    """The AI app sets a job's loop template (never removes one or any interview). One logged AI action
    (loop_template), so it shows on Changes and Undo puts the old template back."""
    store.get_job(job_id)
    clean = clean_sessions(sessions)
    if not clean:
        raise ValueError("Send at least one session.")
    old = get_template(store, job_id)
    start, end = parse_hours(hours) if hours else (tuple(old["hours"]) if old else DEFAULT_HOURS)
    new = save_template(store, job_id, template_text(clean), f"{start}-{end}")
    with store._write():
        action = store._log_ai("loop_template", job_id=job_id, old={"template": old}, new={"template": new},
                               body=f"{len(clean)} session{'' if len(clean) == 1 else 's'}")
    return {"sessions": new["sessions"], "hours": new["hours"], "replaced": old is not None, "action_id": action}


def ai_blocker(store: Store, a: dict[str, Any]) -> str | None:
    now = get_template(store, a["job_id"]) if a.get("job_id") else None
    return None if now == (a.get("new_data") or {}).get("template") else "the loop template was changed again since"


def ai_undo(store: Store, a: dict[str, Any]) -> str:
    reason = ai_blocker(store, a)
    if reason:
        raise ValueError(reason[:1].upper() + reason[1:] + ".")
    old = (a.get("old_data") or {}).get("template")
    with store._write():
        if old:
            store._conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"{TEMPLATE_PREFIX}{a['job_id']}", json.dumps(old)))
        else:
            store._conn.execute("DELETE FROM settings WHERE key = ?", (f"{TEMPLATE_PREFIX}{a['job_id']}",))
        store._conn.execute("UPDATE ai_actions SET undone_at = ? WHERE id = ?", (now_iso(), a["id"]))
    return a["summary"]


# ------------------------------------------------------------ what is already booked
@dataclass
class _Cal:
    """Booked sessions in a window, plus the ones this plan adds as it goes."""
    entries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, store: Store, lo: datetime, hi: datetime) -> "_Cal":
        rows = store._rows(
            """SELECT t.*, c.name AS candidate_name FROM timeline t JOIN candidates c ON c.id = t.candidate_id
               WHERE t.kind = 'interview' AND t.at >= ? AND t.at < ?
                 AND coalesce(json_extract(t.data, '$.status'), 'scheduled') <> 'cancelled' ORDER BY t.at""",
            (lo.replace(microsecond=0).isoformat(), hi.replace(microsecond=0).isoformat()))
        cal = cls()
        for t in rows:
            s = to_dt(t["at"])
            cal.add(s, s + timedelta(minutes=interviews.session_minutes(t)),
                    [interviews.display_name(w) for w in interviews.interviewers(actions.interviewer_of(t))],
                    t["candidate_id"], t["candidate_name"], actions.interview_name(t) or "Interview")
        return cal

    def add(self, s: datetime, e: datetime, names: list[str], cid: int, cname: str, what: str) -> None:
        self.entries.append({"s": s, "e": e, "names": names, "cid": cid, "cname": cname, "what": what})

    def busy_for(self, name: str, s: datetime, e: datetime) -> dict[str, Any] | None:
        return next((x for x in self.entries if x["s"] < e and s < x["e"]
                     and any(interviews.name_matches(name, n) for n in x["names"])), None)

    def candidate_busy(self, cid: int, s: datetime, e: datetime) -> dict[str, Any] | None:
        return next((x for x in self.entries if x["cid"] == cid and x["s"] < e and s < x["e"]), None)

    def count(self, name: str, day: date) -> int:
        return sum(1 for x in self.entries if as_local(x["s"]).date() == day
                   and any(interviews.name_matches(name, n) for n in x["names"]))


def _at(day: date, minute: int) -> datetime:
    """`minute` minutes after midnight on `day` on the recruiter's clock, in UTC."""
    naive = datetime.combine(day, time.min) + timedelta(minutes=minute)
    zone = shown_zone()
    return (naive.replace(tzinfo=zone) if zone is not None else naive.astimezone()).astimezone(timezone.utc)


# ------------------------------------------------------------ US public holidays (built in, no network)
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7, weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """A holiday on a Saturday is taken on the Friday before, one on a Sunday on the Monday after."""
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def us_holidays(year: int) -> dict[date, str]:
    """The major US public holidays of a year (the days offices close), by the day they are taken. Plain rules,
    no network: New Year's Day, Martin Luther King Jr. Day, Presidents' Day, Memorial Day, Juneteenth,
    Independence Day, Labor Day, Thanksgiving and the day after, Christmas Day."""
    thanks = _nth_weekday(year, 11, 3, 4)
    out = {_observed(date(year, 1, 1)): "New Year's Day", _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
           _nth_weekday(year, 2, 0, 3): "Presidents' Day", _last_weekday(year, 5, 0): "Memorial Day",
           _observed(date(year, 6, 19)): "Juneteenth", _observed(date(year, 7, 4)): "Independence Day",
           _nth_weekday(year, 9, 0, 1): "Labor Day", thanks: "Thanksgiving",
           thanks + timedelta(days=1): "the day after Thanksgiving", _observed(date(year, 12, 25)): "Christmas Day"}
    return {d: n for d, n in out.items() if d.year == year}


def us_holiday(day: date) -> str | None:
    """The name of the US public holiday `day` is, or None."""
    return us_holidays(day.year).get(day)


def _working_days(first: date, n: int) -> list[date]:
    """The next n weekdays from `first`, skipping US public holidays."""
    out, d = [], first
    while len(out) < n:
        if d.weekday() < 5 and not us_holiday(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _when(dt: datetime) -> str:
    return actions.local_time(dt.replace(microsecond=0).isoformat(), actions.IV_TIME)


_OFFSET_TAIL = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$", re.I)


def _on_my_clock(start: str | None) -> str | None:
    """A start with an offset ("2026-11-10T10:00:00-08:00") is that moment, shown on the recruiter's clock
    ("2026-11-10T18:00" in UTC), so the day and the not-before time are read where the recruiter is."""
    text = (start or "").strip()
    if len(text) > 11 and _OFFSET_TAIL.search(text):
        try:
            dt = datetime.fromisoformat(re.sub(r"(?i)z$", "+00:00", text))
        except ValueError:
            return start
        if dt.tzinfo is not None:
            return as_local(dt).strftime("%Y-%m-%dT%H:%M")
    return start


def zone_line(store: Store) -> tuple[str, str]:
    """(the zone times are shown in, where it came from): the recruiter's saved zone, else the company's own when it
    is the only one (own-company mode), else this computer's. Used so a plan says whose clock it is on."""
    from . import reports
    saved = reports.user_zone(store)
    if saved:
        return saved, "the time zone saved in Settings"
    other = reports.fallback_zone(store)
    if other:
        return other, "your company's time zone"
    z = shown_zone()
    name = getattr(z, "key", None) if z is not None else None
    if not name:
        name = datetime.now().astimezone().tzname() or "this computer's clock"
    return name, "this computer's time zone, because none is saved in Settings"


def _first_day(start: str | None, now: datetime) -> date:
    """The day to start looking: the one asked for (or tomorrow), moved past a weekend or a US holiday."""
    if start and start.strip():
        try:
            d = date.fromisoformat(start.strip()[:10])
        except ValueError:
            raise ValueError("start is a day like 2026-11-09.") from None
    else:
        d = as_local(now).date() + timedelta(days=1)
    while d.weekday() >= 5 or us_holiday(d):
        d += timedelta(days=1)
    return d


def _skipped_holidays(asked: date, last: date) -> list[str]:
    """'Thanksgiving (Thu Nov 26)' for each weekday US holiday from `asked` to `last`, which the plan skips."""
    out, d = [], asked
    while d <= last:
        name = us_holiday(d)
        if name and d.weekday() < 5:
            out.append(f"{name} ({actions.day_text(d)})")
        d += timedelta(days=1)
    return out


def _clock(dt: datetime) -> str:
    return actions.day_text(dt, "%-I:%M %p")


def _odd_hours(name: str, zone: str | None, s: datetime, e: datetime) -> str | None:
    """'Dana Lee would be at 5:00 AM GMT (early) in their time zone.' when a session starts before 7 AM or runs
    past 8 PM where that person is. None when it's a sensible hour (or no zone is known)."""
    z = zone_named(zone) if zone else None
    if z is None:
        return None
    a, b = s.astimezone(z), e.astimezone(z)
    if a.hour < 7 or a.date() != b.date() or b.hour > 20 or (b.hour == 20 and b.minute):
        word = "early" if a.hour < 12 else "late"
        return (f"{name} would be at {_clock(a)} {interviews.zone_label(a)} ({word}) in their time zone. "
                "Pick another time or check it works for them.")
    return None


PERSON_HOURS = (8, 18)  # a person's own working hours (their local clock) when only their zone is known


def _in_their_hours(zone: str | None, s: datetime, e: datetime, hours: tuple[int, int] | None = None,
                    default: tuple[int, int] = PERSON_HOURS) -> bool:
    """Whether a session fits inside that person's working hours on their own clock: `hours` when they have
    their own, else `default` (8 to 6 for the candidate, 9 to 5 for an interviewer). With no zone known and no
    hours set it is True; hours set with no zone are read on the recruiter's clock."""
    z = zone_named(zone) if zone else None
    if z is None:
        if hours is None:
            return True
        z = shown_zone() or datetime.now().astimezone().tzinfo
    lo, hi = hours or default
    a, b = s.astimezone(z), e.astimezone(z)
    return a.date() == b.date() and a.hour >= lo and (b.hour, b.minute) <= (hi, 0)


def _start_time(start: str | None) -> tuple[int, int] | None:
    """The clock part of a start ('2026-11-12T13:00' or '2026-11-12 1:30 PM') as (hour, minute), else None."""
    text = (start or "").strip()[10:].strip(" Tt") if start else ""
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*(am|pm)?", text.lower())
    if not m:
        raise ValueError("start is a day like 2026-11-09, or a day and time like 2026-11-09T13:00 (not before 1 PM).")
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap:
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59:
        raise ValueError("start has a time that isn't on the clock. Use 24-hour time like 2026-11-09T13:00.")
    return h, mi


# ------------------------------------------------------------ the plan
def plan(store: Store, job_id: int, candidate_id: int, start: str | None = None,
         unavailable: list[str] | None = None, now: datetime | None = None,
         candidate_tz: str | None = None) -> dict[str, Any]:
    """{job, candidate, hours, sessions, warnings, invites, candidate_invite}. Each session: {name, minutes, at,
    at_local, end_local, interviewer, others, warnings, placed, already}. Sessions go in template order, each
    after the one before (with a short break), on the earliest half hour when the candidate and a free eligible
    interviewer (the least loaded that day, template order on a tie) are both free, inside working hours."""
    now = now or datetime.now(timezone.utc)
    start = _on_my_clock(start)
    job = store.get_job(job_id)
    cand = store.get_candidate(candidate_id)
    cz = _zone_or_error(candidate_tz) or store.candidate_zone(candidate_id)  # the candidate's own clock, if known
    book = store.interviewer_book()
    hours_book = person_hours_book(store)
    tpl = get_template(store, job_id)
    if not tpl:
        raise ValueError("This job has no loop template yet. Add one on the job's Loop page (one session per line, "
                         "like: System design | 60 | Priya Nair, Sam Okoye), or with set_loop_template.")
    h0, h1 = tpl["hours"]
    out_of = [u.strip() for u in (unavailable or []) if u and u.strip()][:10]
    first = _first_day(start, now)
    days = _working_days(first, HORIZON_DAYS)
    asked = date.fromisoformat(start.strip()[:10]) if start and start.strip() else first
    cal = _Cal.load(store, _at(days[0], 0), _at(days[-1] + timedelta(days=1), 0))
    have = [t for t in actions.upcoming_for(store, candidate_id, job_id, now)]
    cursor = _at(days[0], h0 * 60)
    not_before = _start_time(start)
    if not_before and days[0] == asked:  # a time with the start day means not before then on that day
        cursor = max(cursor, _at(days[0], not_before[0] * 60 + not_before[1]))
    if as_local(now).date() == days[0]:
        cursor = max(cursor, now + timedelta(hours=2))
    warnings: list[str] = []
    skipped = _skipped_holidays(asked, days[-1])
    if skipped:
        warnings.append("Skipped " + ", ".join(skipped) + ", a US public holiday. Nothing is planned on it.")
    sessions = []
    for spec in tpl["sessions"]:
        mins = int(spec["minutes"])
        eligible = [w for w in spec["interviewers"] if not any(interviews.name_matches(u, w) for u in out_of)]
        dropped = [w for w in spec["interviewers"] if w not in eligible]
        row: dict[str, Any] = {"name": spec["name"], "minutes": mins, "at": None, "at_local": None, "end_local": None,
                               "interviewer": None, "others": [], "warnings": [], "placed": False, "already": False}
        again = next((t for t in have if actions.interview_name(t).casefold().startswith(spec["name"].casefold())), None)
        if again:
            row["already"] = True
            row["warnings"].append(f"{cand['name']} already has {spec['name']} booked for {_when(to_dt(again['at']))}. "
                                   "Skipped, so it isn't booked twice.")
            sessions.append(row)
            continue
        found = None
        for day in days:
            if day < as_local(cursor).date():
                continue
            lo = max(cursor, _at(day, h0 * 60)) if as_local(cursor).date() == day else _at(day, h0 * 60)
            m = int((lo - _at(day, 0)).total_seconds() // 60)
            m = -(-m // STEP) * STEP
            while m + mins <= h1 * 60:
                s, e = _at(day, m), _at(day, m + mins)
                if not cal.candidate_busy(candidate_id, s, e) and _in_their_hours(cz, s, e):
                    if not spec["interviewers"]:
                        found = (s, e, None, [])
                    else:
                        free = [w for w in eligible if not cal.busy_for(w, s, e)
                                and _in_their_hours(interviews.interviewer_zone({}, w, book), s, e,
                                                    person_hours(hours_book, w), INTERVIEWER_DEFAULT_HOURS)]
                        if free:
                            free.sort(key=lambda w: (cal.count(w, day), eligible.index(w)))
                            found = (s, e, free[0], free[1:])
                if found:
                    break
                m += STEP
            if found:
                break
        if not found:
            if not eligible and dropped:
                row["warnings"].append(f"{', '.join(dropped)} {'is' if len(dropped) == 1 else 'are'} out and no one else "
                                       f"is listed for {spec['name']}. Add an interviewer for this session.")
            else:
                who = ", ".join(eligible) or "anyone"
                row["warnings"].append(f"No time in the next {HORIZON_DAYS} working days when {cand['name']} and {who} "
                                       "are all free and inside working hours (the recruiter's, 8 AM to 6 PM for "
                                       "the candidate, and each interviewer's own hours, 9 AM to 5 PM unless set, "
                                       "where they are when their zones are known). "
                                       "Add an interviewer for this session, or start later.")
            sessions.append(row)
            continue
        s, e, who, others = found
        day = as_local(s).date()
        iso = s.replace(microsecond=0).isoformat()
        email = interviews.email_for(who, book) if who else None
        iz = interviews.interviewer_zone({}, who, book) if who else None
        row.update({"at": iso, "at_local": _when(s), "end_local": _when(e),
                    "interviewer": who, "others": others, "placed": True, "interviewer_email": email,
                    "interviewer_tz": iz, "candidate_tz": cz,
                    "at_for_interviewer": interviews.when_for(iso, iz), "at_for_candidate": interviews.when_for(iso, cz)})
        for who_name, zone in ((cand["name"], cz), (who, iz)):
            odd = _odd_hours(who_name, zone, s, e) if who_name else None
            if odd:
                row["warnings"].append(odd)
        if who:
            top = spec["interviewers"][0]
            if top != who:
                busy = cal.busy_for(top, s, e)
                if top in dropped:
                    row["warnings"].append(f"{top} is out, so {who} would run it."
                                           + (" Also free: " + ", ".join(others) + "." if others else ""))
                elif busy:
                    row["warnings"].append(f"{top} is booked then ({busy['cname']}, {busy['what']}), so {who} would run it."
                                           + (" Also free: " + ", ".join(others) + "." if others else ""))
                elif not _in_their_hours(interviews.interviewer_zone({}, top, book), s, e, person_hours(hours_book, top),
                                         INTERVIEWER_DEFAULT_HOURS):
                    mine = person_hours(hours_book, top) or INTERVIEWER_DEFAULT_HOURS
                    row["warnings"].append(f"{top} is outside their working hours then ({mine[0]}:00 to {mine[1]}:00), "
                                           f"so {who} would run it."
                                           + (" Also free: " + ", ".join(others) + "." if others else ""))
            n = cal.count(who, day) + 1
            if n > MAX_PER_DAY:
                row["warnings"].append(f"{who} would have {n} sessions on {actions.day_text(day)}, which is a heavy day.")
        cal.add(s, e, [who] if who else [], candidate_id, cand["name"], spec["name"])
        sessions.append(row)
        cursor = e + timedelta(minutes=GAP)
    placed = [x for x in sessions if x["placed"]]
    if placed and len({as_local(to_dt(x["at"])).date() for x in placed}) > 1:
        warnings.append("The loop runs over more than one day. Start earlier, or open more hours, to fit one day.")
    unplaced = [x["name"] for x in sessions if not x["placed"] and not x["already"]]
    if unplaced:
        warnings.append("Not placed: " + ", ".join(unplaced) + ".")
    result = {"job": job, "candidate": cand, "hours": [h0, h1], "sessions": sessions, "warnings": warnings,
              "unavailable": out_of, "candidate_tz": cz}
    zone, why = zone_line(store)
    result["zone"], result["zone_note"] = zone, f"Times are in {zone} ({why})."
    result["invites"] = invites(result)
    result["candidate_invite"] = candidate_invite(result)
    return result


# ------------------------------------------------------------ invite text
def _job_name(job: dict[str, Any]) -> str:
    return actions.job_label(job["title"], job.get("company"))


def invites(p: dict[str, Any]) -> list[dict[str, Any]]:
    """One ready-to-paste invite per placed session with an interviewer: {session, interviewer, subject, body, mailto}."""
    out = []
    job, cand = _job_name(p["job"]), p["candidate"]["name"]
    for s in p["sessions"]:
        if not s["placed"] or not s["interviewer"]:
            continue
        first = interviews.display_name(s["interviewer"]).split(" ")[0] or "there"
        when = s.get("at_for_interviewer") or s["at_local"]  # on their own clock first, mine in brackets
        subject = f"Interview: {cand}, {s['name']}, {when}"
        body = (f"Hi {first},\n\nCould you run the {s['name']} interview ({s['minutes']} minutes) with {cand} for "
                f"{job} on {when}? I'll send the details and a feedback request after.\n\n"
                "If that time doesn't work for you, tell me and I'll find someone else.\n\nThanks!")
        email = s.get("interviewer_email")
        who = f"{interviews.display_name(s['interviewer'])} <{email}>" if email else s["interviewer"]
        out.append({"session": s["name"], "interviewer": s["interviewer"], "subject": subject, "body": body,
                    "email": email, "mailto": interviews.mailto(who, subject, body)})
    return out


def candidate_invite(p: dict[str, Any]) -> dict[str, Any] | None:
    placed = [s for s in p["sessions"] if s["placed"]]
    if not placed:
        return None
    cand, job = p["candidate"], _job_name(p["job"])
    first = cand["name"].split(" ")[0]
    lines = "\n".join(f"- {s.get('at_for_candidate') or s['at_local']} ({s['minutes']} min): {s['name']}"
                      + (f" with {interviews.display_name(s['interviewer'])}" if s["interviewer"] else "")
                      for s in placed)
    subject = f"Your interview schedule for {job}"
    body = (f"Hi {first},\n\nHere is the schedule for your interviews for {job}:\n\n{lines}\n\n"
            "Could you confirm these times work for you?\n\nThanks!")
    return {"subject": subject, "body": body, "mailto": interviews.mailto(cand.get("email") or "", subject, body)}


def plan_text(p: dict[str, Any]) -> str:
    cand, job = p["candidate"]["name"], _job_name(p["job"])
    lines = [f"Proposed loop for {cand} on {job} (working hours {p['hours'][0]}:00 to {p['hours'][1]}:00). Nothing is "
             "booked and no calendar is touched; book_loop logs these as interviews once the recruiter agrees."]
    for s in p["sessions"]:
        if s["placed"]:
            lines.append(f"- {s['name']} ({s['minutes']} min): {s['at_local']}"
                         + (f", with {s['interviewer']}" if s["interviewer"] else ", interviewer not set")
                         + (f". Free to swap in: {', '.join(s['others'])}" if s["others"] else ""))
        elif s["already"]:
            lines.append(f"- {s['name']}: already booked")
        else:
            lines.append(f"- {s['name']}: no slot found")
        lines += [f"  Warning: {w}" for w in s["warnings"]]
    lines += [f"Warning: {w}" for w in p["warnings"]]
    if p.get("zone_note"):
        lines.append(p["zone_note"] + " Say so if that is not where you are, and change it in Settings.")
    return "\n".join(lines)


def check_slot(store: Store, candidate_id: int, at: str, minutes: int, interviewer: str | None) -> list[str]:
    """Warnings for a slot the recruiter (or AI) picked by hand: the candidate or the interviewer already booked."""
    s = to_dt(at)
    e = s + timedelta(minutes=minutes)
    cal = _Cal.load(store, s - timedelta(days=1), e + timedelta(days=1))
    out = []
    b = cal.candidate_busy(candidate_id, s, e)
    if b:
        out.append(f"The candidate already has {b['what']}, which overlaps this session.")
    for w in interviews.interviewers(interviewer or ""):
        b = cal.busy_for(w, s, e)
        if b:
            out.append(f"{w} is booked then ({b['cname']}, {b['what']}), which overlaps this session.")
    return out


def session_summary(name: str, minutes: int) -> str:
    """The summary an interview is logged with; the '(60 min)' is how its length is remembered."""
    return f"{name} ({minutes} min)"


def unplaced_from(p: dict[str, Any]) -> list[dict[str, Any]]:
    """The sessions plan() could not place (not the ones already booked): [{"session", "reason"}]."""
    return [{"session": s["name"], "reason": (s["warnings"] or ["no free time"])[0]}
            for s in p["sessions"] if not s["placed"] and not s["already"]]


def slots_from(p: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"session": s["name"], "at": s["at"], "minutes": s["minutes"], "interviewer": s["interviewer"]}
            for s in p["sessions"] if s["placed"]]


def book(store: Store, candidate_id: int, job_id: int, slots: list[dict[str, Any]], by: str = "app",
         candidate_tz: str | None = None) -> list[int]:
    """Log each slot as an interview (what log_activity does). by app logs them as the recruiter's own. The
    candidate's time zone, when given, is kept on each session and for the candidate."""
    ids = []
    for sl in slots:
        entry = store.log(candidate_id, "interview", session_summary(sl["session"], int(sl["minutes"])), at=sl["at"],
                          job_id=job_id, source="AI app" if by == "ai" else "App", interviewer=sl.get("interviewer"),
                          candidate_tz=candidate_tz)
        if by == "ai":
            store.mark_ai_note(entry)
        ids.append(entry["id"])
    return ids


__all__ = ["us_holiday", "us_holidays", "NotFound", "book", "candidate_invite", "check_slot", "get_template", "invites", "parse_template", "plan",
           "plan_text", "save_template", "session_summary", "slots_from", "template_text"]
