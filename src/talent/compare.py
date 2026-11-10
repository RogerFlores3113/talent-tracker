"""Compare and contrast a few chosen people side by side.

One builder feeds the app page (/candidates/compare) and the in-chat tool (compare_candidates), so both say the same
things. It reads only saved data, writes nothing, and treats every candidate field as text to show, never as an
instruction. The "At a glance" lines are plain sentences made from the data; names appear only as names.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlsplit

from .links import link_label, stored_links
from .searchquery import years_of_experience

MIN_PEOPLE = 2
MAX_PEOPLE = 6
GLANCE_MAX = 6
SKILL_NAMES_PER_LINE = 4

# The rows, in the order they are shown. "differs" is computed for the ones where a difference means something.
ROWS = (
    ("title", "Title and company"), ("location", "Location"), ("years", "Years of experience"),
    ("skills", "Skills"), ("jobs", "Jobs and stages"), ("source", "Found via"), ("contacted", "Last contacted"),
    ("added", "Added"), ("links", "Links"), ("about", "About"),
)
# Rows about the recruiter's own work with the person, left out of the hiring manager view.
HM_HIDDEN = ("source", "contacted", "added")


def parse_ids(raw: Any) -> list[int]:
    """'1, 2,x,2,3' -> [1, 2, 3]: whole numbers only, no repeats, in the order given."""
    out: list[int] = []
    for part in str(raw or "").replace(" ", "").split(","):
        if part.isdigit() and int(part) > 0 and int(part) not in out:
            out.append(int(part))
    return out


def years_text(years: float | None) -> str:
    if years is None:
        return ""
    if years < 1:
        return "under 1 year"
    n = round(years * 2) / 2  # halves are plenty for a comparison
    s = str(int(n)) if n == int(n) else f"{n:.1f}"
    return f"{s} {'year' if n == 1 else 'years'}"


def _day(iso: Any) -> str:
    return str(iso or "")[:10]


def _web(u: Any) -> str | None:
    u = str(u or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else None


def _person(store: Any, cid: int, today: date | None) -> dict[str, Any]:
    c = store.candidate_detail(cid, timeline_limit=5)
    roles = [(w.get("start"), w.get("end")) for w in c.get("work_history") or []]
    years = years_of_experience(roles, today)
    links = []
    if _web(c.get("linkedin_url")):
        links.append({"label": "LinkedIn" if "linkedin.com" in c["linkedin_url"].lower() else "Profile", "url": c["linkedin_url"]})
    if _web(c.get("website")):
        links.append({"label": "Website", "url": c["website"]})
    links += [{"label": link_label(u), "url": u} for u in stored_links(c.get("links")) if _web(u)]
    return {
        "id": c["id"], "name": c["name"], "color": c.get("color") or "", "title": c.get("current_title") or "",
        "company": c.get("current_company") or "", "location": c.get("location") or "", "years": years,
        "years_text": years_text(years), "tags": list(c.get("tags") or []), "source": c.get("source") or "",
        "last_contacted_at": c.get("last_contacted_at") or "", "created_at": c.get("created_at") or "",
        "about": (c.get("about") or "").strip(), "links": links,
        "jobs": [{"job_id": p["job_id"], "job": p["job_title"] + (f" ({p['job_company']})" if p.get("job_company") else ""),
                  "stage": p["stage"]} for p in c.get("pipelines") or []],
    }


def short_names(people: list[dict[str, Any]]) -> dict[int, str]:
    """A short name for each person: the first name, or the full name where two share one."""
    firsts = [(p["name"].split() or [""])[0] for p in people]
    return {p["id"]: (f if firsts.count(f) == 1 and f else p["name"]) for p, f in zip(people, firsts)}


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _skill_list(skills: list[str]) -> str:
    shown = skills[:SKILL_NAMES_PER_LINE]
    more = len(skills) - len(shown)
    return _join(shown) + (f" and {more} more" if more > 0 else "")


def split_skills(people: list[dict[str, Any]]) -> tuple[list[str], dict[int, list[str]], dict[int, list[str]]]:
    """(skills everyone has, skills only one person has by id, the rest by id), each person's own spelling, order kept.
    With one distinct skill list, nothing is "only them"."""
    have = {p["id"]: {t.lower() for t in p["tags"]} for p in people}
    counts: dict[str, int] = {}
    for tags in have.values():
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
    n = len(people)
    shared: list[str] = []
    seen: set[str] = set()
    for t in people[0]["tags"]:
        if counts.get(t.lower()) == n and t.lower() not in seen:
            seen.add(t.lower())
            shared.append(t)
    only: dict[int, list[str]] = {p["id"]: [t for t in p["tags"] if counts.get(t.lower()) == 1] for p in people}
    rest: dict[int, list[str]] = {p["id"]: [t for t in p["tags"] if 1 < counts.get(t.lower(), 0) < n] for p in people}
    return shared, only, rest


def skill_counts(people: list[dict[str, Any]]) -> dict[str, int]:
    """How many of these people have each skill (lower case)."""
    counts: dict[str, int] = {}
    for p in people:
        for t in {t.lower() for t in p["tags"]}:
            counts[t] = counts.get(t, 0) + 1
    return counts


def glance_names(people: list[dict[str, Any]]) -> dict[int, str]:
    """Names for the glance lines: first names, unless two people share a first name, then everyone's full name."""
    firsts = [(p["name"].split() or [""])[0] for p in people]
    clash = any(not f or firsts.count(f) > 1 for f in firsts)
    return {p["id"]: (p["name"] if clash else f) for p, f in zip(people, firsts)}


