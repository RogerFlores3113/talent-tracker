"""Find people saved twice, across the whole database.

Nothing here compares everyone with everyone. Each rule groups people by a key (a cleaned email, a LinkedIn
key, a folded name with a company or a phone, ...) in one pass, so 20,000 people take a fraction of a second.
Only people who share a key are ever compared, and a group of k people makes at most k - 1 pairs.

The recruiter decides what to do with a pair (merge it, or say "not the same person"). The AI can only read
the list. A pair the recruiter dismissed is remembered in one settings row and never comes back.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from .store import Store, _names_line_up, fold_name, fold_text, normalize_linkedin, phone_key, same_person_name

DISMISSED_KEY = "duplicates_dismissed"  # settings row: JSON list of "a-b" id pairs (a < b)
DISMISSED_MAX = 5000
BLOCK_MAX = 300  # the looser name rule skips a company block bigger than this; exact rules have no such limit

# Strongest first. A pair shows every reason that applies, strongest first.
REASONS = ("same email", "same LinkedIn", "same name and phone", "same name and company",
           "similar name and same company", "same name and company, but different emails",
           "similar name and company, but different emails")
STRONG = (REASONS[0], REASONS[1])  # exact email or LinkedIn: what "Merge all exact matches" is about
_RANK = {r: i for i, r in enumerate(REASONS)}


def email_key(email: str | None) -> str | None:
    """A cleaned email: lowercase; for Gmail, dots and a +tag don't count ('a.b+jobs@gmail.com' is 'ab@gmail.com')."""
    e = (email or "").strip().lower()
    if "@" not in e:
        return None
    local, _, domain = e.rpartition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}" if local else None


def _company_key(company: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", " ", fold_text(company))).strip()


def _similar(ta: list[str], tb: list[str]) -> bool:
    """store.same_person_name on names already split into folded words (so nothing is folded twice)."""
    return (sorted(ta) == sorted(tb) or _names_line_up(ta, tb) or (len(ta) == 2 and _names_line_up(ta[::-1], tb)))


def pair_key(a: int, b: int) -> str:
    lo, hi = sorted((int(a), int(b)))
    return f"{lo}-{hi}"


def dismissed(store: Store) -> set[str]:
    row = store._row("SELECT value FROM settings WHERE key = ?", (DISMISSED_KEY,))
    try:
        data = json.loads(row["value"]) if row else []
    except (ValueError, TypeError):
        data = []
    return {str(x) for x in data if isinstance(x, str) and re.fullmatch(r"\d+-\d+", x)}


def _save_dismissed(store: Store, pairs: set[str]) -> None:
    keep = sorted(pairs, key=lambda p: tuple(map(int, p.split("-"))))[-DISMISSED_MAX:]
    with store._lock, store._conn:
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                            "value = excluded.value", (DISMISSED_KEY, json.dumps(keep)))


def dismiss(store: Store, a: int, b: int) -> None:
    """The recruiter said these two are different people."""
    if a == b:
        raise ValueError("Pick two different people.")
    store.get_candidate(a)
    store.get_candidate(b)
    _save_dismissed(store, dismissed(store) | {pair_key(a, b)})


def forget_dismissed(store: Store) -> int:
    """Show every dismissed pair again. Returns how many there were."""
    n = len(dismissed(store))
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM settings WHERE key = ?", (DISMISSED_KEY,))
    return n


def _edges(members: list[int], skip: set[str]) -> list[tuple[int, int]]:
    """Pairs that connect a group: at most k - 1 of them. Each person links to the earliest earlier one they
    weren't dismissed against; a small group is also checked so that dismissing one pair never hides a third
    person who is still a likely match."""
    ordered = sorted(members)
    out: list[tuple[int, int]] = []
    if len(ordered) > 12 or not skip:
        for i, m in enumerate(ordered):
            for p in ordered[:i]:
                if pair_key(p, m) not in skip:
                    out.append((p, m))
                    break
        return out
    root = {m: m for m in ordered}

    def find(x: int) -> int:
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    for i, m in enumerate(ordered):
        for p in ordered[:i]:
            if pair_key(p, m) not in skip and find(p) != find(m):
                root[find(m)] = find(p)
                out.append((p, m))
    return out


def _complete(r: dict[str, Any]) -> int:
    return sum(1 for k in ("email", "phone", "linkedin_url", "current_title", "current_company", "location", "about")
               if (r.get(k) or "").strip())


