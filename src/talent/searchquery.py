"""The search box's small query language, and the helpers the filters and saved searches share.

Nothing here touches the database, and nothing here builds SQL or FTS5 text from what was typed. `parse_query`
turns the typed words into plain pieces (words, phrases, OR choices, exclusions, field filters); the store then
builds its own FTS5 expression from those pieces, quoting every one, so no typed character can become syntax.

What the box understands (AND stays the default):
    machine learning          every word must match
    "machine learning"        that exact phrase
    python OR go              either word (a lowercase "or" between two words counts too)
    -recruiter, NOT manager   leave out people who have that exact word
    title:engineer            the same as the Title filter (also company:, location:, skill: or tag:, source:);
                              punctuation stays, so skill:c++ and skill:node.js work
    skill:python OR go        an OR after a prefix value joins that same prefix (skill:python OR skill:go)
    python NEAR django        both words close together, within 5 words of each other, in either order
    python NEAR/3 django      the same, within 3 words (1 to 50). Quoted phrases work as either side, and
                              a chain like a NEAR b NEAR c needs all of them close together.
                              Only a capital NEAR, or NEAR/n in any case, counts: a plain "near" is an ordinary
                              word, so "lives near boston" and "near me" still search for those words.
                              NEAR works within one part of a profile (the same title, the same about text...)
    -title:manager            a minus or NOT before a prefix leaves out people whose field has it
    company:google company:meta   the same prefix twice means either one (OR); skill: twice still needs both
    c#, c++, f#, .net         whole words of their own (not the letter c or the word net), in plain words,
                              quotes, OR and NEAR alike
Parentheses are not supported: they are ignored, so `(python OR go)` reads as `python OR go`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Mapping

# field prefix -> the filter it sets
FIELD_ALIASES = {"title": "title", "company": "company", "location": "location", "skill": "skill", "skills": "skill",
                 "tag": "skill", "tags": "skill", "source": "source"}
_FIELD_AT = re.compile(r"([A-Za-z]+):")
_WORD = re.compile(r"\w+", re.UNICODE)
# Words that carry a symbol stay whole: c#, c++, f# and .net are skills of their own, not the letter c or the word net.
_TOKEN = re.compile(r"(?<!\w)\.net(?!\w)|(?<!\w)\w+(?:\+\+|#)(?![\w#+])|\w+", re.UNICODE | re.IGNORECASE)
_SYM_INDEX = re.compile(r"(?<!\w)\.(net)(?!\w)|(?<!\w)(\w+)(\+\+|#)(?![\w#+])", re.UNICODE | re.IGNORECASE)


def tokens(text: str | None) -> list[str]:
    """The words of a text, as typed: ordinary words, and c#, c++, f# and .net kept whole."""
    return _TOKEN.findall(text or "")


def fts_token(token: str) -> str:
    """The word the search index holds for a token: c# -> csharp, c++ -> cplusplus, .net -> dotnet."""
    t = token.lower()
    if t == ".net":
        return "dotnet"
    if t.endswith("++"):
        return t[:-2] + "plusplus"
    if t.endswith("#"):
        return t[:-1] + "sharp"
    return t


def sym_text(text: str | None) -> str | None:
    """A profile's text as the search index reads it: c# becomes csharp, c++ becomes cplusplus and .net becomes
    dotnet, so they are separate words from c and net. Capitals are kept, and None stays None."""
    if text is None:
        return None
    return _SYM_INDEX.sub(lambda m: "dot" + m.group(1) if m.group(1) else m.group(2) + (
        "sharp" if m.group(3) == "#" else "plusplus"), str(text))


_UNSYM = (
    (re.compile(r"(?<!\w)dot(net)(?!\w)", re.I), r".\1"),
    (re.compile(r"(?<!\w)(\w+)plusplus(?!\w)", re.I), r"\1++"),
    (re.compile(r"(?<!\w)([A-Za-z])sharp(?!\w)", re.I), r"\1#"),
)


def unsym_text(text: str | None) -> str | None:
    """sym_text the other way, for the excerpts shown under a match."""
    if not text:
        return text
    for rx, back in _UNSYM:
        text = rx.sub(back, text)
    return text