def partly_shared(people: list[dict[str, Any]]) -> list[tuple[str, list[int]]]:
    """Skills some of them have (not everyone, not just one): (skill in the first holder's spelling, holder ids),
    the most shared first, ties in the order the skills first appear."""
    n = len(people)
    holders: dict[str, list[int]] = {}
    spell: dict[str, str] = {}
    for p in people:
        for t in p["tags"]:
            k = t.lower()
            spell.setdefault(k, t)
            if p["id"] not in holders.setdefault(k, []):
                holders[k].append(p["id"])
    order = list(holders)
    mid = [k for k in order if 1 < len(holders[k]) < n]
    mid.sort(key=lambda k: (-len(holders[k]), order.index(k)))
    return [(spell[k], holders[k]) for k in mid]


def glance(people: list[dict[str, Any]], shared: list[str], only: dict[int, list[str]],
           contact: bool = True) -> list[str]:
    """A few plain sentences about what is the same and what is different. At most GLANCE_MAX, the useful ones first.
    contact=False leaves out the never-contacted lines (the hiring manager view)."""
    n = len(people)
    name = glance_names(people)
    out: list[str] = []
    if shared:
        out.append(f"{'Both have' if n == 2 else f'All {n} have'} {_skill_list(shared)}.")
    # skills that only one person has: one line per person, the people with most first
    for p in sorted((p for p in people if only[p["id"]]), key=lambda p: -len(only[p["id"]]))[:2]:
        out.append(f"Only {name[p['id']]} has {_skill_list(only[p['id']])}.")
    # skills some of them have: one line per group of the same people, the biggest groups first
    by_holders: dict[tuple[int, ...], list[str]] = {}
    for skill, ids in partly_shared(people):
        by_holders.setdefault(tuple(ids), []).append(skill)
    for ids, skills in list(by_holders.items())[:2]:
        out.append(f"{_join([name[i] for i in ids])} {'has' if len(ids) == 1 else 'have'} {_skill_list(skills)}.")
    # location: all the same, or one person apart from the rest
    places = {p["id"]: " ".join(p["location"].split()) for p in people}
    given = {i: v for i, v in places.items() if v}
    if len(given) == n:
        distinct = {v.lower() for v in given.values()}
        if len(distinct) == 1:
            out.append(f"{'Both are' if n == 2 else f'All {n} are'} in {next(iter(given.values()))}.")
        else:
            by: dict[str, list[int]] = {}
            for i, v in given.items():
                by.setdefault(v.lower(), []).append(i)
            sizes = sorted(by.values(), key=len)
            if len(by) == 2 and n >= 3 and len(sizes[0]) == 1:
                one = sizes[0][0]
                other = next(given[i] for i in given if i != one)
                out.append(f"{name[one]} is in {given[one]}; the others are in {other}.")
            elif n == 2:
                a, b = people
                out.append(f"{name[a['id']]} is in {given[a['id']]}; {name[b['id']]} is in {given[b['id']]}.")
    # most experience
    ys = [(p["years"], p) for p in people if p["years"] is not None]
    if len(ys) >= 2:
        ys.sort(key=lambda t: -t[0])
        if round(ys[0][0] * 2) > round(ys[1][0] * 2):
            out.append(f"{name[ys[0][1]['id']]} has the most experience ({years_text(ys[0][0])}).")
    # never contacted
    never = [p for p in people if not p["last_contacted_at"]] if contact else []
    if never and len(never) == n:
        out.append("Nobody here has been contacted yet.")
    elif never:
        who = _join([name[p["id"]] for p in never])
        out.append(f"{who} {'was' if len(never) == 1 else 'were'} never contacted.")
    return out[:GLANCE_MAX]


