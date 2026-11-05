"""Hiring plan: a scoreboard for the few roles a founder is hiring, one seat each.

A seat is a role, the job that fills it (optional), a target start date and a status. For each seat the page
shows the funnel by stage group, how long people have waited in each, and whether the seat is on track: working
back from the start date, an offer has to be out about 4 weeks before it (3 weeks' notice and a week to answer),
first interviews 2 weeks before that, and people in the funnel 2 weeks before that. A seat that has missed a
step, or will within a week, says what to do this week. Everything is computed here from the pipeline; the
seats are the only thing stored (the plan_seats table, store migration v12).

The AI app reads the plan with list_hiring_plan and adds or changes a seat with set_plan_seat. It cannot delete
one. Each write is logged in ai_actions (kind plan_seat) with the seat as it was, so Changes can undo it; it
waits on Review in Cautious mode and counts toward the hourly caps.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.types import CallToolResult

from . import actions, reports
from .store import AI_NOTE_CAP, AI_EDIT_WINDOW_MINUTES, NotFound, Store, as_local, now_iso, quoted

STATUSES = ("open", "filled", "paused")
STATUS_WORDS = {"open": "Hiring", "filled": "Filled", "paused": "Paused"}
_ALIASES = {"hiring": "open", "active": "open", "sourcing": "open", "planned": "open", "on hold": "paused",
            "hold": "paused", "pause": "paused", "done": "filled", "hired": "filled", "closed": "filled",
            "fill": "filled"}
FIELDS = ("role", "job_id", "target_start", "status")
LABELS = {"role": "role", "job_id": "job", "target_start": "target start date", "status": "status"}
GROUPS = ("sourcing", "screening", "interview", "offer", "hired")
GROUP_WORDS = {"sourcing": "Sourcing", "screening": "Screening", "interview": "Interviewing", "offer": "Offer",
               "hired": "Hired"}
RANK = {g: i for i, g in enumerate(GROUPS)}
MAX_SEATS = 25
NOTICE_KEY = "plan_notice_days"
DEFAULT_NOTICE = 21  # days between an accepted offer and the start date
ANSWER_DAYS = 7  # a week for the candidate to answer
STEP_DAYS = 14  # between first people in the funnel, first interviews and an offer going out
TIGHT_DAYS = 7  # within this many days of missing a step, a seat reads Tight
WAIT_DAYS = 7  # people waiting longer than this in one stage are worth a nudge
THIN = 3  # fewer people than this in play reads as thin
UNSET: Any = object()
SNAP = FIELDS


# ------------------------------------------------------------------ reading
def seats(store: Store) -> list[dict[str, Any]]:
    return store._rows("SELECT * FROM plan_seats ORDER BY position, id")


def get(store: Store, seat_id: int) -> dict[str, Any]:
    row = store._row("SELECT * FROM plan_seats WHERE id = ?", (seat_id,))
    if row is None:
        raise NotFound(f"No seat with id {seat_id}. list_hiring_plan shows them.")
    return row


def snap(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return {k: row.get(k) for k in SNAP} if row else None


def notice_days(store: Store) -> int:
    try:
        return max(0, min(int(store.setting(NOTICE_KEY) or DEFAULT_NOTICE), 120))
    except ValueError:
        return DEFAULT_NOTICE


def lead_days(store: Store) -> dict[str, int]:
    """Days before the start date by which each step has to be reached."""
    offer = notice_days(store) + ANSWER_DAYS
    return {"offer": offer, "interview": offer + STEP_DAYS, "screening": offer + 2 * STEP_DAYS}


def _today(now: datetime | None = None) -> date:
    return as_local(now or datetime.now(timezone.utc)).date()


def _day(iso: str | None) -> date | None:
    try:
        return date.fromisoformat(str(iso)[:10]) if iso else None
    except ValueError:
        return None


def _when(d: date) -> str:
    return actions.day_text(d, "%a %b %-d")


def _days_since(iso: str | None, today: date) -> int:
    d = _day(iso)
    return max(0, (today - d).days) if d else 0


def _funnel(store: Store, job_id: int, today: date) -> dict[str, Any]:
    """People on a job by plan group: count, longest wait (days) and who has waited longest."""
    groups = reports.stage_groups(store, job_id)
    out: dict[str, Any] = {g: {"count": 0, "longest_days": 0, "names": []} for g in GROUPS}
    out_n = 0
    for a in store.pipeline(job_id):
        g = groups.get((a["stage"] or "").lower(), "screening")
        if g == "out":
            out_n += 1
            continue
        g = "screening" if g == "submitted" else g
        row = out[g]
        days = _days_since(a.get("stage_changed_at"), today)
        row["count"] += 1
        if days >= row["longest_days"]:
            row["longest_days"] = days
        row["names"].append((days, a["candidate_name"], a["candidate_id"], a["stage"]))
    for g in GROUPS:
        out[g]["names"].sort(key=lambda t: (-t[0], t[1]))
    return {"groups": out, "out": out_n}


def score(store: Store, seat: dict[str, Any], today: date | None = None, *, hired_before: int = 0,
          leads: dict[str, int] | None = None) -> dict[str, Any]:
    """One seat's scoreboard: funnel, state (on_track, tight, behind, filled, paused, no_job, no_date), the
    next step with its date, and 'what to do this week' lines. hired_before: hires already counted against
    earlier seats on the same job."""
    today = today or _today()
    leads = leads or lead_days(store)
    start = _day(seat.get("target_start"))
    job = store.get_job(seat["job_id"]) if seat.get("job_id") else None
    res: dict[str, Any] = {
        "id": seat["id"], "role": seat["role"], "status": seat["status"], "job_id": seat.get("job_id"),
        "job": (job["title"] + (f" ({job['company']})" if job.get("company") else "")) if job else None,
        "target_start": seat.get("target_start"), "target_text": _when(start) if start else None,
        "days_left": (start - today).days if start else None, "funnel": None, "todo": [], "next_step": None,
    }
    fun = _funnel(store, job["id"], today) if job else None
    if fun:
        g = fun["groups"]
        total = sum(g[k]["count"] for k in GROUPS) + fun["out"]
        reached = lambda k: sum(g[x]["count"] for x in GROUPS if RANK[x] >= RANK[k])  # noqa: E731
        res["funnel"] = {
            "groups": [{"group": k, "label": GROUP_WORDS[k], "count": g[k]["count"],
                        "longest_days": g[k]["longest_days"]} for k in GROUPS],
            "out": fun["out"], "total": total, "in_play": sum(g[k]["count"] for k in GROUPS[:4]),
            "reached_interview": reached("interview"), "reached_offer": reached("offer"),
            "interview_rate": round(100 * reached("interview") / total) if total else 0}
    hired = fun["groups"]["hired"]["count"] if fun else 0
    furthest = max((RANK[k] for k in GROUPS[:4] if fun and fun["groups"][k]["count"]), default=-1)
    if seat["status"] == "paused":
        res.update(state="paused", state_word="Paused", todo=["This seat is paused. Switch it back to Hiring when "
                                                               "you start again."])
        return res
    if seat["status"] == "filled" or (job and hired - hired_before >= 1):
        res.update(state="filled", state_word="Filled", todo=[])
        return res
    todo: list[str] = []
    state = "on_track"
    if job is None:
        state = "no_job"
        todo.append("Make a job for this role (Jobs), then link it here so the plan can count its people.")
    if start is None:
        state = "no_date" if state == "on_track" else state
        todo.append("Set a target start date so this seat can be tracked.")
    steps: list[tuple[str, str]] = [("screening", "have people in the funnel"), ("interview", "be interviewing"),
                                    ("offer", "have an offer out")]
    worst: tuple[int, str, date] | None = None
    if start is not None and job is not None:
        if start < today:
            state = "behind"
            todo.append(f"The start date ({_when(start)}) has passed. Move it, or mark the seat Filled or Paused.")
        for key, _word in steps:
            need = start - timedelta(days=leads[key])
            if furthest >= RANK[key]:
                continue
            slack = (need - today).days
            if worst is None or slack < worst[0]:
                worst = (slack, key, need)
        if worst is not None and state == "on_track":
            state = "behind" if worst[0] < 0 else "tight" if worst[0] <= TIGHT_DAYS else "on_track"
        if worst is not None:
            res["next_step"] = {"step": worst[1], "label": dict(steps)[worst[1]], "by": worst[2].isoformat(),
                                "by_text": _when(worst[2]), "days": worst[0]}
    if fun is not None and start is not None:
        g = fun["groups"]
        if worst is not None and worst[0] <= TIGHT_DAYS:
            key, by = worst[1], _when(worst[2])
            if key == "offer":
                top = g["interview"]["names"]
                todo.append(f"Decide on {top[0][1]} ({top[0][0]} days in {top[0][3]}): an offer should be out by "
                            f"{by}." if top else f"Get someone to a final interview: an offer should be out by {by}.")
            elif key == "interview":
                n = g["screening"]["count"]
                todo.append(f"Move {n} screening {'person' if n == 1 else 'people'} on to interviews: first "
                            f"interviews should start by {by}." if n else
                            f"Line up first interviews by {by}. Nobody is screened yet, so reach out to more people.")
            else:
                todo.append(f"Start reaching out now: have people in the funnel by {by}.")
        play = sum(g[k]["count"] for k in GROUPS[:4])
        if play < THIN:
            todo.append(f"Only {play} {'person' if play == 1 else 'people'} in play. Aim for at least {THIN * 2}: "
                        "ask Claude to find more.")
        for k in GROUPS[:4]:
            waiting = [t for t in g[k]["names"] if t[0] > WAIT_DAYS]
            if waiting:
                todo.append(f"{len(waiting)} {'person has' if len(waiting) == 1 else 'people have'} waited over "
                            f"{WAIT_DAYS} days in {waiting[0][3]}, longest {waiting[0][1]} ({waiting[0][0]} days). "
                            "Decide or nudge.")
    if not todo and job is not None:
        todo.append("Nothing urgent. Keep conversations moving.")
    res.update(state=state, todo=todo[:5], state_word={"on_track": "On track", "tight": "Tight", "behind": "Behind",
                                                       "no_job": "No job yet", "no_date": "No date"}[state])
    return res


def scoreboard(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every seat scored. Seats on the same job split its hires in order (the first hire fills the first seat)."""
    today, leads = _today(now), lead_days(store)
    taken: dict[int, int] = {}
    out = []
    for s in seats(store):
        jid = s.get("job_id")
        row = score(store, s, today, hired_before=taken.get(jid, 0) if jid else 0, leads=leads)
        if jid and row["state"] == "filled" and s["status"] != "filled":
            taken[jid] = taken.get(jid, 0) + 1
        out.append(row)
    return out


