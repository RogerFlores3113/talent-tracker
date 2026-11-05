"""Offers and close: one offer record per person and job.

An offer holds its terms (salary, equity and bonus as free text, a start date), the date an answer is due, notes
from the offer call and where it stands: drafted, out (the candidate has it), accepted, declined or withdrawn (the
company pulled it). The recruiter opens it from a person in the job's offer stage (Today asks when one has none),
from Decide or the board card. Recording an answer offers the next move in one click: accepted to the job's hired
stage, declined or withdrawn to an out stage with the reason. That move is an ordinary app move, undone on the
timeline.

Offer terms are private. They never go in a client update or a pasted status update. Only when the recruiter ticks
"Share offer status" does the status go there, as words ("offer out, answer due Fri Nov 13"), never the numbers.
The paste-ready summary is for the recruiter's own email to the candidate; the app sends nothing.

The AI app writes offers with record_offer and record_offer_answer. Each write is logged in ai_actions (kinds
offer_terms and offer_answer) with the offer as it was, so Changes can undo it; it waits on Review in Cautious mode,
counts toward the hourly overwrite cap, and at most AI_NOTE_CAP offer writes per person go through in an hour.
Rows live in the offers table (store migration v11).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.types import CallToolResult
from pydantic import WithJsonSchema

from . import actions
from .wording import who
from .store import AI_NOTE_CAP, AI_EDIT_WINDOW_MINUTES, NotFound, Store, _stage_key, as_local, now_iso, quoted, to_dt

STATUSES = ("drafted", "out", "accepted", "declined", "withdrawn")
ANSWERS = ("accepted", "declined", "withdrawn")
STATUS_WORDS = {"drafted": "Drafted", "out": "Out with the candidate", "accepted": "Accepted", "declined": "Declined",
                "withdrawn": "Withdrawn"}
_ALIASES = {"draft": "drafted", "drafting": "drafted", "sent": "out", "extended": "out", "made": "out", "open": "out",
            "accept": "accepted", "yes": "accepted", "signed": "accepted", "decline": "declined", "no": "declined",
            "turned down": "declined", "rejected": "declined", "withdraw": "withdrawn", "pulled": "withdrawn",
            "rescinded": "withdrawn"}
TERMS = ("salary", "equity", "bonus", "start_date", "answer_by", "notes")
LABELS = {"salary": "salary", "equity": "equity", "bonus": "bonus", "start_date": "start date",
          "answer_by": "answer-by date", "notes": "notes", "status": "status", "reason": "reason",
          "share_status": "sharing"}
LIMITS = {"salary": 200, "equity": 200, "bonus": 200, "notes": 4000, "reason": 500}
# What one snapshot of an offer holds, for Undo and for Review to tell it changed since.
SNAP = ("status", *TERMS, "reason", "share_status", "out_at", "answered_at")
ANSWER_DAYS = 14  # how long an answered offer offers its next move on Today
DUE_SOON_DAYS = 3  # an offer out shows on Today from this many days before its answer is due
MISSING_SHOWN = 1  # imported people in an offer stage with no offer: more than this fold into one Today line
AI_KINDS = ("offer_terms", "offer_answer")


# ------------------------------------------------------------------ reading
def _today(now: datetime | None = None) -> date:
    return as_local(now or datetime.now(timezone.utc)).date()


def get(store: Store, candidate_id: int, job_id: int) -> dict[str, Any] | None:
    return store._row("SELECT * FROM offers WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id))


def snap(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return {k: row.get(k) for k in SNAP} if row else None


def offer_like(stage: str | None) -> bool:
    """A stage named like Offer (Offer, Offer out, Verbal offer)."""
    return "offer" in _stage_key(stage).split()


def href(candidate_id: int, job_id: int) -> str:
    return f"/jobs/{job_id}/offers/{candidate_id}"


def _job_name(row: dict[str, Any]) -> str:
    title = row.get("job_title") or row.get("title") or "a job"
    company = row.get("job_company") or row.get("company")
    return f"{title} ({company})" if company else str(title)


def _status(value: Any, allowed: tuple[str, ...]) -> str:
    key = " ".join(str(value or "").strip().lower().replace("_", " ").split()).strip(" .!")
    key = _ALIASES.get(key, key)
    if key not in allowed:
        if allowed == ("drafted", "out"):
            raise ValueError(
                f"No change made. {str(value)[:40]!r} isn't a status record_offer takes. Use drafted (terms still "
                "being worked out) or out (the candidate has the offer). To record their answer, call "
                "record_offer_answer with accepted, declined or withdrawn.")
        raise ValueError(f"Status must be one of: {', '.join(allowed)}. accepted means they said yes, declined "
                         "that they said no, and withdrawn that the company pulled the offer.")
    return key


def _day_word(d: date, today: date) -> str:
    """'today', 'tomorrow', 'yesterday', 'Fri' within the week either side, else 'Fri Nov 20'."""
    gap = (d - today).days
    if gap == 0:
        return "today"
    if gap == 1:
        return "tomorrow"
    if gap == -1:
        return "yesterday"
    return actions.day_text(d, "%a" if -6 <= gap <= 6 else "%a %b %-d")


def _as_day(iso: str | None) -> date | None:
    try:
        return date.fromisoformat(str(iso)[:10]) if iso else None
    except ValueError:
        return None


def overdue(offer: dict[str, Any], now: datetime | None = None) -> bool:
    due = _as_day(offer.get("answer_by"))
    return offer.get("status") == "out" and due is not None and due < _today(now)


def status_line(offer: dict[str, Any], now: datetime | None = None) -> str:
    """'Offer out 3 days, answer due Fri', 'Offer drafted, not out yet', 'Offer accepted Nov 5, starts Mon Dec 1'."""
    today, st = _today(now), offer.get("status")
    if st == "drafted":
        return "Offer drafted, not out yet"
    if st == "out":
        sent = to_dt(offer.get("out_at"))
        days = (today - as_local(sent).date()).days if sent else None
        text = ("Offer out" if days is None else "Offer out today" if days <= 0
                else f"Offer out {days} day{'' if days == 1 else 's'}")
        due = _as_day(offer.get("answer_by"))
        if due:
            text += f", answer {'was due' if due < today else 'due'} {_day_word(due, today)}"
        return text
    answered = to_dt(offer.get("answered_at"))
    when = f" {actions.day_text(as_local(answered), '%b %-d')}" if answered else ""
    if st == "accepted":
        start = _as_day(offer.get("start_date"))
        return f"Offer accepted{when}" + (f", starts {actions.day_text(start, '%a %b %-d')}" if start else "")
    return f"Offer {st}{when}" + (f": {offer['reason']}" if offer.get("reason") else "")


def note(offer: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any] | None:
    """What the profile and the board card show for an offer: {"line", "href", "overdue", "status"}."""
    if not offer:
        return None
    return {"line": status_line(offer, now), "href": href(offer["candidate_id"], offer["job_id"]),
            "overdue": overdue(offer, now), "status": offer["status"]}


def on_job(store: Store, job_id: int) -> dict[int, dict[str, Any]]:
    """{candidate id: note} for the job's board: every offer, and "missing" for people in an offer stage with none."""
    out = {o["candidate_id"]: note(o) for o in store._rows("SELECT * FROM offers WHERE job_id = ?", (job_id,))}
    for a in store._rows("""SELECT a.candidate_id, a.stage FROM applications a JOIN job_stages s
                              ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
                            WHERE a.job_id = ? AND s.outcome = 'open'""", (job_id,)):
        if a["candidate_id"] not in out and offer_like(a["stage"]):
            out[a["candidate_id"]] = {"line": None, "missing": True, "href": href(a["candidate_id"], job_id)}
    return out