def differs(people: list[dict[str, Any]]) -> dict[str, bool]:
    """Per row: do the values differ across these people? (Added and About are not marked: they always differ.)"""
    def varied(vals: list[Any]) -> bool:
        return len({v for v in vals}) > 1

    return {
        "title": varied([(" ".join(p["title"].lower().split()), " ".join(p["company"].lower().split())) for p in people]),
        "location": varied([" ".join(p["location"].lower().split()) for p in people]),
        "years": varied([round(p["years"] * 2) if p["years"] is not None else None for p in people]),
        "skills": varied([tuple(sorted(t.lower() for t in p["tags"])) for p in people]),
        "jobs": varied([tuple(sorted((j["job_id"], j["stage"].lower()) for j in p["jobs"])) for p in people]),
        "source": varied([p["source"].strip().lower() for p in people]),
        "contacted": varied([_day(p["last_contacted_at"]) for p in people]),
    }


def empty_rows(people: list[dict[str, Any]]) -> list[str]:
    """Rows where nobody has anything saved: the page and the view leave them out."""
    def none(f: Any) -> bool:
        return not any(f(p) for p in people)

    checks = {
        "title": lambda p: p["title"] or p["company"], "location": lambda p: p["location"],
        "years": lambda p: p["years"] is not None, "skills": lambda p: p["tags"], "jobs": lambda p: p["jobs"],
        "source": lambda p: p["source"].strip(), "contacted": lambda p: p["last_contacted_at"],
        "links": lambda p: p["links"], "about": lambda p: p["about"],
    }
    return [k for k, f in checks.items() if none(f)]


def years_gap(people: list[dict[str, Any]]) -> bool:
    """Years is shown, but missing for some of them."""
    return any(p["years"] is not None for p in people) and any(p["years"] is None for p in people)


def hl_counts(people: list[dict[str, Any]], hl: list[str]) -> dict[int, int]:
    """How many of the highlighted skills each person has."""
    return {p["id"]: sum(1 for h in hl if h in {t.lower() for t in p["tags"]}) for p in people}