def behind_seats(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    return [r for r in scoreboard(store, now) if r["state"] == "behind"]


def today_items(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """Today: one line for a seat that has missed a step toward its start date."""
    if not store._row("SELECT 1 FROM plan_seats LIMIT 1"):
        return []
    now = now or datetime.now(timezone.utc)
    items = []
    for r in behind_seats(store, now):
        nxt = r.get("next_step")
        why = (f"Should {nxt['label']} by {nxt['by_text']}." if nxt and nxt["days"] < 0 else
               f"The start date ({r['target_text']}) has passed." if r.get("days_left") is not None
               and r["days_left"] < 0 else "")
        items.append({"key": f"plan:{r['id']}:{r.get('target_start')}", "kind": "plan", "rank": 2,
                      "when": now.replace(microsecond=0).isoformat(), "job_id": r.get("job_id"),
                      "title": f"{r['role']} is behind the plan for {r['target_text']}",
                      "detail": " ".join(x for x in (why, (r["todo"] or [""])[0]) if x), "href": "/plan",
                      "button": "Open the plan"})
    return items


def claude_prompt(rows: list[dict[str, Any]]) -> str:
    names = ", ".join(r["role"] for r in rows[:5])
    return ("Open my hiring plan with list_hiring_plan. For each seat that is Behind or Tight"
            + (f" ({names})" if names else "")
            + ", tell me the three things to do this week to hit the start date, in order. Where a seat needs more "
              "people, search my saved candidates first, then put new people on the job's Shortlist. Don't change "
              "anything until I say so.")


# ------------------------------------------------------------------ writing
def _status(value: Any) -> str:
    key = " ".join(str(value or "").strip().lower().replace("_", " ").split()).strip(" .!")
    key = _ALIASES.get(key, key)
    if key not in STATUSES:
        raise ValueError("No change made. A seat's status is open (hiring), filled or paused. "
                         f"{str(value)[:40]!r} is none of those.")
    return key


def clean(store: Store, given: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Seat fields as they are saved. '' clears the job or the date. Raises ValueError with what's wrong."""
    from .offers import _day as parse_day

    out: dict[str, Any] = {}
    for k, v in given.items():
        if k == "role":
            text = " ".join(str(v or "").split())
            if not text:
                raise ValueError("A seat needs a role, like \"Founding backend engineer\".")
            out[k] = text[:120]
        elif k == "job_id":
            if v in (None, ""):
                out[k] = None
            else:
                try:
                    jid = int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"job_id is a number, not {str(v)[:40]!r}; list_jobs shows them.") from None
                store.get_job(jid)
                out[k] = jid
        elif k == "target_start":
            out[k] = parse_day(v, "target start date", _today(now))
        elif k == "status":
            out[k] = _status(v)
        else:
            raise ValueError(f"Unknown seat field {k}. It accepts: {', '.join(FIELDS)}.")
    return out


def save(store: Store, seat_id: int | None = None, *, by: str = "app", now: datetime | None = None,
         **given: Any) -> dict[str, Any]:
    """Add a seat (seat_id None, role needed) or change one. {"seat", "created", "changed": {field: (old, new)},
    "action_id"}. by="ai" logs the write for Changes."""
    fields = clean(store, given, now)
    with store._write():
        row = get(store, seat_id) if seat_id is not None else None
        created = row is None
        before = snap(row)
        if created:
            if not fields.get("role"):
                raise ValueError("A new seat needs a role, like \"Founding backend engineer\".")
            n = store._row("SELECT count(*) AS n FROM plan_seats")["n"]
            if n >= MAX_SEATS:
                raise ValueError(f"The plan holds at most {MAX_SEATS} seats.")
            ts = now_iso()
            pos = store._row("SELECT coalesce(max(position), 0) + 1 AS p FROM plan_seats")["p"]
            sid = store._conn.execute(
                "INSERT INTO plan_seats(role, job_id, target_start, status, position, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fields["role"], fields.get("job_id"), fields.get("target_start"), fields.get("status", "open"),
                 pos, ts, ts)).lastrowid
            row = get(store, sid)
            changed = {k: (None, row[k]) for k in FIELDS if row.get(k) is not None}
        else:
            changed = {k: (row.get(k), v) for k, v in fields.items() if row.get(k) != v}
            if changed:
                cols = ", ".join(f"{k} = ?" for k in changed)
                store._conn.execute(f"UPDATE plan_seats SET {cols}, updated_at = ? WHERE id = ?",
                                    (*[v for _o, v in changed.values()], now_iso(), row["id"]))
        after = get(store, row["id"])
        action_id = None
        if by == "ai" and (changed or created):
            action_id = store._log_ai("plan_seat", job_id=after.get("job_id"), target_id=after["id"],
                                      body=", ".join(LABELS[k] for k in changed) or None, old=before,
                                      new=snap(after))
    return {"seat": after, "created": created, "changed": changed, "action_id": action_id}


def remove(store: Store, seat_id: int) -> str:
    """The recruiter takes a seat off the plan (the AI app cannot)."""
    seat = get(store, seat_id)
    with store._write():
        store._conn.execute("DELETE FROM plan_seats WHERE id = ?", (seat_id,))
    return seat["role"]


# ------------------------------------------------------------------ AI log: summary, undo, caps
def overwrite_count(old: dict[str, Any] | None, new: dict[str, Any] | None) -> int:
    """Saved seat values one AI write replaced or cleared (a new seat replaces nothing)."""
    return sum(1 for k in FIELDS if (old or {}).get(k) not in (None, "") and (new or {}).get(k) != old[k])


def ai_summary(r: dict[str, Any]) -> str:
    new = r.get("new_data") or {}
    role = quoted(new.get("role") or "a seat")
    if r.get("old_data") is None:
        return f"Added the seat {role} to the hiring plan"
    return f"Changed the hiring plan seat {role}" + (f": {r['body']}" if r.get("body") else "")


def ai_blocker(store: Store, a: dict[str, Any]) -> str | None:
    row = store._row("SELECT * FROM plan_seats WHERE id = ?", (a.get("target_id"),))
    if row is None:
        return "that seat is gone"
    return None if snap(row) == (a.get("new_data") or {}) else "the seat was changed again since"


def ai_undo(store: Store, a: dict[str, Any]) -> str:
    """Put a seat back as it was before the AI app's write (a seat it added is taken off the plan)."""
    reason = ai_blocker(store, a)
    if reason:
        raise ValueError(reason[:1].upper() + reason[1:] + ".")
    old = a.get("old_data")
    with store._write():
        if old is None:
            store._conn.execute("DELETE FROM plan_seats WHERE id = ?", (a["target_id"],))
        else:
            cols = ", ".join(f"{k} = ?" for k in FIELDS)
            store._conn.execute(f"UPDATE plan_seats SET {cols}, updated_at = ? WHERE id = ?",
                                (*[old.get(k) for k in FIELDS], now_iso(), a["target_id"]))
        store._conn.execute("UPDATE ai_actions SET undone_at = ? WHERE id = ?", (now_iso(), a["id"]))
    return a["summary"]


def ai_writes_this_hour(store: Store) -> int:
    since = (datetime.now(timezone.utc) - timedelta(minutes=AI_EDIT_WINDOW_MINUTES)).replace(microsecond=0).isoformat()
    done = store._row("SELECT count(*) AS n FROM ai_actions WHERE kind = 'plan_seat' AND at >= ?", (since,))["n"]
    waiting = store._row("SELECT count(*) AS n FROM ai_review WHERE kind = 'plan' AND status = 'pending'"
                         " AND created_at >= ?", (since,))["n"]
    return done + waiting


def check_cap(store: Store) -> None:
    if ai_writes_this_hour(store) >= AI_NOTE_CAP:
        raise ValueError(f"No change made. Your AI app already changed the hiring plan {AI_NOTE_CAP} times this "
                         "hour. Ask the recruiter to make more changes in the app, or try again later.")


# ------------------------------------------------------------------ Cautious mode
def queue(store: Store, seat_id: int | None, fields: dict[str, Any], session: str | None) -> dict[str, Any]:
    from . import review

    before = snap(get(store, seat_id)) if seat_id is not None else None
    role = (fields.get("role") or (before or {}).get("role") or "a seat")
    summary = (f"Add the seat {quoted(role)} to the hiring plan" if before is None else
               f"Change the hiring plan seat {quoted(role)}: " + ", ".join(LABELS[k] for k in fields))
    return review._queue(store, "plan", summary, {"seat_id": seat_id, "fields": fields, "before": before},
                         job_id=fields.get("job_id") or (before or {}).get("job_id"), session=session)


def apply_review(store: Store, r: dict[str, Any]) -> str:
    """Make a kept seat write, once, unless the seat changed since it was sent."""
    p = r["payload"]
    if p.get("seat_id") is not None:
        try:
            now = snap(get(store, p["seat_id"]))
        except NotFound:
            raise ValueError("That seat is gone.") from None
        if now != p.get("before"):
            raise ValueError("The seat was changed since, so nothing was changed.")
    save(store, p.get("seat_id"), by="ai", **(p.get("fields") or {}))
    return r["summary"]


def review_lines(r: dict[str, Any]) -> list[dict[str, str]]:
    p = r["payload"]
    before = p.get("before") or {}
    show = lambda k, v: (STATUS_WORDS.get(v, v) if k == "status" else v)  # noqa: E731
    return [{"label": LABELS.get(k, k), "old": str(show(k, before.get(k)) or "(empty)"),
             "new": str(show(k, v) or "(cleared)")} for k, v in (p.get("fields") or {}).items()]


# ------------------------------------------------------------------ MCP tools
def register_tools(mcp: Any, store: Store, session: str) -> None:
    """list_hiring_plan and set_plan_seat on the MCP server."""
    from . import review
    from .mcp_server import EDIT, READ, _check_cap, _plain, _wait_text, _waiting
    from .paths import app_url

    def seat_line(r: dict[str, Any]) -> str:
        bits = [f"{quoted(r['role'])} (seat id {r['id']}): {r['state_word']}"]
        if r.get("target_text"):
            left = r["days_left"]
            bits.append(f"start {r['target_text']}" + (f", {left} days left" if left is not None and left >= 0 else ""))
        if r.get("job"):
            bits.append(f"job {r['job']} [job_id {r['job_id']}]")
        f = r.get("funnel")
        if f:
            bits.append(", ".join(f"{g['label']} {g['count']}" for g in f["groups"]) + f", out {f['out']}")
        if r.get("next_step") and r["state"] in ("behind", "tight"):
            n = r["next_step"]
            bits.append(f"needs to {n['label']} by {n['by_text']}")
        return "- " + "; ".join(bits) + (". This week: " + " ".join(r["todo"][:3]) if r["todo"] else ".")

    @mcp.tool(annotations=READ)
    def list_hiring_plan() -> CallToolResult:
        """The hiring plan: each seat (a role the recruiter is hiring for) with its target start date, the funnel by
        stage group, how long people have waited, whether it is on track, tight or behind, and what to do this
        week. Seats are set with set_plan_seat. Use it when asked how hiring is going or what to do this week."""
        rows = scoreboard(store)[:MAX_SEATS]
        page = app_url("/plan")
        if not rows:
            return _plain("The hiring plan is empty. Add a seat with set_plan_seat (a role, its job_id and a target "
                          "start date), or the recruiter adds them on the Hiring plan page.",
                          {"seats": [], "app_url": page})
        behind = sum(1 for r in rows if r["state"] == "behind")
        tight = sum(1 for r in rows if r["state"] == "tight")
        head = (f"{len(rows)} seat{'' if len(rows) == 1 else 's'} in the hiring plan: {behind} behind, "
                f"{tight} tight.")
        return _plain("\n".join([head, *(seat_line(r) for r in rows)]),
                      {"seats": rows, "behind": behind, "tight": tight, "app_url": page})

    @mcp.tool(annotations=EDIT)
    def set_plan_seat(role: str | None = None, seat_id: int | None = None, job_id: int | None = None,
                      target_start: str | None = None, status: str | None = None) -> CallToolResult:
        """Add a seat to the hiring plan, or change one. Without seat_id it adds a seat (role needed); with seat_id
        it changes that seat. job_id links the job that fills it, target_start is the day they should start
        (2026-12-01, Dec 1, friday), status is open (hiring), filled or paused. Leave a field out to keep it; "" or 0
        clears the job or the date. Also accepted: title for role, start for target_start. It cannot remove a seat.
        The recruiter can undo every change on Changes."""
        given: dict[str, Any] = {}
        if role is not None:
            given["role"] = role
        if job_id is not None:
            given["job_id"] = job_id or None
        if target_start is not None:
            given["target_start"] = target_start
        if status is not None:
            given["status"] = status
        if seat_id is None and not (role or "").strip():
            raise ValueError("No change made. A new seat needs a role. To change a seat, give its seat_id "
                             "(list_hiring_plan shows them).")
        if seat_id is not None and not given:
            raise ValueError("Nothing to change. Give role, job_id, target_start or status.")
        fields = clean(store, given)
        row = get(store, seat_id) if seat_id is not None else None
        if row is None:  # the same role again is a change, not a second seat
            same = [s for s in seats(store) if s["role"].casefold() == fields["role"].casefold()
                    and (fields.get("job_id") in (None, s.get("job_id")))]
            if same:
                raise ValueError(f"No change made. The plan already has a seat {quoted(same[0]['role'])} (seat id "
                                 f"{same[0]['id']}). Give seat_id {same[0]['id']} to change it.")
        changed = {k: v for k, v in fields.items() if (row or {}).get(k) != v}
        if row is not None and not changed:
            return _plain(f"Nothing changed: the seat {quoted(row['role'])} already says that.",
                          {"seat": snap(row), "seat_id": row["id"]})
        check_cap(store)
        if review.cautious(store):
            waiting = queue(store, seat_id, changed if row else fields, session)
            return _plain(_wait_text(waiting), {"pending_review": _waiting(waiting)})
        _check_cap(store, overwrite_count(snap(row), {**(snap(row) or {}), **changed}))
        res = save(store, seat_id, by="ai", **(changed if row else fields))
        seat = res["seat"]
        what = ", ".join(LABELS[k] for k in res["changed"])
        sentence = (f"{'Added the seat' if res['created'] else 'Changed the seat'} {quoted(seat['role'])} "
                    f"(seat id {seat['id']})" + (f" ({what})" if what and not res["created"] else "")
                    + (f", starting {_when(_day(seat['target_start']))}" if seat.get("target_start") else "")
                    + ". The recruiter can undo it on Changes.")
        return _plain(sentence, {"seat": snap(seat), "seat_id": seat["id"], "app_url": app_url("/plan")})


# ------------------------------------------------------------------ web
def register_routes(app: Any, store: Store, page: Any, back: Any) -> None:
    """The Hiring plan page: GET /plan, and POST /plan/seat, /plan/seat/{id}/remove and /plan/settings."""

    def to_plan(notice: str = "", error: str = "") -> RedirectResponse:
        q = urlencode([(k, v) for k, v in (("notice", notice), ("flash_error", error)) if v])
        return RedirectResponse("/plan" + (f"?{q}" if q else ""), status_code=303)

    @app.get("/plan", response_class=HTMLResponse)
    def plan_page(request: Request):
        rows = scoreboard(store)
        jobs = [j for j in store.list_jobs() if j["status"] != "closed"]
        from . import review

        waiting = [r for r in review.pending(store) if r["kind"] == "plan"]
        return page(request, "plan.html", seats=rows, jobs=jobs, statuses=STATUS_WORDS, notice_days=notice_days(store),
                    leads=lead_days(store), prompt=claude_prompt(rows), waiting=waiting)

    @app.post("/plan/seat")
    async def plan_seat(request: Request):
        form = await request.form()
        raw = str(form.get("seat_id") or "").strip()
        given: dict[str, Any] = {"role": str(form.get("role") or ""), "job_id": str(form.get("job_id") or ""),
                                 "target_start": str(form.get("target_start") or "")}
        if str(form.get("status") or "").strip():
            given["status"] = str(form.get("status"))
        try:
            res = save(store, int(raw) if raw.isdigit() else None, by="app", **given)
        except (ValueError, NotFound) as exc:
            return to_plan(error=str(exc).removeprefix("No change made. "))
        role = quoted(res["seat"]["role"])
        return to_plan(notice=f"Added the seat {role}." if res["created"] else
                       f"Saved the seat {role}." if res["changed"] else "Nothing changed.")

    @app.post("/plan/seat/{seat_id}/remove")
    def plan_remove(seat_id: int):
        try:
            role = remove(store, seat_id)
        except NotFound:
            return to_plan(error="That seat is already gone.")
        return to_plan(notice=f"Took the seat {quoted(role)} off the plan.")

    @app.post("/plan/settings")
    async def plan_settings(request: Request):
        form = await request.form()
        raw = str(form.get("notice_days") or "").strip()
        if not re.fullmatch(r"\d{1,3}", raw) or int(raw) > 120:
            return to_plan(error="Notice must be a number of days, 0 to 120.")
        actions._put_setting(store, NOTICE_KEY, raw)
        return to_plan(notice=f"Counting {raw} days between an accepted offer and the start date.")


__all__ = ["scoreboard", "today_items", "register_routes", "register_tools", "save", "seats"]