def of_candidate(store: Store, candidate_id: int) -> dict[int, dict[str, Any]]:
    """{job id: note} for a profile, with "missing" where they sit in an offer stage with no offer recorded."""
    out = {o["job_id"]: note(o) for o in store._rows("SELECT * FROM offers WHERE candidate_id = ?", (candidate_id,))}
    for a in store._rows("""SELECT a.job_id, a.stage FROM applications a JOIN job_stages s
                              ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
                            WHERE a.candidate_id = ? AND s.outcome = 'open'""", (candidate_id,)):
        if a["job_id"] not in out and offer_like(a["stage"]):
            out[a["job_id"]] = {"line": None, "missing": True, "href": href(candidate_id, a["job_id"])}
    return out


def listed(store: Store, job_id: int | None = None) -> list[dict[str, Any]]:
    """Offers with names, newest first (one job's, or every job's)."""
    return store._rows(
        """SELECT o.*, c.name AS candidate_name, j.title AS job_title, j.company AS job_company, j.status AS job_status
           FROM offers o JOIN candidates c ON c.id = o.candidate_id JOIN jobs j ON j.id = o.job_id
           WHERE (? IS NULL OR o.job_id = ?) ORDER BY o.updated_at DESC, o.id DESC""", (job_id, job_id))


# ------------------------------------------------------------------ writing
def _pair(store: Store, candidate_id: int, job_id: int) -> dict[str, Any]:
    """The person, the job and their stage, or a plain error when they aren't in that job."""
    c, j = store.get_candidate(candidate_id), store.get_job(job_id)
    app = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id))
    if app is None:
        raise ValueError(f"{quoted(c['name'])} isn't in the pipeline of {_job_name(j)}. Add them to the job first.")
    return {"candidate": c, "job": j, "stage": app["stage"]}


_MONEY = re.compile(r"^\s*(?P<sym>[$\u20ac\u00a3])?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<k>[kKmM])?\s*$")
_MONTH_DAY = re.compile(r"\b(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<d>\d{1,2})"
                        r"(?:st|nd|rd|th)?(?:,?\s+(?P<y>\d{4}))?\b|\b(?P<d2>\d{1,2})(?:st|nd|rd|th)?\s+"
                        r"(?P<mon2>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s+(?P<y2>\d{4}))?\b",
                        re.I)