_NEAR_TOKEN = re.compile(r"(?:NEAR|[Nn][Ee][Aa][Rr]/\d{1,3})")  # NEAR, or NEAR/3 in any case. Bare "near" is a word.
NEAR_DEFAULT, NEAR_MAX = 5, 50
MAX_TOKENS = 40  # a pasted paragraph is not a query; the rest is ignored


@dataclass
class Item:
    text: str  # words joined by one space, lowercase
    phrase: bool = False  # two or more words that must sit together
    raw: str = ""  # the same words as typed (capitals kept: "Dana Okafor" reads as a name)
    plain: bool = True  # typed as ordinary letters and digits (no c++, c#, o'brien)
    near: tuple["Item", ...] = ()  # python NEAR django: the words or phrases that must sit close together
    dist: int = 0  # ... and how many words apart at most

    @property
    def leaves(self) -> tuple["Item", ...]:
        """The words and phrases this stands for: itself, or the operands of a NEAR."""
        return self.near or (self,)

    @classmethod
    def of(cls, raw: str, phrase: bool = False) -> "Item | None":
        typed = tokens(raw)
        words = [w.lower() for w in typed]
        if not words:
            return None
        return cls(" ".join(words), phrase and len(words) > 1, " ".join(typed),
                   plain=bool(re.fullmatch(r"\w+", (raw or "").strip())))

    @property
    def prefix_ok(self) -> bool:
        """The last word while typing ("pyth" finds Python) needs 3 plain characters: "c++" or "go" don't spread."""
        return self.plain and not self.phrase and " " not in self.text and len(self.text) >= 3


@dataclass
class Advice:
    """One thing that looks like a slip, in plain words. `fix` turns the typed text into what was probably meant.
    always=False: only worth saying when the search found nobody (skill:python django is often meant as a skill
    and a word, and "lives near boston" is just English)."""
    note: str
    fix: Callable[[str], str] | None = None
    always: bool = True


_PREFIXES = "|".join(sorted(FIELD_ALIASES, key=len, reverse=True))
_MULTI_VALUE = re.compile(rf'(?<![\w:])(-?)({_PREFIXES}):([^\s"():]+)\s+([^\s"():-][^\s"():]*)(?=[\s"()]|$)', re.I)
_PROXIMITY = re.compile(r'"([^"]+)"\s*~\s*(\d{1,3})(?!\d)')
_LONE_DASH = re.compile(r'(?:(?<=\s)|^)-(?=\s|$)')
_QUOTED = re.compile(r'"[^"]*"')
_OPWORDS = {"or", "and", "not", "near"}


