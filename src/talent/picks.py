"""Highlight picks: a pick is a skill ("python") or a job title ("title:data engineer"), and skill names that a person
has chosen to treat as one (aliases, see Store.skill_alias_map). Pure functions, no database.

How a pick matches a person:
- a skill pick matches when the person has that skill, or any name in its alias group (case never matters);
- a title pick matches the person's CURRENT title only (never their work history), after folding seniority and level
  words and punctuation, so "Sr. Data Engineer" and "Data Engineer II" both match a "Data Engineer" pick.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

TITLE_PREFIX = "title:"
LEVEL_WORDS = frozenset({"senior", "sr", "jr", "junior", "lead", "staff", "principal", "i", "ii", "iii", "iv", "1", "2", "3"})
_ACRONYMS = frozenset({"ai", "ml", "qa", "sre", "ui", "ux", "hr", "it", "vp", "cto", "ceo", "cfo", "coo", "sdet", "bi",
                       "seo", "sql", "api", "ios", "devops", "nlp", "etl", "gtm", "pm", "swe", "sde"})
_SPECIAL = {"devops": "DevOps", "ios": "iOS"}


def fold_title(text: Any) -> str:
    """'Sr. Data Engineer II' -> 'data engineer'. Lower case, punctuation gone, level words dropped."""
    low = re.sub(r"[^\w\s]|_", " ", str(text or "").lower())
    words = [w for w in low.split() if w not in LEVEL_WORDS]
    return " ".join(words)


def is_title(pick: str) -> bool:
    return str(pick).startswith(TITLE_PREFIX)


def title_key(pick: str) -> str:
    """The folded title a title pick stands for ('' for a skill pick)."""
    return fold_title(str(pick)[len(TITLE_PREFIX):]) if is_title(pick) else ""


def make_title_pick(title: Any) -> str:
    """The pick for a person's title, or '' when nothing is left once level words are folded."""
    key = fold_title(title)
    return TITLE_PREFIX + key[:60] if key else ""


def normalize_pick(name: str) -> str:
    """What parse_hl keeps for one pick: skills lower case; titles folded, with the 'title:' prefix."""
    name = " ".join(str(name).split())[:70].lower()
    if is_title(name):
        key = fold_title(name[len(TITLE_PREFIX):]) or " ".join(name[len(TITLE_PREFIX):].split())
        return (TITLE_PREFIX + key[:60]) if key else ""
    return name[:60]


def display_words(key: str) -> str:
    out = []
    for w in key.split():
        out.append(_SPECIAL.get(w) or (w.upper() if w in _ACRONYMS else w[:1].upper() + w[1:]))
    return " ".join(out)


def label(pick: str) -> str:
    """'title:data engineer' -> 'title: Data Engineer'; a skill is shown as it is."""
    return "title: " + display_words(title_key(pick) or str(pick)[len(TITLE_PREFIX):].strip()) if is_title(pick) else str(pick)


def canon(name: str, aliases: Mapping[str, str] | None) -> str:
    low = " ".join(str(name).split()).lower()
    return (aliases or {}).get(low, low)


def canon_pairs(pairs: Iterable[tuple[str, str]], aliases: Mapping[str, str] | None) -> list[tuple[str, str]]:
    """Picks with skill names swapped for the name their group keeps; two picks that are now one keep the first."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, color in pairs:
        key = name if is_title(name) else canon(name, aliases)
        if key not in seen:
            seen.add(key)
            out.append((key, color))
    return out


def person_hits(tags: Iterable[str], title: Any, picks: Iterable[str], aliases: Mapping[str, str] | None = None) -> list[bool]:
    """For each pick (in order): does this person match it. Picks should already be canonical (canon_pairs)."""
    have = {canon(t, aliases) for t in (tags or [])}
    folded = fold_title(title)
    out = []
    for p in picks:
        if is_title(p):
            k = title_key(p)
            out.append(bool(k) and folded == k)
        else:
            out.append(canon(p, aliases) in have)
    return out


def person_matches(c: Mapping[str, Any], picks: Iterable[str], aliases: Mapping[str, str] | None = None) -> list[str]:
    """The picks (in pick order) this person row matches."""
    ps = list(picks)
    return [p for p, hit in zip(ps, person_hits(c.get("tags") or [], c.get("current_title") or c.get("title"), ps, aliases)) if hit]


# ------------------------------------------------------------------ look-alike skill names
_SHORT = [
    ("js", "javascript"), ("ts", "typescript"), ("k8s", "kubernetes"), ("postgres", "postgresql"),
    ("golang", "go"), ("ml", "machine learning"), ("node", "node.js"), ("node", "nodejs"), ("node.js", "nodejs"),
]


def _squash(name: str) -> str:
    s = re.sub(r"[\s.\-_]+", "", name.lower())
    if s.endswith("js") and len(s) > 3:
        s = s[:-2]
    return s


def _has_symbol(name: str) -> bool:
    """c#, c++, f#, .net: symbols that are part of the name, so never folded together."""
    return bool(re.search(r"[#+]", name))


def _singular(s: str) -> str:
    if len(s) > 4 and s.endswith("ies"):
        return s[:-3] + "y"
    if len(s) > 3 and s.endswith("s") and not s.endswith("ss"):
        return s[:-1]
    return s


def look_alike(a: str, b: str) -> str:
    """Why two skill names look like one ('' when they do not). Never for names whose symbols differ (c# / c++)."""
    x, y = " ".join(a.lower().split()), " ".join(b.lower().split())
    if x == y or not x or not y:
        return ""
    if _has_symbol(x) or _has_symbol(y):
        return ""
    for p, q in _SHORT:
        if {x, y} == {p, q}:
            return "short form"
    sx, sy = _squash(x), _squash(y)
    if sx and sx == sy:
        return "spelling"
    px, py = _singular(sx), _singular(sy)
    if px == py and len(px) > 2:
        return "plural"
    for short, long_ in ((sx, sy), (sy, sx)):  # engineer / engineering, manage / managing
        if len(short) > 3 and (long_ == short + "ing" or (short.endswith("e") and long_ == short[:-1] + "ing")):
            return "-ing"
    return ""