_FILLER = re.compile(r"^(?:by|on|before|until|due|around|about|the)\s+", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def money_text(value: Any) -> Any:
    """A number or a money string as readable text: 180000 -> '180,000', '$180k' -> '$180,000'. Other text stays
    as it is ('$185,000 base' is already fine)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    m = _MONEY.match(str(value))
    if not m:
        return value
    num = float(m.group("num").replace(",", ""))
    num *= {"k": 1_000, "m": 1_000_000}.get((m.group("k") or "").lower(), 1)
    body = f"{num:,.0f}" if float(num).is_integer() else f"{num:,.2f}"
    return (m.group("sym") or "") + body


def _day(value: Any, label: str, today: date) -> str | None:
    v = str(value).strip() if value is not None else ""
    if not v:
        return None
    text = v
    while True:  # "by Friday", "on Nov 13th"
        shorter = _FILLER.sub("", text)
        if shorter == text:
            break
        text = shorter
    m = _MONTH_DAY.search(text)  # "Nov 13", "Friday Nov 13", "13 Nov 2026": the user's own year, not the computer's
    if m:
        mon = (m.group("mon") or m.group("mon2")).lower()[:3]
        day, year = int(m.group("d") or m.group("d2")), m.group("y") or m.group("y2")
        try:
            found = date(int(year) if year else today.year, _MONTHS.index(mon) + 1, day)
            if not year and found < today:
                found = date(today.year + 1, found.month, found.day)
            return found.isoformat()
        except ValueError:
            pass
    try:
        return actions.parse_day(text, today).isoformat()
    except ValueError:
        raise ValueError(f"Couldn't read the {label} {v!r}. Try 2026-12-01, Dec 1, friday or tomorrow.") from None


def clean(given: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Offer fields as they are saved: text trimmed and within its limit, dates as YYYY-MM-DD, status drafted or out.
    An empty value clears the field. Raises ValueError on a value the app would refuse."""
    out: dict[str, Any] = {}
    today = _today(now)
    for k, v in given.items():
        if k in ("start_date", "answer_by"):
            out[k] = _day(v, LABELS[k], today)
        elif k == "status":
            out[k] = _status(v, ("drafted", "out"))
        elif k == "share_status":
            out[k] = 1 if v and str(v).lower() not in ("0", "false", "off", "no") else 0
        elif k in LIMITS:
            text = str(money_text(v) if k in ("salary", "bonus") else (v if v is not None else "")).strip()
            if k != "notes":
                text = " ".join(text.split())
            if len(text) > LIMITS[k]:
                raise ValueError(f"The {LABELS[k]} is too long ({len(text):,} characters). The limit is {LIMITS[k]:,}.")
            out[k] = text or None
        else:
            raise ValueError(f"Unknown offer field {k}. It accepts: {', '.join(TERMS)}, status.")
    return out


def _insert(store: Store, candidate_id: int, job_id: int) -> dict[str, Any]:
    _pair(store, candidate_id, job_id)
    ts = now_iso()
    store._conn.execute("INSERT INTO offers(candidate_id, job_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                        (candidate_id, job_id, ts, ts))
    return get(store, candidate_id, job_id) or {}


def _update(store: Store, offer_id: int, values: dict[str, Any]) -> None:
    cols = ", ".join(f"{k} = ?" for k in values)
    store._conn.execute(f"UPDATE offers SET {cols}, updated_at = ? WHERE id = ?", (*values.values(), now_iso(), offer_id))


def save(store: Store, candidate_id: int, job_id: int, *, by: str = "app", now: datetime | None = None,
         **given: Any) -> dict[str, Any]:
    """Create the offer (drafted) or change its terms, status (drafted or out) and sharing.
    {"offer", "created", "changed": {field: (old, new)}, "action_id"}. by="ai" logs the write for Changes."""
    fields = clean(given, now)
    if by == "ai":
        fields.pop("share_status", None)  # sharing is the recruiter's own tick
    with store._write():
        row = get(store, candidate_id, job_id)
        created = row is None
        before = snap(row)
        if created:
            row = _insert(store, candidate_id, job_id)
        changed = {k: (row.get(k), v) for k, v in fields.items() if row.get(k) != v}
        values = {k: v for k, (_old, v) in changed.items()}
        if "status" in changed:
            values["out_at"] = (row.get("out_at") or now_iso()) if values["status"] == "out" else None
            if row["status"] in ANSWERS:  # back to drafted or out: the answer no longer stands
                values.update(reason=None, answered_at=None)
        if values:
            _update(store, row["id"], values)
        after = get(store, candidate_id, job_id) or {}
        action_id = None
        if by == "ai" and (changed or created):
            action_id = store._log_ai("offer_terms", candidate_id=candidate_id, job_id=job_id, target_id=after["id"],
                                      body=", ".join(LABELS[k] for k in changed) or None, old=before, new=snap(after))
    return {"offer": after, "created": created, "changed": changed, "action_id": action_id}


def record_answer(store: Store, candidate_id: int, job_id: int, answer: str, reason: str | None = None, *,
                  by: str = "app") -> dict[str, Any]:
    """The candidate accepted or declined, or the company withdrew the offer. Creates the offer if there is none.
    {"offer", "created", "changed", "action_id", "next"} where next is the move the app offers (see next_move)."""
    status = _status(answer, ANSWERS)
    why = clean({"reason": reason})["reason"]
    with store._write():
        row = get(store, candidate_id, job_id)
        created = row is None
        before = snap(row)
        if created:
            row = _insert(store, candidate_id, job_id)
        changed = created or row["status"] != status or (row.get("reason") or None) != why
        if changed:
            _update(store, row["id"], {"status": status, "reason": why, "answered_at": now_iso()})
        after = get(store, candidate_id, job_id) or {}
        action_id = None
        if by == "ai" and changed:
            action_id = store._log_ai("offer_answer", candidate_id=candidate_id, job_id=job_id, target_id=after["id"],
                                      body=status, old=before, new=snap(after))
    return {"offer": after, "created": created, "changed": changed, "action_id": action_id,
            "next": next_move(store, after)}


def next_move(store: Store, offer: dict[str, Any]) -> dict[str, Any] | None:
    """The move an answer calls for, while the person isn't there yet: {"kind": "hired" or "out", "to", "notes"}."""
    st = offer.get("status")
    if st not in ANSWERS:
        return None
    app = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?",
                     (offer["candidate_id"], offer["job_id"]))
    if app is None:
        return None
    stages = store.job_stages(offer["job_id"])
    cur = next((s for s in stages if s["name"].lower() == app["stage"].lower()), None)
    if st == "accepted":
        if cur and cur["outcome"] == "hired":
            return None
        return {"kind": "hired", "to": next((s["name"] for s in stages if s["outcome"] == "hired"), "Hired"),
                "notes": None}
    if cur and cur["outcome"] == "lost":
        return None
    lost = [s["name"] for s in stages if s["outcome"] == "lost"]
    to = None
    if st == "declined":  # the candidate said no: their own way out, when the job has one
        to = next((n for n in lost if "declin" in _stage_key(n)), None) or next(
            (n for n in lost if "withdr" in _stage_key(n)), None)
    to = to or actions.out_stage_name(store, offer["job_id"])
    why = ("Declined the offer" if st == "declined" else "Offer withdrawn") + (
        f": {offer['reason']}" if offer.get("reason") else "")
    return {"kind": "out", "to": to, "notes": f"Reason: {why}"}


def move(store: Store, candidate_id: int, job_id: int, kind: str) -> dict[str, Any]:
    """Make the move the offer's answer calls for, as an app move (undone on the timeline)."""
    offer = get(store, candidate_id, job_id)
    nxt = next_move(store, offer) if offer else None
    if not nxt or nxt["kind"] != kind:
        raise ValueError("That move isn't needed any more. Reload the page.")
    if not any(s["name"].lower() == nxt["to"].lower() for s in store.job_stages(job_id)):
        store.add_stage(job_id, nxt["to"], outcome="hired" if kind == "hired" else "lost")
    return store.move_stage(candidate_id, job_id, nxt["to"], notes=nxt["notes"], by="app")


def stage_nudge(store: Store, candidate_id: int, job_id: int) -> dict[str, Any] | None:
    """After an offer is recorded for someone still in an earlier in-play stage: the move_stage step to the job's
    Offer stage, as {"nudge": sentence, "suggested_step": {...}}, like log_activity's. None when they're there
    already (or past it, hired or out), or the job has no stage named like Offer."""
    app = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id))
    if app is None or offer_like(app["stage"]):
        return None
    stages = store.job_stages(job_id)
    at = next((i for i, s in enumerate(stages) if s["name"].lower() == app["stage"].lower()), None)
    to = next((i for i, s in enumerate(stages) if s["outcome"] == "open" and offer_like(s["name"])), None)
    if at is None or to is None or stages[at]["outcome"] != "open" or at > to:
        return None
    name, target = store.get_candidate(candidate_id)["name"], stages[to]["name"]
    nudge = (f"{quoted(name)} is still in {app['stage']} on {_job_name(store.get_job(job_id))} with an offer "
             f"recorded. If the offer is being made, call move_stage(candidate_id={candidate_id}, job_id={job_id}, "
             f"stage=\"{target}\") so the board and status updates show it; ask the recruiter if you're not sure.")
    return {"nudge": nudge, "suggested_step": {"tool": "move_stage", "candidate_id": candidate_id, "job_id": job_id,
                                               "stage": target}}