def query_advice(text: str | None) -> list[Advice]:
    """Notes for mistakes people bring from other search boxes: several words after a prefix without quotes, the
    "a b"~3 proximity form, a lowercase not or near, and a minus with nothing after it. Never raises."""
    s = (text or "")[:2000]
    bare = _QUOTED.sub('""', s)  # what is inside quotes was typed on purpose
    out: list[Advice] = []
    # skill:machine learning  ->  skill:"machine learning"
    for m in _MULTI_VALUE.finditer(bare):
        neg, pre, first, second = m.groups()
        if first.lower() in _OPWORDS or second.lower() in _OPWORDS:
            continue
        pat = re.compile(rf'(?<![\w:])({re.escape(neg + pre)}:){re.escape(first)}\s+{re.escape(second)}(?!\w)', re.I)
        out.append(Advice(
            f'{pre}:{first} only takes the one word after the colon, so {second} was searched as a separate word. '
            f'For a value of several words, use quotes: {pre}:"{first} {second}".',
            lambda t, pat=pat, v=f"{first} {second}": pat.sub(lambda mm: f'{mm.group(1)}"{v}"', t), always=False))
    # "python django"~3  ->  python NEAR/3 django
    for m in _PROXIMITY.finditer(s):
        words = m.group(1).split()
        if len(words) < 2:
            continue
        n = max(1, min(int(m.group(2)), NEAR_MAX))
        fixed = f" NEAR/{n} ".join(words)
        out.append(Advice(
            f'"{m.group(1)}"~{m.group(2)} is how other tools write proximity. Here, write {fixed}: the words must be '
            f'at most {n} words apart.', lambda t, old=m.group(0), new=fixed: t.replace(old, new)))
    toks = bare.split()
    # python not manager
    for k, t in enumerate(toks):
        nxt = toks[k + 1] if k + 1 < len(toks) else ""
        if t == "not" and nxt and nxt.lower() not in _OPWORDS and not nxt.startswith("-") and len(toks) > 1:
            out.append(Advice(
                'A lowercase not is searched as a plain word. To leave people out, write NOT in capitals '
                f'(NOT {nxt}) or put a minus in front (-{nxt}).',
                lambda t: re.sub(r'(?<![\w-])not(?=\s+[^\s-])', 'NOT', t)))
            break
    # python near django
    for k, t in enumerate(toks):
        if t == "near" and 0 < k < len(toks) - 1:
            out.append(Advice(
                'A lowercase near is searched as a plain word. For two words close together, write NEAR in capitals '
                f'({toks[k - 1]} NEAR {toks[k + 1]}), or NEAR/3 for at most 3 words apart.',
                lambda t: re.sub(r'(?<=\s)near(?=\s)', 'NEAR', t), always=False))
            break
    # a minus with nothing after it
    if _LONE_DASH.search(bare):
        out.append(Advice(
            'A minus on its own was ignored. To leave people out, put it right before the word (-manager), or write '
            'NOT manager.',
            lambda t: re.sub(r'(?:(?<=\s)|^)-\s+(?=[^\s-])', '-', t) if re.search(r'(?:(?<=\s)|^)-\s+[^\s-]', t)
            else re.sub(r'(?:(?<=\s)|^)-(?=\s|$)', '', t).strip()))
    return out


