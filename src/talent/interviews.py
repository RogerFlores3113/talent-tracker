"""The Interviews week: every session booked Monday to Friday across all jobs, grouped into loops, with each
interviewer's load and the clashes worth fixing (an interviewer double-booked, a candidate in two sessions at once).
Read-only, computed from timeline interview rows."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from . import actions
from .store import Store, to_dt

SESSION_MINUTES = 45  # interviews have no length saved; two starting closer than this overlap


def week_start(day: date | None = None) -> date:
    """The Monday of the week holding `day` (today by default); on a weekend, the coming Monday."""
    day = day or datetime.now(timezone.utc).astimezone().date()
    if day.weekday() >= 5:
        day += timedelta(days=7 - day.weekday())
    return day - timedelta(days=day.weekday())


def interviewers(text: str | None) -> list[str]:
    """'Sam Rivera, Alex Chen' or 'Priya and Ravi' -> each name."""
    parts = re.split(r"\s*(?:,|;|&|/|\band\b|\+)\s*", text or "")
    return [p.strip() for p in parts if p.strip()]


def week(store: Store, start: date | None = None) -> dict[str, Any]:
    """{start, end, prev, next, days: [{day, label, loops: [...]}], load: [{name, count}], clashes: [...],
    total, loops}. Each loop is one person's sessions for one job that day, in time order."""
    monday = week_start(start)
    tz = datetime.now(timezone.utc).astimezone().tzinfo
    lo = datetime.combine(monday, time.min, tz).astimezone(timezone.utc).isoformat()
    hi = datetime.combine(monday + timedelta(days=5), time.min, tz).astimezone(timezone.utc).isoformat()
    rows = store._rows(
        """SELECT t.*, c.name AS candidate_name, j.title AS job_title, j.company AS job_company FROM timeline t
           JOIN candidates c ON c.id = t.candidate_id LEFT JOIN jobs j ON j.id = t.job_id
           WHERE t.kind = 'interview' AND t.at >= ? AND t.at < ?
             AND coalesce(json_extract(t.data, '$.status'), 'scheduled') <> 'cancelled' ORDER BY t.at, t.id""", (lo, hi))
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    days = [{"day": monday + timedelta(days=i), "label": (monday + timedelta(days=i)).strftime("%a %b %-d"),
             "loops": []} for i in range(5)]
    loops = []
    for g in actions.group_loops(rows):
        by_day: dict[date, list[dict[str, Any]]] = {}
        for t in g:  # a session moved to another day shows on that day, still marked as part of its loop
            by_day.setdefault(actions._local_day(t["at"]), []).append(t)
        first = g[0]
        loop = {"candidate_id": first["candidate_id"], "name": first["candidate_name"], "job_id": first.get("job_id"),
                "job": actions.job_label(first.get("job_title"), first.get("job_company")) if first.get("job_title")
                else None, "size": len(g), "sessions": [actions._session(t, now) for t in g]}
        loops.append(loop)
        for d, ts in by_day.items():
            col = next((c for c in days if c["day"] == d), None)
            if col is not None:
                col["loops"].append({**loop, "sessions": [actions._session(t, now) for t in ts],
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
    return {"start": monday, "end": monday + timedelta(days=4), "prev": monday - timedelta(days=7),
            "next": monday + timedelta(days=7), "days": days, "loops": loops, "total": len(rows), "clashes": clashes,
            "load": sorted(load.values(), key=lambda e: (-e["count"], e["name"].casefold()))}


def _clashes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two sessions closer than SESSION_MINUTES with the same interviewer, or with the same candidate."""
    out = []
    gap = timedelta(minutes=SESSION_MINUTES)
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if to_dt(b["at"]) - to_dt(a["at"]) >= gap:
                break
            when = actions.local_time(a["at"], actions.IV_TIME)
            shared = {w.casefold() for w in interviewers(actions.interviewer_of(a))} & \
                     {w.casefold() for w in interviewers(actions.interviewer_of(b))}
            if shared:
                who = next(w for w in interviewers(actions.interviewer_of(a)) if w.casefold() in shared)
                out.append({"kind": "interviewer", "ids": [a["id"], b["id"]],
                            "text": f"{who} is booked twice around {when}: {a['candidate_name']} and {b['candidate_name']}"})
            if a["candidate_id"] == b["candidate_id"]:
                out.append({"kind": "candidate", "ids": [a["id"], b["id"]],
                            "text": f"{a['candidate_name']} has two sessions around {when}"})
    return out


def week_text(w: dict[str, Any]) -> str:
    """The week as plain text (for the AI): each day's loops, then each interviewer's load and any clashes."""
    lines = [f"Interviews {w['start'].strftime('%a %b %-d')} to {w['end'].strftime('%a %b %-d')}: {w['total']} "
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
    return "\n".join(lines)


__all__ = ["interviewers", "week", "week_start", "week_text"]