# ------------------------------------------------------------------ text
def summary_text(store: Store, offer: dict[str, Any]) -> str:
    """A paste-ready summary of the terms for the recruiter's own email to the candidate. Notes stay out."""
    c, j = store.get_candidate(offer["candidate_id"]), store.get_job(offer["job_id"])
    first = (c.get("name") or "").split(" ")[0] or "there"
    role = j["title"] + (f" at {j['company']}" if j.get("company") else "")
    lines = [f"Hi {first},", "", f"Here is a summary of the offer for {role}:", ""]
    for k, label in (("salary", "Salary"), ("equity", "Equity"), ("bonus", "Bonus")):
        if offer.get(k):
            lines.append(f"{label}: {offer[k]}")
    start, due = _as_day(offer.get("start_date")), _as_day(offer.get("answer_by"))
    if start:
        lines.append(f"Start date: {actions.day_text(start, '%A, %B %-d, %Y')}")
    if lines[-1] == "":
        lines.append("The details are on their way.")
    lines.append("")
    if due:
        lines.append(f"Please let me know your answer by {actions.day_text(due, '%A, %B %-d')}.")
    lines.append("I'm happy to talk any of it through.")
    return "\n".join(lines)


def client_status(offer: dict[str, Any]) -> str:
    """Where an offer stands as a client update says it: words only, never the numbers or the reason."""
    st = offer["status"]
    if st == "drafted":
        return "offer being prepared"
    if st == "out":
        due = _as_day(offer.get("answer_by"))
        return "offer out" + (f", answer due {actions.day_text(due, '%a %b %-d')}" if due else "")
    return f"offer {st}"


def shared_lines(store: Store, job_ids: list[int], since: str | None = None, titles: bool = True) -> list[str]:
    """'- Marcus Oyelaran (Backend Engineer): offer out, answer due Fri Nov 13' for the offers the recruiter chose
    to share, on these jobs. An answered offer shows in the update that covers its answer (answered since `since`)."""
    if not job_ids:
        return []
    marks = ", ".join("?" * len(job_ids))
    out = []
    for o in store._rows(f"""SELECT o.*, c.name AS candidate_name, j.title AS job_title FROM offers o
                              JOIN candidates c ON c.id = o.candidate_id JOIN jobs j ON j.id = o.job_id
                              WHERE o.share_status = 1 AND o.job_id IN ({marks}) ORDER BY o.updated_at, o.id""",
                         tuple(job_ids)):
        if o["status"] in ANSWERS and since and (o.get("answered_at") or "") < str(since):
            continue
        out.append(f"- {o['candidate_name']}" + (f" ({o['job_title']})" if titles else "") + f": {client_status(o)}")
    return out


def recruiter_lines(store: Store, job_id: int) -> list[str]:
    """Every offer on one job as the recruiter reads it (status words, no numbers)."""
    return [f"{o['candidate_name']}: {status_line(o)}" for o in listed(store, job_id)]


# ------------------------------------------------------------------ today
def _hired_pairs(store: Store) -> set[tuple[int, int]]:
    return {(r["candidate_id"], r["job_id"]) for r in store._rows(
        """SELECT a.candidate_id, a.job_id FROM applications a JOIN job_stages s
             ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage) WHERE s.outcome = 'hired'""")}


