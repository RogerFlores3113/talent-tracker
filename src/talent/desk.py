"""Client desk: one run sheet per client, in the order a recruiter works it.

It merges what other modules already know about one client's open jobs: the drafted update (reports), submittals
waiting on feedback with a nudge per hiring manager (chase), interview feedback owed (interviews), offers waiting on
an answer (offers) and follow-ups due (outreach). Nothing is rebuilt here and nothing is sent: each item carries its
paste-ready text and either a Mark done form (a POST the app already has) or a link to the page that does it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import actions, chase, interviews, offers, outreach, reports
from .store import Store, as_local, quoted

MAX_ITEMS = 40  # items per section on the page and in the tool
SECTIONS = (("update", "Client update"), ("chase", "Waiting on the client"), ("feedback", "Interview feedback owed"),
            ("offer", "Offers waiting on an answer"), ("followup", "Follow-ups due"))


def _done(label: str, url: str, **fields: str) -> dict[str, Any]:
    """A Mark done: a form the page posts to a route the app already has."""
    return {"label": label, "post": url, "fields": {**fields, "back": "desk"}}


def _link(label: str, href: str) -> dict[str, Any]:
    return {"label": label, "href": href}


def _update_items(store: Store, c: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    if not any(j["status"] == "open" for j in c["jobs"]):
        return []
    try:
        reps = reports.reports_for(store, c["key"], now=now)
    except ValueError:
        return []
    due = {mk for mk, _ in reports.due_parts(store, c["key"], now, c)}
    out = []
    for rep in reps:
        text = reports.report_text(rep)
        is_due = rep.get("manager_key") in due
        who = f" for {rep['manager']}" if rep.get("manager") else ""
        day = c.get("update_day")
        out.append({"kind": "update", "title": f"Update{who}" + (" is due" if is_due else ""),
                    "detail": (f"Goes out {day}s. " if day else "No update day set. ") + (
                        "Nobody has marked it sent this week." if is_due else "Draft as it stands."),
                    "due": is_due, "text": text, "email": reports.email_link(rep, text),
                    "ai_moves": len(rep.get("ai_moves") or []),
                    "done": _link("Review and mark sent", f"/clients/{c['slug']}#report")})
    out.sort(key=lambda i: not i["due"])
    return out


def _chase_items(store: Store, c: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    out = []
    for g in chase.chase_list(store, c["key"], now=now, days=chase.chase_days(store)):
        n = len(g["people"])
        who = g["manager"] or c["name"]
        if g["next_on"]:  # chased lately: say so, don't draft the nudge again
            out.append({"kind": "chase", "not_due": True, "snoozed": True, "last_chased_at": g["chased_at"],
                        "nudge_on": as_local(g["next_on"]).date().isoformat(),
                        "title": f"Waiting on {who}: {n} {'person' if n == 1 else 'people'}",
                        "detail": chase.chased_line(g["chased_at"], g["next_on"]) + ". The nudge isn't drafted again until then.",
                        "people": [{k: p[k] for k in ("candidate_id", "name", "job_id", "job", "stage", "days")}
                                   for p in g["people"][:chase.PAGE_SHOWN]]})
            continue
        out.append({"kind": "chase", "title": f"Chase {who}: {n} {'person' if n == 1 else 'people'} waiting",
                    "detail": ", ".join(f"{p['name']} ({p['job']}, {p['stage']}"
                                        + ("" if p["date_unknown"] else f", {p['days']} days") + ")"
                                        for p in g["people"][:4]) + (f" and {n - 4} more" if n > 4 else ""),
                    "text": g["text"], "to": g["to"], "subject": g["subject"], "email": g["email"],
                    "last_chased_at": g["chased_at"], "longest": g["longest"],
                    "people": [{k: p[k] for k in ("candidate_id", "name", "job_id", "job", "stage", "days")}
                               for p in g["people"][:chase.PAGE_SHOWN]],
                    "done": _done("Mark as chased", f"/clients/{c['slug']}/chased", manager=g["manager_key"],
                                  manager_name=who)})
    if not out:  # nothing is overdue: say who is still waiting on the client, and when the nudge comes due
        out = _waiting_items(store, c, now)
    return out


def _waiting_items(store: Store, c: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """People waiting on the client whose nudge isn't due yet: one item per hiring manager, with the day count and the
    day the nudge becomes due. Not work to do, so they don't count toward the desk's to-do total."""
    after = chase.chase_days(store)
    today = as_local(now).date()
    out = []
    for g in chase.chase_list(store, c["key"], now=now, days=0):
        n = len(g["people"])
        who = g["manager"] or c["name"]
        left = max(0, after - min(p["days"] for p in g["people"]))
        on = today + timedelta(days=left)
        shown = ", ".join(f"{p['name']} ({p['job']}, " + ("date not known" if p["date_unknown"] else f"day {p['days']}") + ")"
                          for p in g["people"][:4]) + (f" and {n - 4} more" if n > 4 else "")
        when = "today" if left == 0 else "tomorrow" if left == 1 else f"in {left} days ({actions.day_text(on)})"
        out.append({"kind": "chase", "not_due": True, "nudge_on": on.isoformat(),
                    "title": f"Waiting on {who}: {n} {'person' if n == 1 else 'people'}",
                    "detail": f"{shown}. The nudge comes due {when}, after {after} days.",
                    "people": [{k: p[k] for k in ("candidate_id", "name", "job_id", "job", "stage", "days")}
                               for p in g["people"][:chase.PAGE_SHOWN]]})
    return out


def _feedback_items(store: Store, job_ids: set[int], now: datetime) -> list[dict[str, Any]]:
    out = []
    for r in interviews.feedback_owed(store, now):
        if r["job_id"] not in job_ids:
            continue
        people = list(dict.fromkeys(o["name"] for o in r["owed"]))
        out.append({"kind": "feedback", "title": f"Feedback owed on {r['name']} ({r['day']})",
                    "detail": f"{r['job']} - owed by {', '.join(people)}", "candidate_id": r["candidate_id"],
                    "job_id": r["job_id"], "owed": [{k: o[k] for k in ("name", "email", "session", "interview_id",
                                                                       "subject", "body", "mailto")} for o in r["owed"]],
                    "text": "\n\n".join(f"To {o['name']}:\n{o['body']}" for o in r["owed"]),
                    "done": _link("Open Decide", f"/jobs/{r['job_id']}/decide#c{r['candidate_id']}")})
    return out


def _offer_items(store: Store, job_ids: set[int], now: datetime) -> list[dict[str, Any]]:
    out = []
    for o in offers.listed(store):
        if o["job_id"] not in job_ids or o["job_status"] == "closed" or o["status"] != "out":
            continue
        late = offers.overdue(o, now)
        out.append({"kind": "offer", "title": f"Offer to {o['candidate_name']} waiting on an answer",
                    "detail": f"{offers._job_name(o)} - {offers.status_line(o, now)}", "overdue": late,
                    "candidate_id": o["candidate_id"], "job_id": o["job_id"], "answer_by": o.get("answer_by"),
                    "done": _link("Record their answer", offers.href(o["candidate_id"], o["job_id"]))})
    out.sort(key=lambda i: (not i["overdue"], i["answer_by"] or "9999"))
    return out


def _followup_items(store: Store, c: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    out = []
    for j in c["jobs"]:
        if j["status"] != "open":
            continue
        for p in outreach.due(store, j["id"], now):
            out.append({"kind": "followup", "title": f"{p['label']} to {p['name']}",
                        "detail": f"{p['job']} - step {p['step']} of {p['steps']}, {outreach.due_text(p, now)}",
                        "candidate_id": p["candidate_id"], "job_id": j["id"], "overdue": p["overdue"],
                        "due_on": p["due_on"], "text": p["draft"],
                        "done": _done("Mark sent", f"/jobs/{j['id']}/followups/{p['candidate_id']}/sent"),
                        "replied": _done("They replied", f"/jobs/{j['id']}/followups/{p['candidate_id']}/replied")})
    out.sort(key=lambda i: (i["due_on"], i["title"]))
    return out


def desk(store: Store, key: str, now: datetime | None = None) -> dict[str, Any]:
    """One client's run sheet: {"client", "key", "slug", "sections": [{"kind", "label", "items", "count", "more"}],
    "count"}. Sections with nothing in them are kept (count 0) so the page can say they are clear. Items are capped
    per section (MAX_ITEMS), most urgent first."""
    now = now or datetime.now(timezone.utc)
    c = reports.client(store, key)
    open_ids = {j["id"] for j in c["jobs"] if j["status"] == "open"}
    built = {"update": _update_items(store, c, now), "chase": _chase_items(store, c, now),
             "feedback": _feedback_items(store, open_ids, now), "offer": _offer_items(store, open_ids, now),
             "followup": _followup_items(store, c, now)}
    sections = []
    for kind, label in SECTIONS:
        items = built[kind]
        sections.append({"kind": kind, "label": label, "items": items[:MAX_ITEMS], "count": len(items),
                         "more": max(0, len(items) - MAX_ITEMS)})
    # An update that is only a draft (not due) is not work to do, so it doesn't count toward what is open.
    todo = sum(1 for k, v in built.items() if k != "update" for i in v if not i.get("not_due")) + sum(
        1 for i in built["update"] if i["due"])
    return {"client": c["name"], "key": c["key"], "slug": c["slug"], "sections": sections, "count": todo,
            "own": actions.inhouse(store), "now": now.isoformat()}


def desk_text(d: dict[str, Any]) -> str:
    """The desk as text for the AI app: each section in work order, with the paste-ready text under each item."""
    lines = [f"Client desk for {d['client']}: " + (f"{d['count']} thing{'' if d['count'] == 1 else 's'} to do."
                                                    if d["count"] else "nothing to do right now.")
             + " Nothing is sent by Talent Search; the recruiter sends each message from their own mail."]
    for s in d["sections"]:
        if not s["count"]:
            continue
        lines.append(f"\n## {s['label']} ({s['count']})")
        for i in s["items"]:
            lines.append(f"- {i['title']}. {i['detail']}")
            if i.get("text"):
                lines.append("  " + i["text"].replace("\n", "\n  "))
        if s["more"]:
            lines.append(f"- and {s['more']} more on the desk page.")
    return "\n".join(lines)


def tool_data(d: dict[str, Any], app_url: str) -> dict[str, Any]:
    """The structured result: items without the form details (the tool only reads)."""
    keep = ("kind", "title", "detail", "text", "to", "subject", "candidate_id", "job_id", "overdue", "due", "owed",
            "people", "answer_by", "due_on", "last_chased_at", "not_due", "nudge_on")
    return {"client": d["client"], "to_do": d["count"], "app_url": app_url,
            "sections": [{"kind": s["kind"], "label": s["label"], "count": s["count"], "more": s["more"],
                          "items": [{k: i[k] for k in keep if k in i} for i in s["items"]]} for s in d["sections"]]}
