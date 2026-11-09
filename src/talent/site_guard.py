"""Stricter site guards: an optional setting that tells the AI app to stay off sites whose terms forbid automated
collection, and limits how many new people it saves or proposes from them each hour.

Talent Search never fetches a web page itself, so this only guides and limits the AI app through this app's own
tools and text. It can't control what the AI app does elsewhere. Recruiter actions in the app (paste, CSV import,
the form) are never limited by it. Nothing here makes a network call."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

SETTING = "strict_site_guards"  # "1" when on; off by default
HITS_KEY = "ai_guarded_hits"  # settings row: when the AI app last saved or proposed people from a listed site
CAP = 10  # new people from a listed site the AI app may save or propose in an hour
WINDOW_MINUTES = 60

# Sites whose terms forbid automated collection: (domain, name as people write it).
GUARDED_SITES: tuple[tuple[str, str], ...] = (
    ("linkedin.com", "LinkedIn"),
    ("indeed.com", "Indeed"),
    ("glassdoor.com", "Glassdoor"),
    ("facebook.com", "Facebook"),
    ("instagram.com", "Instagram"),
)
SITE_DOMAINS = tuple(d for d, _ in GUARDED_SITES)

_HOST = re.compile(r"(?<![@\w.-])((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w-])", re.I)


def enabled(store: Any) -> bool:
    return store.setting(SETTING) == "1"


def site_list() -> str:
    return ", ".join(SITE_DOMAINS)


def _domain_of(host: str) -> str | None:
    host = host.lower().strip(".")
    return next((d for d in SITE_DOMAINS if host == d or host.endswith("." + d)), None)


def listed_site(*values: Any, source: Any = None) -> str | None:
    """The listed domain that a link in `values` points at, or that `source` names (a link, or the site's name
    such as "LinkedIn"). None when it is neither."""
    for v in (*values, source):
        for m in _HOST.finditer(str(v or "")):
            if (d := _domain_of(m.group(1))):
                return d
    text = str(source or "")
    for d, name in GUARDED_SITES:
        if re.search(r"(?<![\w@.])" + re.escape(name) + r"(?!\w)", text, re.I):
            return d
    return None


def instruction_line() -> str:
    """For the AI app's instructions and prompts, only while the switch is on."""
    return (f"The recruiter turned on Stricter site guards in Talent Search. Don't browse, crawl or bulk-collect from "
            f"{site_list()}. Use pages the recruiter opens and shares with you, their own exports, or other "
            f"sources. Saving or proposing more than {CAP} new people an hour whose source or profile link is on "
            "one of those sites is limited.")


def result_note(domain: str) -> str:
    """Added to a save or propose reply while the switch is on and the person's source or link is a listed site."""
    return (f"The recruiter turned on Stricter site guards, and this person's source or link is on {domain}. "
            f"Don't browse or collect from {site_list()}; use pages the recruiter shares with you, their own "
            "exports, or other sources.")


def cap_text(left_now: int) -> str:
    return (f"Nothing saved. The recruiter turned on Stricter site guards in Talent Search, which lets you save or "
            f"propose {CAP} new people an hour whose source or profile link is on {site_list()}, and {left_now} "
            "are left now. Use other sources, pages the recruiter shares with you, or ask the recruiter to add the "
            "rest in Talent Search or turn the switch off in Settings.")


def _recent(store: Any) -> list[str]:
    row = store._row("SELECT value FROM settings WHERE key = ?", (HITS_KEY,))
    try:
        hits = json.loads(row["value"]) if row else []
    except (ValueError, TypeError):
        hits = []
    since = (datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)).isoformat()
    return [h for h in hits if isinstance(h, str) and h >= since]


def left(store: Any) -> int:
    return max(0, CAP - len(_recent(store)))


def record(store: Any, n: int = 1) -> None:
    """Count n new people from a listed site, saved or proposed by the AI app."""
    if n < 1:
        return
    with store._write():
        hits = _recent(store) + [datetime.now(timezone.utc).isoformat()] * n
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                            "value = excluded.value", (HITS_KEY, json.dumps(hits)))
