"""Private tags: the recruiter's own working labels ("Recruiter: Priya", "sourced-nov", "dnc") kept apart from skills.

They live in candidates.private_tags (see store.py) and never leave this computer: not in shares, spreadsheets,
client write-ups or reports, and not in anything an AI app is sent. This module is the Settings page's side of it:
which skill tags look like labels (a suggestion only), and the pick lists."""
from __future__ import annotations

import re
from typing import Any

from .store import Store

PAGE_SIZE = 200
SUGGEST_LIMIT = 100

_WORDS = re.compile(
    r"\b(recruiter|sourced|source|referral|referred|dnc|do not|owner|priority|hot|cold|follow[- ]?up|to call)\b", re.I)
_MONTH = re.compile(
    r"(?:^|[-_ /])(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*(?:$|[-_ /]|\d)", re.I)


def looks_private(tag: str) -> bool:
    """Whether a skill tag reads like a label of the recruiter's own (a guess; they always choose)."""
    t = tag.strip()
    if ":" in t or "@" in t:
        return True
    if _WORDS.search(t):
        return True
    return "-" in t and bool(_MONTH.search(t))


def settings_view(store: Store, q: str = "", page: int = 1) -> dict[str, Any]:
    """What the Settings page lists: suggested tags (look private, most used first), every other skill tag (searchable,
    paginated past PAGE_SIZE) and the tags that are private now."""
    tags = store.list_tags(1_000_000)
    suggested = [t for t in tags if looks_private(t["tag"])]
    rest = [t for t in tags if not looks_private(t["tag"])]
    q = " ".join((q or "").split())
    if q:
        rest = [t for t in rest if q.casefold() in t["tag"].casefold()]
    rest.sort(key=lambda t: t["tag"].casefold())
    pages = max(1, -(-len(rest) // PAGE_SIZE))
    page = min(max(1, page), pages)
    return {
        "suggested": suggested[:SUGGEST_LIMIT], "suggested_more": max(0, len(suggested) - SUGGEST_LIMIT),
        "others": rest[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], "others_total": len(rest), "q": q,
        "page": page, "pages": pages, "mine": store.list_private_tags(),
    }


PRIVATE_WORDS = "a private tag"


def redact_filter(store: Store, label: str, value: str) -> str:
    """The text of a filter line for a file that may be sent on: a private tag name is never printed, so the
    filter's value becomes "a private tag" when it is one (the Skill filter, or any piece of the Skills filter)."""
    if label not in ("Skill", "Skills"):
        return value
    names = {n.casefold() for n in store.private_tag_names()}
    if not names:
        return value
    if label == "Skill":
        group = {n.casefold() for n in store.skill_group_names(value)} | {value.casefold()}
        return PRIVATE_WORDS if group & names else value
    parts = re.split(r"( and | or |,)", value)
    return "".join(PRIVATE_WORDS if p.strip().casefold() in names else p for p in parts)


def search_hint(store: Store, q: str, total: int) -> str:
    """When a plain search finds nobody and its words match the name of a private tag in use, that tag's name (the
    page then points at the tag filter, which is where private tags are found). Local window only."""
    words = " ".join((q or "").split()).casefold()
    if total or not words:
        return ""
    for t in store.list_private_tags():
        name = t["tag"].casefold()
        if t["count"] and (words in name or name in words):
            return t["tag"]
    return ""