@dataclass
class ParsedQuery:
    groups: list[list[Item]] = field(default_factory=list)  # AND of OR-groups
    exclude: list[Item] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)  # title, company, location, source, skill
    skill_any: bool = False  # skill:a OR skill:b
    not_fields: dict[str, list[str]] = field(default_factory=dict)  # -title:x, NOT skill:x
    notes: list[str] = field(default_factory=list)  # things that could not be read as typed, in plain words
    used_syntax: bool = False  # quotes, OR, NOT, a minus or a field prefix was understood
    hints: list[tuple[str, str]] = field(default_factory=list)  # (typed prefix, the prefix it looks like): skil -> skill
    unknown_prefix: bool = False  # something like foo:bar, which is read as plain words
    advice: list[Advice] = field(default_factory=list)  # common slips: a note each, and the fix to offer

    @property
    def operator_mode(self) -> bool:
        """A phrase or an OR needs the operator search; plain words keep the everyday search (typo help, notes)."""
        return (any(i.phrase or i.near for g in self.groups for i in g)
                or any(len(g) > 1 for g in self.groups))

    @property
    def bare(self) -> str:
        """The plain words, for the everyday search. Empty in operator mode."""
        if self.operator_mode:
            return ""
        return " ".join(g[0].raw or g[0].text for g in self.groups)

    @property
    def structured(self) -> bool:
        return self.used_syntax or bool(self.hints) or self.unknown_prefix or bool(self.notes)

    def apply_advice(self, text: str, *, only_always: bool = False) -> str | None:
        """`text` with the fix of each piece of advice applied (skill:machine learning -> skill:"machine learning",
        "a b"~3 -> a NEAR/3 b), or None when nothing changed. only_always leaves out the advice that is only
        spoken when nothing was found."""
        out = text
        for a in self.advice:
            if a.fix and (a.always or not only_always):
                out = a.fix(out)
        return out if out != text else None

    def fix_prefixes(self, text: str) -> str | None:
        """`text` with each mistyped prefix swapped for the one it looks like (skil:python -> skill:python), or
        None when no prefix was mistyped."""
        if not self.hints:
            return None
        out = text
        for typed, fixed in self.hints:
            out = re.sub(rf"(?<!\w){re.escape(typed)}:", f"{fixed}:", out, flags=re.I)
        return out if out != text else None

    def explain(self) -> str:
        """The search as it was read, in plain words: '(python OR go) AND NOT recruiter AND title: engineer'.
        Empty when nothing special was typed."""
        if not (self.used_syntax or self.operator_mode):
            return ""

        def show(i: Item) -> str:
            if i.near:
                ops = [show(o) for o in i.near]
                if len(ops) == 2:
                    return f"{ops[0]} within {i.dist} words of {ops[1]}"
                return f"{', '.join(ops[:-1])} and {ops[-1]} all within {i.dist} words of each other"
            return f'"{i.text}"' if i.phrase else (i.raw or i.text)

        label = {"skill": "skill", "title": "title", "company": "company", "location": "location",
                 "source": "found via"}
        many = len(self.groups) + len(self.fields) + len(self.exclude) + len(self.not_fields) > 1
        parts = []
        for g in self.groups:
            one = " OR ".join(show(i) for i in g)
            parts.append(f"({one})" if len(g) > 1 and many else one)
        for fld, vals in self.fields.items():
            joiner = " AND " if fld == "skill" and not self.skill_any else " OR "
            parts.append(f"{label.get(fld, fld)}: " + joiner.join(vals))
        parts += [f"NOT {show(i)}" for i in self.exclude]
        for fld, vals in self.not_fields.items():
            parts += [f"NOT {label.get(fld, fld)}: {v}" for v in vals]
        return " AND ".join(parts)


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
        elif m and len(m.group(1)) >= 3 and s[m.end():m.end() + 2] != "//":
            typed, fixed = m.group(1), nearest_prefix(m.group(1))
            empty = m.end() >= n or s[m.end()].isspace()
            if fixed:  # skil:python: the mistyped prefix is left off and the value is searched as plain words
                pq.hints.append((typed, fixed))
                pq.notes.append(f"{typed}: isn't a search prefix, so it was left off and the rest searched as plain "
                                f"words. The closest prefix is {fixed}:.")
                i = m.end()
                if empty:
                    continue
            elif not empty:  # foo:bar reads as the two plain words foo and bar
                pq.unknown_prefix = True
                pq.notes.append(f"{typed}: isn't a search prefix, so it was read as plain words. The prefixes are "
                                "title, company, location, skill, tag and source.")
        if i < n and s[i] == '"':
            j = s.find('"', i + 1)
            if j == -1:  # unbalanced: the quote is dropped and the rest reads as plain words
                for w in tokens(s[i + 1:]):
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
        if fld is None and not neg and _NEAR_TOKEN.fullmatch(raw):
            dist = int(raw[5:]) if "/" in raw else NEAR_DEFAULT
            toks.append(("near", max(1, min(dist, NEAR_MAX)), False, None))
            continue
        toks.append(("word", raw, neg, fld))

    toks = _join_near(toks, pq)
    pq.advice = query_advice(s)
    pq.notes += [a.note for a in pq.advice if a.always]

    # A lowercase "or" between two words is an OR too ("python or go").
    for k, (kind, value, neg, fld) in enumerate(toks):
        if (kind == "word" and value == "or" and not neg and fld is None and 0 < k < len(toks) - 1
                and toks[k - 1][0] in ("word", "phrase", "nearset") and toks[k + 1][0] in ("word", "phrase", "nearset")
                and not toks[k + 1][2]):
            toks[k] = ("op", "OR", False, None)
            pq.used_syntax = True

    pending_or = pending_not = False
    repeated: set[str] = set()
    last: str | None = None  # the prefix of the value just before (None after a plain word or phrase)
    for kind, value, neg, fld in toks:
        if kind == "op":
            if value == "OR":
                pending_or = True
            elif value == "NOT":
                pending_not = True
            continue
        neg = neg or pending_not
        pending_not = False
        if kind == "nearset":
            ops = [Item.of(v, phrase=(k == "phrase")) for k, v in value[0]]
            ops = [o for o in ops if o]
            if len(ops) < 2:  # nothing left to be near: whatever is left reads as an ordinary word
                item = ops[0] if ops else None
            else:
                item = Item(" ".join(o.text for o in ops), False, " ".join(o.raw for o in ops), False,
                            near=tuple(ops), dist=value[1])
        else:
            item = Item.of(value, phrase=(kind == "phrase"))
        if item is None:
            continue
        # A prefix value keeps its punctuation: skill:c++, skill:node.js, source:ci/cd.
        text = " ".join((value if isinstance(value, str) else "").lower().split()).rstrip(",;")
        if neg:  # a minus or NOT leaves people out; an OR before it means nothing
            if fld:
                pq.not_fields.setdefault(fld, []).append(text)
            else:
                pq.exclude.append(item)
            pending_or = False
            continue
        if fld:
            vals = pq.fields.setdefault(fld, [])
            if pending_or and last == fld and vals:
                vals.append(text)  # skill:python OR skill:go
                pq.skill_any = pq.skill_any or fld == "skill"
            else:
                if pending_or:
                    pq.notes.append(f"An OR next to {fld}: only works between values of the same prefix, "
                                    f"like {fld}:a OR {fld}:b, so it was read as AND.")
                if fld == "skill":
                    vals.append(text)
                elif vals:  # company:google company:meta: either one
                    if text not in vals:
                        vals.append(text)
                    if fld not in repeated:
                        repeated.add(fld)
                        pq.notes.append(f"{fld}: was typed more than once, so people who match any of the values "
                                        "are shown (OR).")
                else:
                    vals.append(text)
            last, pending_or = fld, False
            continue
        if pending_or and last and not item.near:  # skill:python OR go: the word joins that prefix
            pq.fields[last].append(text)
            pq.skill_any = pq.skill_any or last == "skill"
        elif pending_or and pq.groups:
            pq.groups[-1].append(item)
            last = None
        else:
            pq.groups.append([item])
            last = None
        pending_or = False
    return pq


