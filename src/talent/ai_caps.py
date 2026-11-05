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


def people_left(store: Store) -> int:
    return max(0, people_cap(store) - people_this_hour(store))


def passes_left(store: Store) -> int:
    return max(0, PASS_CAP - passes_this_hour(store))


def people_text(left: int, cap: int, what: str = "saved") -> str:
    return (f"Nothing {what}. Your AI app can add {cap} new people an hour and has {left} left now. Add the rest "
            "in Talent Search, or try again later.")


def allowance(store: Store) -> dict[str, Any]:
    """What the AI app may still do this hour, for the Changes page."""
    over = store.ai_overwrites_this_hour()["count"]
    return {"people_left": people_left(store), "people_cap": people_cap(store),
            "passes_left": passes_left(store), "passes_cap": PASS_CAP,
            "presses_left": max(0, PRESS_CAP - presses_this_hour(store)), "presses_cap": PRESS_CAP,
            "overwrites_left": max(0, AI_OVERWRITE_CAP - over), "overwrites_cap": AI_OVERWRITE_CAP}