def public_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Links that mean something to someone else: none that point at this computer."""
    out = []
    for link in links:
        host = (urlsplit(link["url"]).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost"):
            continue
        out.append(link)
    return out


def build(store: Any, ids: list[int], today: date | None = None) -> dict[str, Any]:
    """The comparison for these people (the first MAX_PEOPLE that exist). Raises ValueError with a friendly line when
    fewer than MIN_PEOPLE are left. "left_out" names the people past the limit."""
    asked = list(dict.fromkeys(int(i) for i in ids))
    people, missing, left_out = [], [], []
    for cid in asked:
        if len(people) >= MAX_PEOPLE:
            try:
                left_out.append({"id": cid, "name": store.get_candidate(cid)["name"]})
            except (KeyError, LookupError):
                missing.append(cid)
            continue
        try:
            people.append(_person(store, cid, today))
        except (KeyError, LookupError):
            missing.append(cid)
    if len(people) < MIN_PEOPLE:
        raise ValueError("Pick two to six saved people to compare." if not missing else
                         "Fewer than two of those people are saved, so there is nothing to compare.")
    shared, only, rest = split_skills(people)
    counts = skill_counts(people)
    return {"people": people, "shared": shared, "only": only, "rest": rest, "missing": missing, "left_out": left_out,
            "counts": counts, "glance": glance(people, shared, only), "glance_hm": glance(people, shared, only, contact=False),
            "differs": differs(people), "empty": empty_rows(people), "years_gap": years_gap(people),
            "capped": bool(left_out), "asked": len(people) + len(left_out)}


def text_lines(d: dict[str, Any]) -> list[str]:
    """The comparison as plain lines for the AI (candidate text, so the caller fences it)."""
    out = list(d["glance"])
    n = len(d["people"])
    for p in d["people"]:
        bits = [p["name"]]
        role = " at ".join(x for x in (p["title"], p["company"]) if x)
        if role:
            bits.append(role)
        if p["location"]:
            bits.append(p["location"])
        if p["years_text"]:
            bits.append(p["years_text"])
        if p["tags"]:
            shared_keys = {t.lower() for t in d["shared"]}
            parts = []
            for label, skills in (("shared by all", [t for t in p["tags"] if t.lower() in shared_keys]),
                                  ("some of them", [f"{t}, {d['counts'][t.lower()]} of {n}" for t in d["rest"][p["id"]]]),
                                  ("only them", d["only"][p["id"]])):
                if skills:
                    parts.append(f"{label}: " + ("; " if label == "some of them" else ", ").join(skills))
            bits.append("skills (" + " | ".join(parts) + ")")
        if p["jobs"]:
            bits.append("jobs: " + "; ".join(f"{j['job']}: {j['stage']}" for j in p["jobs"]))
        bits.append("last contacted " + (_day(p["last_contacted_at"]) if p["last_contacted_at"] else "never"))
        out.append("- " + " | ".join(bits))
    if d.get("left_out"):
        out.append("Left out, because compare takes 6 at a time: "
                   + ", ".join(f"{x['name']} (id {x['id']})" for x in d["left_out"]) + ".")
    return out


def _hl_first(skills: list[str], hl: list[str]) -> list[str]:
    """The highlighted skills first, in the order they were clicked, then the others as they were."""
    mine = {t.lower(): t for t in skills}
    first = [mine[h] for h in hl if h in mine]
    return first + [t for t in skills if t not in first]


def skill_groups(d: dict[str, Any], hl: list[str] | None = None) -> dict[int, list[dict[str, Any]]]:
    """Each person's skills in three groups for the skills row: Shared (everyone), Some of them (with how many have
    it) and Only them. Each skill carries its highlight position (-1 when not highlighted) so the page can colour it
    like the Candidates table does."""
    hl = hl or []
    n = len(d["people"])
    shared_keys = {t.lower() for t in d["shared"]}
    out: dict[int, list[dict[str, Any]]] = {}
    for p in d["people"]:
        groups = []
        for key, label, skills in (
                ("shared", "Shared", [t for t in p["tags"] if t.lower() in shared_keys]),
                ("some", "Some of them", d["rest"][p["id"]]),
                ("only", "Only them", d["only"][p["id"]])):
            if skills:
                groups.append({"key": key, "label": label, "skills": [
                    {"name": t, "pos": hl.index(t.lower()) if t.lower() in hl else -1,
                     "count": d["counts"][t.lower()] if key == "some" else 0, "of": n}
                    for t in _hl_first(skills, hl)]})
        out[p["id"]] = groups
    return out