def find_pairs(store: Store, limit: int | None = None) -> list[dict[str, Any]]:
    """Likely duplicate pairs, strongest reason first. Each: {"keep": id, "other": id, "reasons": [...]}, where
    "keep" is the more complete record (the older one when they tie). Dismissed pairs are left out."""
    rows = store._rows("SELECT id, name, email, phone, linkedin_url, linkedin_key, current_company, current_title,"
                       " location, CASE WHEN length(trim(coalesce(about, ''))) > 0 THEN 'x' END AS about FROM candidates")
    skip = dismissed(store)
    by_id = {r["id"]: r for r in rows}
    found: dict[tuple[int, int], set[str]] = defaultdict(set)

    def link(reason: str, groups: Any) -> None:
        for members in groups:
            if len(members) > 1:
                for a, b in _edges(members, skip):
                    found[(a, b)].add(reason)

    emails: dict[str, list[int]] = defaultdict(list)
    extra = store.other_emails_by_id()
    every: dict[int, set[str]] = {}  # every address a person has, cleaned
    links: dict[str, list[int]] = defaultdict(list)
    name_phone: dict[tuple[str, str], list[int]] = defaultdict(list)
    name_co: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_company: dict[str, list[int]] = defaultdict(list)
    names: dict[int, list[str]] = {}
    for r in rows:
        i = r["id"]
        mine = {k for k in (email_key(e) for e in [r["email"], *extra.get(i, [])]) if k}
        every[i] = mine
        for k in mine:
            emails[k].append(i)
        if r["linkedin_url"] or r["linkedin_key"]:
            if k := (r["linkedin_key"] or normalize_linkedin(r["linkedin_url"])):
                links[k].append(i)
        fname = fold_name(r["name"])
        if not fname:
            continue
        toks = fname.split()
        names[i] = toks
        if p := phone_key(r["phone"]):
            name_phone[(fname, p)].append(i)
        if co := _company_key(r["current_company"]):
            name_co[(fname, co)].append(i)
            if len(toks) > 1:
                by_company[co].append(i)
    link(REASONS[0], emails.values())
    link(REASONS[1], links.values())
    link(REASONS[2], name_phone.values())
    link(REASONS[3], name_co.values())

    # Similar names at one company: block by company and by the first letter of the last (or first) word, so
    # 'Priya R.', 'P. Raman', 'Mike Okonkwo' and 'Michael Okonkwo' meet, and nobody is compared with a whole company.
    for co, ids in by_company.items():
        if len(ids) < 2:
            continue
        blocks: dict[str, list[int]] = defaultdict(list)
        for i in ids:
            t = names[i]
            for ch in {t[-1][0], t[0][0]}:
                blocks[ch].append(i)
        for ids2 in blocks.values():
            if len(ids2) < 2 or len(ids2) > BLOCK_MAX:
                continue
            for x, a in enumerate(ids2):
                for b in ids2[x + 1:]:
                    lo, hi = (a, b) if a < b else (b, a)
                    if (lo, hi) in found and REASONS[4] in found[(lo, hi)]:
                        continue
                    if names[a] != names[b] and pair_key(lo, hi) not in skip and _similar(names[a], names[b]):
                        found[(lo, hi)].add(REASONS[4])

    pairs = []
    for (a, b), reasons in found.items():
        ra, rb = by_id[a], by_id[b]
        if not reasons & {REASONS[0], REASONS[1], REASONS[2]} and reasons & {REASONS[3], REASONS[4]}:
            # Only a name and a company agree. Two different LinkedIn profiles say two people (drop it); two
            # different emails might be a work and a home address, so the pair stays but moves to the bottom.
            la = ra["linkedin_key"] or normalize_linkedin(ra["linkedin_url"])
            lb = rb["linkedin_key"] or normalize_linkedin(rb["linkedin_url"])
            if la and lb and la != lb:
                continue
            ea, eb = every.get(a, set()), every.get(b, set())
            if ea and eb and not ea & eb:
                reasons = {REASONS[5] if REASONS[3] in reasons else REASONS[6]}
        keep, other = ((a, b) if (_complete(ra), -a) >= (_complete(rb), -b) else (b, a))
        pairs.append({"keep": keep, "other": other, "reasons": sorted(reasons, key=_RANK.__getitem__)})
    pairs.sort(key=lambda p: (_RANK[p["reasons"][0]], min(p["keep"], p["other"]), max(p["keep"], p["other"])))
    return pairs[:limit] if limit else pairs


