"""The search box's small query language, and the helpers the filters and saved searches share.

Nothing here touches the database, and nothing here builds SQL or FTS5 text from what was typed. `parse_query`
turns the typed words into plain pieces (words, phrases, OR choices, exclusions, field filters); the store then
builds its own FTS5 expression from those pieces, quoting every one, so no typed character can become syntax.

What the box understands (AND stays the default):
    machine learning          every word must match
    "machine learning"        that exact phrase
    python OR go              either word (OR in capitals)
    -recruiter, NOT manager   leave out people who have that word
    title:engineer            the same as the Title filter (also company:, location:, skill: or tag:, source:)
Parentheses are not supported: they are ignored, so `(python OR go)` reads as `python OR go`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

# field prefix -> the filter it sets
FIELD_ALIASES = {"title": "title", "company": "company", "location": "location", "skill": "skill", "skills": "skill",
                 "tag": "skill", "tags": "skill", "source": "source"}
_FIELD_AT = re.compile(r"([A-Za-z]+):")
_WORD = re.compile(r"\w+", re.UNICODE)
MAX_TOKENS = 40  # a pasted paragraph is not a query; the rest is ignored


@dataclass
class Item:
    text: str  # words joined by one space, lowercase
    phrase: bool = False  # two or more words that must sit together
    raw: str = ""  # the same words as typed (capitals kept: "Dana Okafor" reads as a name)

    @classmethod
    def of(cls, raw: str, phrase: bool = False) -> "Item | None":
        typed = _WORD.findall(raw or "")
        words = [w.lower() for w in typed]
        if not words:
            return None
        return cls(" ".join(words), phrase and len(words) > 1, " ".join(typed))


@dataclass
class ParsedQuery:
    groups: list[list[Item]] = field(default_factory=list)  # AND of OR-groups
    exclude: list[Item] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)  # title, company, location, source, skill
    skill_any: bool = False  # skill:a OR skill:b
    used_syntax: bool = False  # quotes, OR, NOT, a minus or a field prefix was understood

    @property
    def operator_mode(self) -> bool:
        """A phrase or an OR needs the operator search; plain words keep the everyday search (typo help, notes)."""
        return any(i.phrase for g in self.groups for i in g) or any(len(g) > 1 for g in self.groups)

    @property
    def bare(self) -> str:
        """The plain words, for the everyday search. Empty in operator mode."""
        if self.operator_mode:
            return ""
        return " ".join(g[0].raw or g[0].text for g in self.groups)

    @property
    def structured(self) -> bool:
        return self.used_syntax


def parse_query(text: str | None) -> ParsedQuery:
    """Typed text -> pieces. Never raises: an unbalanced quote, a lone OR or NOT, a dash or `title:` alone are
    dropped or read as ordinary words."""
    s = (text or "")[:2000]
    pq = ParsedQuery()
    toks: list[tuple[str, Any, bool, str | None]] = []  # (kind, value, negated, field)
    i, n = 0, len(s)
    while i < n and len(toks) < MAX_TOKENS:
        ch = s[i]
        if ch.isspace() or ch in "()":
            i += 1
            continue
        neg = False
        if ch == "-" and i + 1 < n and not s[i + 1].isspace() and s[i + 1] not in "()-":
            neg, i = True, i + 1
            pq.used_syntax = True
        fld = None
        m = _FIELD_AT.match(s, i)
        if m and m.group(1).lower() in FIELD_ALIASES:
            pq.used_syntax = True
            if m.end() >= n or s[m.end()].isspace():  # "title:" with nothing after it: nothing to filter on
                i = m.end()
                continue
            fld, i = FIELD_ALIASES[m.group(1).lower()], m.end()
        if i < n and s[i] == '"':
            j = s.find('"', i + 1)
            if j == -1:  # unbalanced: the quote is dropped and the rest reads as plain words
                for w in _WORD.findall(s[i + 1:]):
                    toks.append(("word", w, neg, fld))
                    neg = False
                break
            toks.append(("phrase", s[i + 1:j], neg, fld))
            pq.used_syntax = True
            i = j + 1
            continue
        j = i
        while j < n and not s[j].isspace() and s[j] not in '"()':
            j += 1
        raw = s[i:j]
        i = max(j, i + 1)
        if not raw:
            continue
        if fld is None and not neg and raw in ("OR", "AND", "NOT"):
            toks.append(("op", raw, False, None))
            pq.used_syntax = pq.used_syntax or raw != "AND"
            continue
        toks.append(("word", raw, neg, fld))

    pending_or = pending_not = False
    last_field_value: dict[str, bool] = {}
    for kind, value, neg, fld in toks:
        if kind == "op":
            if value == "OR":
                pending_or = True
            elif value == "NOT":
                pending_not = True
            continue
        item = Item.of(value, phrase=(kind == "phrase"))
        neg = neg or pending_not
        pending_not = False
        if item is None:
            continue
        if neg:  # a minus or NOT leaves people out; an OR before it means nothing
            pq.exclude.append(item)
            pending_or = False
            continue
        if fld:
            vals = pq.fields.setdefault(fld, [])
            if fld == "skill":
                if pending_or and last_field_value.get(fld):
                    pq.skill_any = True
                vals.append(item.text)
                last_field_value[fld] = True
            else:
                pq.fields[fld] = [item.text]  # the last one given wins
            pending_or = False
            continue
        if pending_or and pq.groups:
            pq.groups[-1].append(item)
        else:
            pq.groups.append([item])
        pending_or = False
    return pq


def rewrite_suggestion(original: str, bare: str, suggestion: str) -> str:
    """The typed query with the words a spelling fix changed swapped in, so operators and filters stay."""
    old, new = bare.split(), suggestion.split()
    if len(old) != len(new):
        return suggestion
    out = original
    for a, b in zip(old, new):
        if a != b:
            out = re.sub(rf"(?<!\w){re.escape(a)}(?!\w)", b, out, flags=re.I)
    return out


# ------------------------------------------------------------------ years of experience
_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-6]\d)(?!\d)")
_NOW_WORDS = ("present", "now", "current", "today", "ongoing", "to date", "till date")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _month_index(text: str | None, *, end: bool, today: date) -> int | None:
    """'Mar 2019', '2019-03', '03/2019', '2019' -> months since year 0 (a bare year is January for a start and
    December for an end). 'present', 'now' and an empty end -> this month. None when it can't be read."""
    t = (text or "").strip().lower()
    if not t:
        return today.year * 12 + today.month - 1 if end else None
    if any(w in t for w in _NOW_WORDS):
        return today.year * 12 + today.month - 1
    m = _YEAR.search(t)
    if not m:
        return None
    year = int(m.group(1))
    rest = t[:m.start()] + " " + t[m.end():]
    month = None
    word = re.search(r"[a-z]{3,}", rest)
    if word and word.group(0)[:3] in _MONTHS:
        month = _MONTHS.index(word.group(0)[:3]) + 1
    else:
        num = re.search(r"(?<!\d)(0?[1-9]|1[0-2])(?!\d)", rest)
        if num:
            month = int(num.group(1))
    if month is None:
        month = 12 if end else 1
    return year * 12 + month - 1