def today_items(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """Today: an offer whose answer is due within DUE_SOON_DAYS (or overdue), an answered offer whose move isn't
    made yet, a hire whose offer still reads drafted or out, and someone in an offer stage with no offer recorded
    (one line for the ones an import or sync put there)."""
    now = now or datetime.now(timezone.utc)
    today = _today(now)
    since = (now - timedelta(days=ANSWER_DAYS)).replace(microsecond=0).isoformat()
    items: list[dict[str, Any]] = []
    hired = _hired_pairs(store)
    for o in listed(store):
        if o["job_status"] == "closed":
            continue
        name, job, link = o["candidate_name"], _job_name(o), href(o["candidate_id"], o["job_id"])
        base = {"kind": "offer", "candidate_id": o["candidate_id"], "job_id": o["job_id"], "href": link}
        due = _as_day(o.get("answer_by"))
        if o["status"] in ("drafted", "out") and (o["candidate_id"], o["job_id"]) in hired:
            items.append({**base, "key": f"offer_hired:{o['id']}:{o['status']}", "rank": 1, "when": o["updated_at"],
                          "button": "Open offer",
                          "title": f"{name} is hired on {job}, but the offer still reads {o['status']}. "
                                   "Mark the offer accepted?",
                          "detail": status_line(o, now) + ". Record the answer on the offer page."})
        elif o["status"] == "out" and due and due <= today + timedelta(days=DUE_SOON_DAYS):
            late = due < today
            items.append({**base, "key": f"offer_due:{o['id']}:{o['answer_by']}", "rank": 0 if late else 1,
                          "when": o["answer_by"] + "T00:00:00+00:00", "overdue": late, "button": "Open offer",
                          "title": f"Offer to {name} on {job}: answer {'was due' if late else 'due'} "
                                   f"{_day_word(due, today)}",
                          "detail": status_line(o, now) + ". Record their answer, or move the date."})
        elif o["status"] in ANSWERS and (o.get("answered_at") or "") >= since:
            nxt = next_move(store, o)
            if nxt:
                said = {"accepted": f"{name} accepted the offer", "declined": f"{name} declined the offer",
                        "withdrawn": f"The offer to {name} was withdrawn"}[o["status"]]
                items.append({**base, "key": f"offer_next:{o['id']}:{o['status']}", "rank": 1,
                              "when": o["answered_at"], "move_to": nxt["to"], "move_kind": nxt["kind"],
                              "move_url": link + "/move", "button": "Open offer",
                              "title": f"{said} on {job}. Move them to {nxt['to']}?",
                              "detail": "One click, and you can undo it on their timeline."})
    from .importers import file_entries  # here: importers imports actions

    from_file = file_entries(store)
    imported: list[dict[str, Any]] = []
    for a in store._rows("""SELECT a.candidate_id, a.job_id, a.stage, a.stage_changed_at, c.name, j.title, j.company
                            FROM applications a JOIN jobs j ON j.id = a.job_id JOIN candidates c ON c.id = a.candidate_id
                            JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
                            WHERE j.status = 'open' AND s.outcome = 'open' AND NOT EXISTS (
                              SELECT 1 FROM offers o WHERE o.candidate_id = a.candidate_id AND o.job_id = a.job_id)"""):
        if not offer_like(a["stage"]):
            continue
        if (a["candidate_id"], a["job_id"]) in from_file:  # an import or a sync put them there, not a move here
            imported.append(a)
            continue
        items.append(_missing_item(a))
    if len(imported) <= MISSING_SHOWN:
        items += [_missing_item(a) for a in imported]
    elif imported:
        jobs = sorted({a["job_id"] for a in imported})
        names = ", ".join(a["name"] for a in imported[:3]) + (f" and {len(imported) - 3} more" if len(imported) > 3
                                                               else "")
        items.append({"key": "offer_missing_import", "kind": "offer", "rank": 3,
                      "when": max(a["stage_changed_at"] or "" for a in imported), "job_id": jobs[0] if len(jobs) == 1
                      else None, "href": f"/jobs/{jobs[0]}" if len(jobs) == 1 else "/jobs?status=open",
                      "button": "See the job" if len(jobs) == 1 else "See the jobs", "count": len(imported),
                      "title": f"{len(imported)} people from an import are in an Offer stage with no offer recorded",
                      "detail": f"{names}, on {len(jobs)} job{'' if len(jobs) == 1 else 's'}. "
                                "Record an offer from their card when it matters."})
    return items


def _missing_item(a: dict[str, Any]) -> dict[str, Any]:
    return {"key": f"offer_missing:{a['candidate_id']}:{a['job_id']}", "kind": "offer", "rank": 2,
            "when": a["stage_changed_at"], "candidate_id": a["candidate_id"], "job_id": a["job_id"],
            "href": href(a["candidate_id"], a["job_id"]), "button": "Record the offer",
            "title": f"{a['name']} is in {a['stage']} on {_job_name(a)} with no offer recorded",
            "detail": "Add the terms, the answer-by date and notes from the offer call."}


# ------------------------------------------------------------------ AI log, undo, caps
def overwrite_count(kind: str, old: dict[str, Any], new: dict[str, Any]) -> int:
    """Saved offer values one AI write replaced or cleared (an answer counts as one)."""
    if kind == "offer_answer":
        return 1
    return sum(1 for k in TERMS + ("status",) if (old or {}).get(k) not in (None, "") and (new or {}).get(k) != old[k])


def ai_summary(r: dict[str, Any]) -> str:
    """The Changes line for an AI offer write. Field names only, never the terms themselves."""
    name, job = quoted(r.get("candidate_name") or "someone"), _job_name(r)
    if r["kind"] == "offer_answer":
        st = (r.get("new_data") or {}).get("status") or "answered"
        return f"Recorded that {name} {st} the offer on {job}" if st != "withdrawn" else \
            f"Recorded that the offer to {name} on {job} was withdrawn"
    verb = "Recorded an offer for" if r.get("old_data") is None else "Changed the offer for"
    return f"{verb} {name} on {job}" + (f": {r['body']}" if r.get("body") else "")


def ai_blocker(store: Store, a: dict[str, Any]) -> str | None:
    row = store._row("SELECT * FROM offers WHERE id = ?", (a.get("target_id"),))
    if row is None:
        return "that offer is gone"
    return None if snap(row) == (a.get("new_data") or {}) else "the offer was changed again since"


def ai_undo(store: Store, a: dict[str, Any]) -> str:
    """Put an offer back as it was before the AI app's write (an offer it made is taken out again)."""
    reason = ai_blocker(store, a)
    if reason:
        raise ValueError(reason[:1].upper() + reason[1:] + ".")
    old = a.get("old_data")
    with store._write():
        if old is None:
            store._conn.execute("DELETE FROM offers WHERE id = ?", (a["target_id"],))
        else:
            _update(store, a["target_id"], {k: old.get(k) for k in SNAP})
        store._conn.execute("UPDATE ai_actions SET undone_at = ? WHERE id = ?", (now_iso(), a["id"]))
    return a["summary"]


def ai_writes_this_hour(store: Store, candidate_id: int) -> int:
    """Offer writes the AI app made on this person in the last hour, plus those waiting for review."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=AI_EDIT_WINDOW_MINUTES)).replace(microsecond=0).isoformat()
    done = store._row("SELECT count(*) AS n FROM ai_actions WHERE candidate_id = ? AND kind IN (?, ?) AND at >= ?",
                      (candidate_id, *AI_KINDS, since))["n"]
    waiting = store._row("SELECT count(*) AS n FROM ai_review WHERE candidate_id = ? AND kind = 'offer'"
                         " AND status = 'pending' AND created_at >= ?", (candidate_id, since))["n"]
    return done + waiting


def check_cap(store: Store, candidate_id: int) -> None:
    if ai_writes_this_hour(store, candidate_id) >= AI_NOTE_CAP:
        name = store.get_candidate(candidate_id)["name"]
        raise ValueError(f"No change made. Your AI app already changed the offer for {quoted(name)} {AI_NOTE_CAP} "
                         "times this hour. Ask the recruiter to make more changes in the app, or try again later.")


# ------------------------------------------------------------------ Cautious mode
def queue(store: Store, op: str, candidate_id: int, job_id: int, args: dict[str, Any],
          session: str | None) -> dict[str, Any]:
    """Hold an AI offer write (op "terms" with cleaned fields, or "answer") for the recruiter's review."""
    from . import review

    pair = _pair(store, candidate_id, job_id) if get(store, candidate_id, job_id) is None else None
    c = pair["candidate"] if pair else store.get_candidate(candidate_id)
    job = _job_name(pair["job"] if pair else store.get_job(job_id))
    if op == "answer":
        st = args["answer"]
        summary = (f"Record that the offer to {quoted(c['name'])} on {job} was withdrawn" if st == "withdrawn"
                   else f"Record that {quoted(c['name'])} {st} the offer on {job}")
    else:
        summary = f"Record the offer for {quoted(c['name'])} on {job}" + (
            ": " + ", ".join(LABELS[k] for k in args["fields"]) if args["fields"] else "")
    return review._queue(store, "offer", summary, {"op": op, **args, "before": snap(get(store, candidate_id, job_id))},
                         candidate_id=candidate_id, job_id=job_id, session=session)


def apply_review(store: Store, r: dict[str, Any]) -> str:
    """Make a kept offer write, once, unless the offer changed since it was sent."""
    p, cid, jid = r["payload"], r.get("candidate_id"), r.get("job_id")
    if cid is None or jid is None:
        raise ValueError("That person or job is gone.")
    if snap(get(store, cid, jid)) != p.get("before"):
        raise ValueError("The offer was changed since, so nothing was changed.")
    if p.get("op") == "answer":
        record_answer(store, cid, jid, p["answer"], p.get("reason"), by="ai")
    else:
        save(store, cid, jid, by="ai", **(p.get("fields") or {}))
    return r["summary"]


def review_lines(r: dict[str, Any]) -> list[dict[str, str]]:
    """What a waiting offer write would do, one line per field, for Review."""
    p = r["payload"]
    before = p.get("before") or {}
    if p.get("op") == "answer":
        out = [{"label": "answer", "old": STATUS_WORDS.get(before.get("status"), "(no offer yet)"),
                "new": STATUS_WORDS.get(p.get("answer"), str(p.get("answer")))}]
        if p.get("reason"):
            out.append({"label": "reason", "old": before.get("reason") or "(empty)", "new": p["reason"]})
        return out
    return [{"label": LABELS.get(k, k), "old": str(STATUS_WORDS.get(before.get(k), before.get(k)) or "(empty)"),
             "new": str(STATUS_WORDS.get(v, v) or "(cleared)")} for k, v in (p.get("fields") or {}).items()]


# ------------------------------------------------------------------ MCP tools
def register_tools(mcp: Any, store: Store, session: str) -> None:
    """record_offer, record_offer_answer and list_offers on the MCP server."""
    from . import review
    from .mcp_server import DATA_NOTE, EDIT, READ, _check_cap, _plain, _wait_text, _waiting
    from .paths import app_url

    def public(o: dict[str, Any]) -> dict[str, Any]:
        return {"id": o["id"], "candidate_id": o["candidate_id"], "job_id": o["job_id"], "status": o["status"],
                **{k: o.get(k) for k in TERMS if o.get(k)}, "reason": o.get("reason"),
                "status_line": status_line(o), "app_url": app_url(href(o["candidate_id"], o["job_id"]))}

    def _read_days(days: dict[str, str]) -> str:
        """' I read the answer-by date as Fri Nov 13.' so a fuzzy 'Friday' is checked back."""
        bits = [f"{LABELS[k]} as {actions.day_text(date.fromisoformat(v), '%a %b %-d')}" for k, v in days.items()]
        return f" I read the {' and the '.join(bits)}." if bits else ""

    @mcp.tool(annotations=EDIT)
    def record_offer(candidate_id: int, job_id: int, salary: str | int | float | None = None,
                     equity: str | int | float | None = None, bonus: str | int | float | None = None,
                     start_date: str | None = None, answer_by: str | None = None,
                     notes: str | None = None,
                     status: Annotated[str | None, WithJsonSchema({
                         "anyOf": [{"type": "string", "enum": ["drafted", "out"]}, {"type": "null"}],
                         "default": None})] = None) -> CallToolResult:
        """Create or change the offer for one person on one job. salary, equity and bonus are free text ("$185,000
        base", "0.4% over 4 years") or a plain number (180000, or "180k"); start_date and answer_by are days
        (2026-12-01, Nov 13, friday, next fri) and the reply says which day it read; notes are what
        was said on the offer call. status is drafted while the terms are worked out, out once the candidate has
        the offer. Also accepted: base_salary, comp, start, deadline and note. Leave a field out to keep it; "" clears it (one per call). Offer terms are private: never put
        them in a status update, a client message or anything pasted to others. Record the answer with
        record_offer_answer. The recruiter sees every change on Changes and can undo it."""
        given = {k: v for k, v in {"salary": salary, "equity": equity, "bonus": bonus, "start_date": start_date,
                                   "answer_by": answer_by, "notes": notes, "status": status}.items() if v is not None}
        row = get(store, candidate_id, job_id)
        pair = _pair(store, candidate_id, job_id) if row is None else None
        name = (pair["candidate"] if pair else store.get_candidate(candidate_id))["name"]
        job = _job_name(pair["job"] if pair else store.get_job(job_id))
        if row is not None and not given:
            raise ValueError("Nothing to change. Give salary, equity, bonus, start_date, answer_by, notes or status.")
        fields = clean(given)
        days = {k: fields[k] for k in ("start_date", "answer_by") if fields.get(k)}
        changed = {k: v for k, v in fields.items() if (row or {}).get(k) != v}
        if row is not None and sum(1 for k, v in changed.items() if v is None and row.get(k)) > 1:
            raise ValueError("Clear one field per call, and only when you're sure. Nothing was changed.")
        if row is not None and not changed:
            return _plain(f"Nothing changed: the offer for {quoted(name)} on {job} already says that.",
                          {"offer": public(row)})
        check_cap(store, candidate_id)
        if review.cautious(store):
            waiting = queue(store, "terms", candidate_id, job_id, {"fields": changed}, session)
            return _plain(_wait_text(waiting) + _read_days(days), {
                "pending_review": _waiting(waiting), "offer": public(row) if row else None,
                **({"read_as": days} if days else {}), **(stage_nudge(store, candidate_id, job_id) or {})})
        _check_cap(store, overwrite_count("offer_terms", snap(row) or {}, {**(snap(row) or {}), **changed}))
        res = save(store, candidate_id, job_id, by="ai", **changed)
        o = res["offer"]
        what = ", ".join(LABELS[k] for k in res["changed"])
        sentence = (f"{'Recorded the offer' if res['created'] else 'Changed the offer'} for {quoted(name)} on {job}"
                    + (f" ({what})" if what else "") + f". {status_line(o)}.{_read_days(days)} "
                    f"{who(store, cap=True)} can undo it on Changes.")
        return _plain(sentence, {"offer": public(o), "note": "Keep the terms out of status updates and client "
                                                             f"messages; they are for {who(store)} and the candidate.",
                                 **({"read_as": days} if days else {}),
                                 **(stage_nudge(store, candidate_id, job_id) or {})})

    @mcp.tool(annotations=EDIT)
    def record_offer_answer(candidate_id: int, job_id: int,
                            answer: Annotated[str, WithJsonSchema(
                                {"type": "string", "enum": list(ANSWERS)})],
                            reason: str | None = None) -> CallToolResult:
        """Record the answer to a person's offer: accepted, declined (the candidate said no; give the reason, like
        "Took another offer") or withdrawn (the company pulled it). The app then offers the recruiter the move to
        the job's hired stage, or to an out stage with the reason; move_stage also makes it. The recruiter can
        undo it on Changes."""
        st = _status(answer, ANSWERS)
        why = clean({"reason": reason})["reason"]
        row = get(store, candidate_id, job_id)
        pair = _pair(store, candidate_id, job_id) if row is None else None
        name = (pair["candidate"] if pair else store.get_candidate(candidate_id))["name"]
        job = _job_name(pair["job"] if pair else store.get_job(job_id))
        if row is not None and row["status"] == st and (row.get("reason") or None) == why:
            return _plain(f"Nothing changed: the offer for {quoted(name)} on {job} is already {st}.",
                          {"offer": public(row)})
        check_cap(store, candidate_id)
        if review.cautious(store):
            waiting = queue(store, "answer", candidate_id, job_id, {"answer": st, "reason": why}, session)
            return _plain(_wait_text(waiting), {"pending_review": _waiting(waiting),
                                                "offer": public(row) if row else None})
        _check_cap(store, 1)
        res = record_answer(store, candidate_id, job_id, st, why, by="ai")
        nxt = res["next"]
        said = (f"the offer to {quoted(name)} on {job} was withdrawn" if st == "withdrawn"
                else f"{quoted(name)} {st} the offer on {job}")
        tail = (f" The app offers {who(store)} the move to {nxt['to']} in one click; move_stage also makes it."
                if nxt else "")
        data = {"offer": public(res["offer"])}
        if nxt:
            data["next_move"] = {"stage": nxt["to"], "notes": nxt["notes"]}
        return _plain(f"Recorded that {said}.{tail} {who(store, cap=True)} can undo it on Changes.", data)

    @mcp.tool(annotations=READ)
    def list_offers(job_id: int | None = None) -> CallToolResult:
        """Every offer (or one job's): where it stands ("Offer out 3 days, answer due Fri"), its terms and the
        answer. Offer terms are private: never put them in a status update or a client message."""
        if job_id is not None:
            store.get_job(job_id)
        rows = listed(store, job_id)
        if not rows:
            return _plain("No offers recorded yet." if job_id is None else "No offers recorded on that job yet.",
                          {"offers": []})
        lines = [f"{len(rows)} offer{'' if len(rows) == 1 else 's'} (terms are private, for the recruiter only):"]
        for o in rows:
            terms = "; ".join(f"{LABELS[k]} {o[k]}" for k in ("salary", "equity", "bonus") if o.get(k))
            lines.append(f"- {quoted(o['candidate_name'])} (candidate id {o['candidate_id']}) on {_job_name(o)} "
                         f"(job id {o['job_id']}): {status_line(o)}" + (f". {terms}" if terms else ""))
        return _plain("\n".join(lines), {"offers": [public(o) for o in rows], "data_note": DATA_NOTE})


# ------------------------------------------------------------------ web
def register_routes(app: Any, store: Store, page: Any, back: Any) -> None:
    """The offer page: GET and POST /jobs/{job_id}/offers/{candidate_id}, and its answer and move forms."""
    def pair_or_404(job_id: int, candidate_id: int) -> dict[str, Any]:
        try:
            c, j = store.get_candidate(candidate_id), store.get_job(job_id)
        except NotFound:
            raise HTTPException(404, "Not found") from None
        app_row = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?",
                             (candidate_id, job_id))
        return {"c": c, "job": j, "stage": app_row["stage"] if app_row else None}

    def to_page(job_id: int, candidate_id: int, notice: str = "", error: str = "") -> RedirectResponse:
        q = urlencode([(k, v) for k, v in (("notice", notice), ("flash_error", error)) if v])
        return RedirectResponse(href(candidate_id, job_id) + (f"?{q}" if q else ""), status_code=303)

    @app.get("/jobs/{job_id}/offers/{candidate_id}", response_class=HTMLResponse)
    def offer_page(request: Request, job_id: int, candidate_id: int):
        p = pair_or_404(job_id, candidate_id)
        o = get(store, candidate_id, job_id)
        if o is None and p["stage"] is None:
            raise HTTPException(404, "They aren't in this job")
        from . import review
        waiting = [r for r in review.pending(store, candidate_id=candidate_id, job_id=job_id) if r["kind"] == "offer"]
        return page(request, "offer.html", o=o, line=status_line(o) if o else None, late=bool(o and overdue(o)),
                    nxt=next_move(store, o) if o else None, summary=summary_text(store, o) if o else "",
                    waiting=waiting, statuses=STATUS_WORDS, **p)

    @app.post("/jobs/{job_id}/offers/{candidate_id}")
    async def offer_save(request: Request, job_id: int, candidate_id: int):
        pair_or_404(job_id, candidate_id)
        form = await request.form()
        given = {k: str(form.get(k) or "") for k in TERMS}
        if str(form.get("status") or "").strip():
            given["status"] = str(form.get("status"))
        given["share_status"] = "1" if form.get("share_status") else ""
        try:
            res = save(store, candidate_id, job_id, by="app", **given)
        except (ValueError, NotFound) as exc:
            return to_page(job_id, candidate_id, error=str(exc))
        notice = ("Saved the offer." if res["created"] else "Saved the changes." if res["changed"]
                  else "Nothing changed.")
        return to_page(job_id, candidate_id, notice=notice)

    @app.post("/jobs/{job_id}/offers/{candidate_id}/answer")
    async def offer_answer(request: Request, job_id: int, candidate_id: int):
        p = pair_or_404(job_id, candidate_id)
        form = await request.form()
        why = " ".join(x for x in (str(form.get("reason") or "").strip(), str(form.get("why") or "").strip()) if x)
        try:
            res = record_answer(store, candidate_id, job_id, str(form.get("answer") or ""), why or None, by="app")
        except (ValueError, NotFound) as exc:
            return to_page(job_id, candidate_id, error=str(exc))
        word = {"accepted": "accepted", "declined": "declined", "withdrawn": "withdrawn"}[res["offer"]["status"]]
        nxt = res["next"]
        notice = f"Recorded: offer {word}." + (f" Move {p['c']['name']} to {nxt['to']} below, in one click." if nxt else "")
        return to_page(job_id, candidate_id, notice=notice)

    @app.post("/jobs/{job_id}/offers/{candidate_id}/move")
    async def offer_move(request: Request, job_id: int, candidate_id: int):
        pair_or_404(job_id, candidate_id)
        form = await request.form()
        try:
            row = move(store, candidate_id, job_id, str(form.get("to") or ""))
        except (ValueError, NotFound) as exc:
            if form.get("next") == "/":
                return back(request, "/", error=str(exc))
            return to_page(job_id, candidate_id, error=str(exc))
        notice = f"Moved {row['candidate_name']} to {row['stage']}. You can undo it on their timeline."
        if form.get("next") == "/":
            return back(request, "/", notice=notice)
        return to_page(job_id, candidate_id, notice=notice)


__all__ = ["ANSWERS", "STATUSES", "STATUS_WORDS", "TERMS", "ai_blocker", "ai_summary", "ai_undo", "apply_review",
           "check_cap", "clean", "client_status", "get", "listed", "move", "next_move", "note", "of_candidate",
           "offer_like", "on_job", "overwrite_count", "queue", "record_answer", "recruiter_lines", "register_routes",
           "register_tools", "review_lines", "save", "shared_lines", "snap", "stage_nudge", "status_line",
           "summary_text", "today_items"]
