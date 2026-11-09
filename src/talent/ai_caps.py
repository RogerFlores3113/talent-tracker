"""Hourly limits on how many people the AI app can add or pass on, and what is left of its allowance.

Counted from the AI action log (store._log_ai), across every AI session, so a noisy run can't hide behind
several small calls. Undone ones still count. Nothing here is an MCP tool."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import AI_OVERWRITE_CAP, Store

PEOPLE_CAP = 40  # new people the AI app may save in an hour (save_candidate(s), and Keep on the Shortlist card)
PASS_CAP = 100  # people it may pass on in an hour (Pass on the Shortlist card)
PRESS_CAP = 30  # Keep and Pass presses on the Shortlist card in an hour, whatever their size
PROPOSE_CAP = 50  # people the AI app may put on Shortlists in an hour
JOB_CAP = 10  # jobs it may create in an hour
CLIENT_CAP = 10  # clients it may create in an hour
SEARCH_CAP = 20  # saved searches it may save in an hour
WINDOW_MINUTES = 60
PEOPLE_KEY = "ai_people_cap"  # optional settings row that replaces PEOPLE_CAP


def _since() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)).replace(microsecond=0).isoformat()


def people_cap(store: Store) -> int:
    row = store._row("SELECT value FROM settings WHERE key = ?", (PEOPLE_KEY,))
    return int(row["value"]) if row and str(row["value"]).isdigit() else PEOPLE_CAP


def _count(new_data: str | None) -> int:
    try:
        return max(1, int((json.loads(new_data or "{}") or {}).get("count") or 1))
    except (ValueError, TypeError, AttributeError):
        return 1


def people_this_hour(store: Store) -> int:
    """New people in the last hour: each one saved by the AI app, and each one a Keep through it put on a job."""
    n = 0
    for r in store._rows("SELECT kind, new_data FROM ai_actions WHERE at >= ? AND kind IN ('candidate', 'card_keep')",
                         (_since(),)):
        n += 1 if r["kind"] == "candidate" else _count(r["new_data"])
    return n


def passes_this_hour(store: Store) -> int:
    return store._row("SELECT count(*) AS n FROM ai_actions WHERE at >= ? AND kind = 'card_pass'", (_since(),))["n"]


def presses_this_hour(store: Store) -> int:
    return store._row("SELECT count(*) AS n FROM ai_actions WHERE at >= ? AND kind IN ('card_keep', 'card_pass')",
                      (_since(),))["n"]


def proposed_this_hour(store: Store) -> int:
    n = 0
    for r in store._rows("SELECT new_data FROM ai_actions WHERE at >= ? AND kind = 'prospects_proposed'", (_since(),)):
        try:
            n += len((json.loads(r["new_data"] or "{}") or {}).get("prospect_ids") or [])
        except (ValueError, TypeError, AttributeError):
            n += 1
    return n


def _kind_this_hour(store: Store, kind: str) -> int:
    return store._row("SELECT count(*) AS n FROM ai_actions WHERE at >= ? AND kind = ?", (_since(), kind))["n"]


def proposed_left(store: Store) -> int:
    return max(0, PROPOSE_CAP - proposed_this_hour(store))


def jobs_left(store: Store) -> int:
    return max(0, JOB_CAP - _kind_this_hour(store, "job_created"))


def searches_left(store: Store) -> int:
    return max(0, SEARCH_CAP - _kind_this_hour(store, "saved_search"))


def clients_left(store: Store) -> int:
    return max(0, CLIENT_CAP - _kind_this_hour(store, "client_create"))


def frees_at(store: Store, kinds: tuple[str, ...]) -> str:
    """When the next slot of an hourly limit opens: the oldest action of these kinds in the window plus an hour, as
    "3:42 PM PST" on the recruiter's clock. "" when there is none."""
    from .store import as_local, to_dt
    marks = ", ".join("?" * len(kinds))
    row = store._row(f"SELECT min(at) AS at FROM ai_actions WHERE at >= ? AND kind IN ({marks})", (_since(), *kinds))
    if not row or not row["at"]:
        return ""
    dt = as_local(to_dt(row["at"]) + timedelta(minutes=WINDOW_MINUTES))
    return f"{dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'} {dt.tzname() or ''}".strip()


def _again_text(store: Store | None, kinds: tuple[str, ...]) -> str:
    when = frees_at(store, kinds) if store is not None and kinds else ""
    return f"the next slot frees up at {when}" if when else "try again later"


def limit_text(what: str, left: int, store: Store | None = None, kinds: tuple[str, ...] = ()) -> str:
    """what: "propose 50 people for Shortlists", "create 10 jobs"."""
    return (f"Nothing added. Your AI app can {what} an hour and has {left} left now. Add the rest in Talent "
            f"Search, or {_again_text(store, kinds)}.")


def people_left(store: Store) -> int:
    return max(0, people_cap(store) - people_this_hour(store))


def passes_left(store: Store) -> int:
    return max(0, PASS_CAP - passes_this_hour(store))


def people_text(left: int, cap: int, what: str = "saved", store: Store | None = None) -> str:
    return (f"Nothing {what}. Your AI app can add {cap} new people an hour and has {left} left now. Add the rest "
            f"in Talent Search, or {_again_text(store, ('candidate', 'card_keep'))}.")


def allowance(store: Store) -> dict[str, Any]:
    """What the AI app may still do this hour, for the Changes page."""
    over = store.ai_overwrites_this_hour()["count"]
    return {"people_left": people_left(store), "people_cap": people_cap(store),
            "passes_left": passes_left(store), "passes_cap": PASS_CAP,
            "presses_left": max(0, PRESS_CAP - presses_this_hour(store)), "presses_cap": PRESS_CAP,
            "proposed_left": proposed_left(store), "proposed_cap": PROPOSE_CAP,
            "jobs_left": jobs_left(store), "jobs_cap": JOB_CAP,
            "clients_left": clients_left(store), "clients_cap": CLIENT_CAP,
            "searches_left": searches_left(store), "searches_cap": SEARCH_CAP,
            "overwrites_left": max(0, AI_OVERWRITE_CAP - over), "overwrites_cap": AI_OVERWRITE_CAP}