def years_of_experience(roles: list[tuple[str | None, str | None]], today: date | None = None) -> float | None:
    """Years worked across the roles (start, end): overlapping roles count once, an open end runs to today, a
    role whose dates can't be read is skipped, and months count in full (Jan 2020 to Dec 2020 is 12 months).
    None when no role has readable dates."""
    today = today or date.today()
    now_idx = today.year * 12 + today.month - 1
    spans = []
    for start, end in roles:
        a = _month_index(start, end=False, today=today)
        b = _month_index(end, end=True, today=today)
        if a is None or b is None:
            continue
        if a > now_idx or b < a:  # not started yet, or an end before the start
            continue
        b = min(b, now_idx)
        spans.append((a, b))
    if not spans:
        return None
    spans.sort()
    months, cur_a, cur_b = 0, *spans[0]
    for a, b in spans[1:]:
        if a <= cur_b + 1:  # touching or overlapping: one stretch
            cur_b = max(cur_b, b)
        else:
            months += cur_b - cur_a + 1
            cur_a, cur_b = a, b
    months += cur_b - cur_a + 1
    return months / 12


# ------------------------------------------------------------------ form and saved-search parameters
# The names the Candidates form, saved searches and the AI's tools share. Existing names are unchanged.
PARAM_KEYS = ("q", "tag", "location", "source", "job", "client", "stage", "title", "company", "skills", "skill_mode",
              "remote", "exp_min", "exp_max", "contacted", "contacted_days", "added_days", "has_email", "has_linkedin",
              "color")