def _join_near(toks: list, pq: ParsedQuery) -> list:
    """Fold `a NEAR b` (and chains) into one ("nearset", (operands, distance)) token. A NEAR without a plain word
    or phrase on each side is left out, with a note."""
    out: list = []
    k = 0
    while k < len(toks):
        kind, value, neg, fld = toks[k]
        if kind != "near":
            out.append(toks[k])
            k += 1
            continue
        left = out[-1] if out else None
        right = toks[k + 1] if k + 1 < len(toks) else None
        ok = (left and left[0] in ("word", "phrase", "nearset") and not left[2] and left[3] is None
              and right and right[0] in ("word", "phrase") and not right[2] and right[3] is None)
        if not ok:
            pq.notes.append("NEAR needs a word or a quoted phrase on each side (python NEAR django), so it was left out.")
            k += 1
            continue
        if left[0] == "nearset":
            ops, dist = left[1]
            out[-1] = ("nearset", ([*ops, (right[0], right[1])], max(dist, value)), False, None)
        else:
            out[-1] = ("nearset", ([(left[0], left[1]), (right[0], right[1])], value), False, None)
        pq.used_syntax = True
        k += 2
    return out


def _osa(a: str, b: str) -> int:
    """Edit distance where swapping two neighbouring letters counts as one change (compnay -> company)."""
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def nearest_prefix(word: str) -> str | None:
    """The known prefix a mistyped one looks like (skil -> skill, tittle -> title, locaton -> location), or None.
    One slip for short words, two for longer ones."""
    w = (word or "").lower()
    if len(w) < 3 or w in FIELD_ALIASES:
        return None
    cap = 1 if len(w) <= 5 else 2
    best: tuple[int, str] | None = None
    for known in FIELD_ALIASES:
        d = _osa(w, known)
        if d <= cap and (best is None or d < best[0]):
            best = (d, known)
    return best[1] if best else None


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
COLOR_CHOICES = ("red", "orange", "yellow", "green", "blue", "purple")
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
            if num is None or (k == "added_days" and num == 0):  # "in the last 0 days" means nothing: not set
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
        elif k == "color":
            s = s.lower()
            if s not in (*COLOR_CHOICES, "none"):
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
    if "color" in p:
        kw["color"] = p["color"]
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
    if "color" in p:
        bits.append("no color" if p["color"] == "none" else f"{p['color']} color")
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