def exact_matches(store: Store) -> list[dict[str, Any]]:
    """Pairs with the same email or the same LinkedIn, whose names also line up: safe to merge in one go. Each:
    {"keep", "other", "reasons", "a", "b"} with the two people as saved. A pair whose names differ is left out,
    because the same email on two different names needs a human look."""
    out = []
    for p in find_pairs(store):
        if not set(p["reasons"]) & set(STRONG):
            continue
        a, b = store.get_candidate(p["keep"]), store.get_candidate(p["other"])
        if same_person_name(a["name"], b["name"]):
            out.append({**p, "a": a, "b": b})
    return out


def merge_pairs(store: Store, pairs: list[tuple[int, int]]) -> dict[str, Any]:
    """Merge each (keep, other), the recruiter's choice in the app. A person already folded into another by an
    earlier pair in the list goes to that person instead. Returns {"merged": n, "skipped": [reasons]}."""
    into: dict[int, int] = {}
    merged, skipped = 0, []
    for keep, other in pairs:
        while keep in into:
            keep = into[keep]
        while other in into:
            other = into[other]
        if keep == other:
            continue
        try:
            store.merge_candidates(keep, other, by="app")
        except (LookupError, ValueError) as exc:
            skipped.append(f"{keep} and {other}: {exc}")
            continue
        into[other] = keep
        merged += 1
    return {"merged": merged, "skipped": skipped}


def plausible_for(store: Store, c: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The "Same person?" list on a profile: drops pairs the recruiter said are different people, and a match
    with a different name unless it shares an email or LinkedIn (a shared phone alone says nothing)."""
    skip = dismissed(store)
    li = normalize_linkedin(c.get("linkedin_url"))
    mine = {k for k in (email_key(e) for e in [c.get("email"), *store.other_emails(c["id"])]) if k}
    extra = store.other_emails_by_id() if mine and rows else {}
    out = []
    for d in rows:
        if pair_key(c["id"], d["id"]) in skip:
            continue
        theirs = {k for k in (email_key(e) for e in [d.get("email"), *extra.get(d["id"], [])]) if k}
        strong = bool((mine & theirs)
                      or (li and li == normalize_linkedin(d.get("linkedin_url"))))
        if same_person_name(c["name"], d["name"]) or strong:
            out.append(d)
    return out


def count(store: Store) -> int:
    """How many pairs there are, remembered until someone is added, edited, merged or dismissed."""
    stamp = store._row("SELECT count(*) AS n, max(updated_at) AS u, max(id) AS m FROM candidates")
    row = store._row("SELECT value FROM settings WHERE key = ?", (DISMISSED_KEY,))
    sig = (stamp["n"], stamp["u"], stamp["m"], row["value"] if row else "")
    cached = getattr(store, "_dup_count", None)
    if cached and cached[0] == sig:
        return cached[1]
    n = len(find_pairs(store))
    store._dup_count = (sig, n)
    return n


def already_here(store: Store, guess: dict[str, Any]) -> dict[str, Any] | None:
    """Someone already saved who this pasted profile looks like: by email or LinkedIn, or else by the same name
    at the same current company. Used before saving, so the recruiter can open them instead of adding a copy."""
    hit = store.find_duplicate(guess.get("email"), guess.get("linkedin_url"))
    if hit or not (guess.get("name") and guess.get("current_company")):
        return hit
    company = _company_key(guess["current_company"])
    if not company:
        return None
    for c in store.find_by_name(guess["name"]):
        if _company_key(c.get("current_company")) == company:
            return c
    return None


FOUND_LABELS = (("name", "name"), ("current_title", "title"), ("current_company", "company"),
                ("location", "location"), ("email", "email"), ("phone", "phone"), ("linkedin_url", "LinkedIn"),
                ("website", "website"), ("tags", "skills"), ("work_history", "work history"), ("about", "about"))


def found_fields(guess: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(what quick fill found, what it didn't), as plain words, for the check-before-saving page."""
    have = [label for key, label in FOUND_LABELS if guess.get(key)]
    return have, [label for key, label in FOUND_LABELS if not guess.get(key)]