CONTACTED_CHOICES = ("never", "older", "within")
MORE_KEYS = ("title", "company", "skills", "remote", "exp_min", "exp_max", "contacted", "added_days", "has_email",
             "has_linkedin")


def _num(value: Any, high: int = 100000) -> int | None:
    s = str(value if value is not None else "").strip()
    if not re.fullmatch(r"\d{1,6}", s):
        return None
    return min(int(s), high)


def split_skills(value: Any) -> list[str]:
    """'python, Go; rust' -> ['python', 'Go', 'rust'] (no repeats, at most 12)."""
    out: list[str] = []
    for part in re.split(r"[,;\n]+", str(value or "")):
        p = " ".join(part.split())
        if p and p.lower() not in {x.lower() for x in out}:
            out.append(p[:60])
    return out[:12]


def clean_params(raw: Mapping[str, Any] | None) -> dict[str, str]:
    """Keep only known keys with a usable value, as short strings. What saved searches store."""
    out: dict[str, str] = {}
    for k in PARAM_KEYS:
        v = (raw or {}).get(k)
        if v is None:
            continue
        s = " ".join(str(v).split())[:300]
        if not s:
            continue
        if k in ("exp_min", "exp_max", "contacted_days", "added_days", "job"):
            num = _num(s)
            if num is None:
                continue
            s = str(num)
        elif k in ("remote", "has_email", "has_linkedin"):
            if s.lower() not in ("1", "true", "yes", "on"):
                continue
            s = "1"
        elif k == "contacted":
            if s.lower() not in CONTACTED_CHOICES:
                continue
            s = s.lower()
        elif k == "skills":
            s = ", ".join(split_skills(s))
            if not s:
                continue
        elif k == "skill_mode":
            if s.lower() != "all":
                continue  # "any" is the default
            s = "all"
        out[k] = s
    if out.get("contacted") in ("older", "within") and "contacted_days" not in out:
        out.pop("contacted")  # "more than N days" with no N means nothing
    if out.get("contacted") not in ("older", "within"):
        out.pop("contacted_days", None)
    return out


def search_kwargs(params: Mapping[str, Any]) -> dict[str, Any]:
    """Cleaned form/saved-search params -> keyword arguments for Store.search_candidates (q excluded)."""
    p = clean_params(params)
    kw: dict[str, Any] = {}
    for k in ("tag", "location", "source", "client", "stage", "title", "company"):
        if k in p:
            kw[k] = p[k]
    if "job" in p:
        kw["job_id"] = int(p["job"])
    if "skills" in p:
        kw["skills"] = split_skills(p["skills"])
        kw["skill_mode"] = "all" if p.get("skill_mode") == "all" else "any"
    for k in ("remote", "has_email", "has_linkedin"):
        if k in p:
            kw[k] = True
    for k in ("exp_min", "exp_max", "added_days"):
        if k in p:
            kw[k] = int(p[k])
    if "contacted" in p:
        kw["contacted"] = p["contacted"]
        if "contacted_days" in p:
            kw["contacted_days"] = int(p["contacted_days"])
    return kw


def describe(params: Mapping[str, Any]) -> str:
    """The saved search in plain words: 'python OR go, in sf, remote, 5+ years'."""
    p = clean_params(params)
    bits = []
    if p.get("q"):
        bits.append(p["q"])
    for k, label in (("title", "title {}"), ("company", "company {}"), ("location", "in {}"), ("tag", "skill {}"),
                     ("source", "found via {}"), ("client", "client {}"), ("stage", "stage {}")):
        if k in p:
            bits.append(label.format(p[k]))
    if "skills" in p:
        bits.append(("all of " if p.get("skill_mode") == "all" else "any of ") + p["skills"])
    if "remote" in p:
        bits.append("remote")
    if "exp_min" in p or "exp_max" in p:
        lo, hi = p.get("exp_min"), p.get("exp_max")
        bits.append(f"{lo} to {hi} years" if lo and hi else f"{lo}+ years" if lo else f"up to {hi} years")
    c = p.get("contacted")
    if c == "never":
        bits.append("never contacted")
    elif c == "older":
        bits.append(f"last contacted over {p['contacted_days']} days ago")
    elif c == "within":
        bits.append(f"contacted in the last {p['contacted_days']} days")
    if "added_days" in p:
        bits.append(f"added in the last {p['added_days']} days")
    if "has_email" in p:
        bits.append("has email")
    if "has_linkedin" in p:
        bits.append("has LinkedIn")
    return ", ".join(bits) or "everyone"
