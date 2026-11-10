"""The guided tour for first-time users: its steps, and where the user is in it.

Where the user is (active, mode, step, whether the tour loaded the demo people, and whether a tour
ever ended) lives in the settings table, so the tour survives moving between pages and restarting
the app. The browser draws it from the steps this module renders (static/app.js).

Two modes. "full" runs on first use: it loads the demo people, walks the real pages, then clears
the demo data. "short" is the replay: the same walk on the user's own pages, and it never loads or
clears anything.
"""
from __future__ import annotations

from typing import Any

from .sample_data import _touched_people, load_sample_data, remove_sample_data, sample_ids
from .store import NotFound, Store, now_iso

FULL, SHORT = "full", "short"
MODES = (FULL, SHORT)

# Each step: id, title, body, path, target, modes, and optionally needs, pulse, action.
#   path: where the step lives. "/x" is a page, "person" and "job" are filled in with a candidate or a
#   job (the demo ones in the full tour, the user's own in the short one), None means wherever the user is.
#   target: CSS selectors. The first one found is lit up; none found means the popover sits in the middle.
#   pulse: a button inside the target to draw the eye to (a disabled one is left alone: the lit panel is the cue).
#   reach: a second element to leave clickable beside the target (the color bubble in the first row), so the recruiter
#   can really try it during the tour.
STEPS: list[dict[str, Any]] = [
    {"id": "welcome", "title": "Welcome to Talent Search", "path": None, "target": [], "modes": (FULL,),
     "body": "This is your own candidate list, kept on this computer. Your AI app can do the typing: ask it "
             "in plain words and it saves, searches and updates people here. The tour takes about two minutes."},
    {"id": "today", "title": "Today", "path": "/", "target": ["section.today", "main h1"], "modes": (SHORT,),
     "body": "Today lists what needs you: interviews coming up, people stuck in a stage and follow-ups that are due."},
    {"id": "claude", "title": "Connect Claude Desktop", "path": "/connect", "target": ["#claude-panel"],
     "pulse": "#claude-panel .btn.primary:not([disabled])", "modes": (FULL, SHORT),
     "body": "The Connect Claude button connects Claude Desktop with one click. It adds Talent Search to Claude's "
             "settings and does nothing else. The tour will not click it for you. Click it now or later, then press "
             "Next. Don't have it? Press Next, you can connect later."},
    {"id": "codex", "title": "Connect Codex or another app", "path": "/connect", "target": ["#codex-panel"],
     "pulse": "#codex-panel .btn.primary:not([disabled])", "modes": (FULL, SHORT),
     "body": "Using Codex instead? One click here connects it the same way. LM Studio has its own button just below, "
             "and any other app that runs local tools (Cursor, Windsurf and others) has its own setup steps on the Connect page. "
             "Don't have it? Press Next, you can connect later."},
    {"id": "skills", "title": "Skills for your AI app", "path": "/connect", "target": ["#skills-panel"],
     "pulse": "#skills-panel .btn.primary:not([disabled])", "modes": (FULL, SHORT),
     "body": "A skill is a short page of instructions that teaches your AI app to work well here. Press Add skills "
             "to add them. You see the exact files first. Don't have one? Press Next, you can add them later."},
    {"id": "load", "title": "Try it with demo people", "path": None, "target": [], "modes": (FULL,),
     "action": "load",
     "body": "Want something to click on? The tour can add 16 made-up people and 2 jobs, all named Example. "
             "When the tour ends it clears them. Nothing real is touched."},
    {"id": "search", "title": "Search and filter", "path": "/candidates",
     "target": [".tablebar input[name=q]", "main h1"], "modes": (FULL, SHORT),
     "body": "Type a skill, a company, a title or a name. Try \"rust\" or \"payments\". The menus beside it filter by "
             "skill, location, job or stage, and a column heading sorts."},
    {"id": "colors", "title": "Color tags", "path": "/candidates", "target": [".colorbar"],
     "reach": ".table-wrap td.colorcell .cbub", "pulse": ".table-wrap td.colorcell .cbub", "modes": (FULL, SHORT),
     "body": "Give people a color to sort them your way. Click the round bubble in a row, it is lit up, and it "
             "changes color. Try it now. The chips above show only one color."},
    {"id": "saved", "title": "Saved searches", "path": "/candidates", "target": [".savedbar", ".tablebar"],
     "modes": (FULL, SHORT),
     "body": "Run a search or pick filters, then press Save this search and give it a name. It stays here, showing "
             "how many people match and how many are new since you last looked."},
    {"id": "person", "title": "A candidate's page", "path": "person", "target": ["main .profile-summary", "main h1"], "modes": (FULL, SHORT),
     "body": "The top of a person's page is their summary: who they are, their skills, how to reach them and when "
             "you last talked. Below it are their work history, the jobs they are in, and a timeline of calls and "
             "notes. Your AI app can add to the timeline too."},
    {"id": "pipeline", "title": "A job's pipeline", "path": "job", "target": ["#pipeline"], "modes": (FULL, SHORT),
     "body": "A job holds a pipeline of candidates in stages you can rename. To move a person along, use the menu "
             "on their card. You can also drag a card to another column. Jobs are listed under the Jobs tab."},
    {"id": "interviews", "title": "Interviews", "path": "/interviews",
     "target": ["main .sec-head", "#interviews-view", "main section.panel", "main h1"], "reach": "#interviews-view", "modes": (FULL, SHORT),
     "body": "Your month of interviews across every job, like a calendar. Switch to Week for loops, clashes and who owes feedback. Download a calendar file to add them to your own calendar."},
    {"id": "today-actions", "title": "Back to Today", "path": "/", "target": ["section.today", "main h1"],
     "modes": (FULL,),
     "body": "Today is where you will start each day. Each item has a button to act on it, and you choose what "
             "shows in Settings."},
    {"id": "finish", "title": "That is the tour", "path": None, "target": [], "modes": (FULL, SHORT),
     "action": "finish", "body": "You can replay the tour any time from the footer or Settings."},
]


def state(store: Store) -> dict[str, Any]:
    s = store.settings()
    try:
        step = max(0, int(s.get("tour_step") or 0))
    except ValueError:
        step = 0
    mode = s.get("tour_mode")
    return {"active": s.get("tour_active") == "1", "mode": mode if mode in MODES else FULL, "step": step,
            "loaded": s.get("tour_loaded") == "1", "seen": s.get("tour_seen") == "1"}


def has_own_data(store: Store) -> bool:
    """Real candidates or jobs of the user's own. Demo people and jobs don't count."""
    people, jobs = sample_ids(store)
    return (store.count_candidates() - len(people)) > 0 or (len(store.list_jobs()) - len(jobs)) > 0


def should_autostart(store: Store) -> bool:
    """First run only: nothing of the user's own yet, and no tour ever finished or skipped."""
    st = state(store)
    return not st["active"] and not st["seen"] and not has_own_data(store)


def start(store: Store, mode: str = FULL) -> dict[str, Any]:
    """Start a tour. One already running is left as it is, so the demo data it loaded stays accounted for."""
    st = state(store)
    if st["active"]:
        return st
    store.set_settings(tour_active="1", tour_mode=mode if mode in MODES else FULL, tour_step="0", tour_loaded="0",
                       tour_loaded_at="")
    return state(store)


def _pick_person(store: Store, mode: str) -> int | None:
    people, _ = sample_ids(store)
    recent = store.recent_candidates(60)
    own = [c["id"] for c in recent if c["id"] not in people]
    demo = [c["id"] for c in recent if c["id"] in people]
    if mode == FULL:
        maya = next((c["id"] for c in recent if c["id"] in people and c["name"] == "Maya Example"), None)
        return maya or (demo or own or [None])[0]
    return (own or demo or [None])[0]


def _pick_job(store: Store, mode: str) -> int | None:
    _, jobs = sample_ids(store)
    every = store.list_jobs("open") or store.list_jobs()
    own = [j["id"] for j in every if j["id"] not in jobs]
    demo = [j for j in every if j["id"] in jobs]
    if mode == FULL:
        pref = next((j["id"] for j in demo if j["title"] == "Senior Backend Engineer"), None)
        return pref or ([j["id"] for j in demo] or own or [None])[0]
    return (own or [j["id"] for j in demo] or [None])[0]


def demo_searches(store: Store) -> list[dict[str, Any]]:
    """Saved searches the recruiter made since the tour loaded the demo people: id and name. They match the demo
    people, so when those are cleared they would open on nothing."""
    since = store.setting("tour_loaded_at") or ""
    if not since or not state(store)["loaded"]:
        return []
    return [{"id": r["id"], "name": r["name"]} for r in store.list_saved_searches()
            if r["created_at"] >= since and r.get("created_by") != "ai"]


def worked_with(store: Store) -> list[dict[str, Any]]:
    """Demo people the recruiter has worked with (a note, an edit, a stage move): clearing the demo keeps them."""
    st = state(store)
    if not st["loaded"]:
        return []
    people, jobs = sample_ids(store)
    if not people:
        return []
    return [{"id": cid, "name": name} for cid, name in sorted(_touched_people(store, people, jobs).items())]


def steps_for(store: Store, mode: str, at: int | None = None) -> list[dict[str, Any]]:
    """The steps of this mode, each with its page resolved.

    The short tour skips a step that needs a person or a job when nobody has one. The full tour keeps every
    step until the demo question has been answered with nobody loaded and no data of their own, then it skips
    them too (`at` asks about a step other than the saved one).
    """
    people, jobs = sample_ids(store)
    present = bool(people or jobs)
    st = state(store)
    loaded = st["loaded"]
    person, job = _pick_person(store, mode), _pick_job(store, mode)
    nobody = store.count_candidates() == 0
    # Past the demo question with nobody loaded and no data of their own: the person and pipeline steps would
    # only show an empty page, so they are left out (as in the short tour).
    mode_ids = [x["id"] for x in STEPS if mode in x["modes"]]
    load_at = mode_ids.index("load") if "load" in mode_ids else None
    here = st["step"] if at is None else at
    declined = mode == FULL and load_at is not None and here > load_at and not present and nobody
    touched = worked_with(store) if mode == FULL and loaded else []
    out = []
    for s in STEPS:
        if mode not in s["modes"]:
            continue
        path = s["path"]
        if path == "person":
            if person is None and (mode == SHORT or declined):
                continue
            path = f"/candidates/{person}" if person else "/candidates"
        elif path == "job":
            if job is None and (mode == SHORT or declined):
                continue
            path = f"/jobs/{job}" if job else "/jobs"
        step = {"id": s["id"], "title": s["title"], "body": s["body"], "path": path, "target": list(s["target"]),
                "pulse": s.get("pulse", ""), "reach": s.get("reach", ""), "action": s.get("action", ""), "cta": ""}
        if s["id"] == "colors" and nobody:
            step["body"] = ("Give people a color to sort them your way: each row has a round bubble you click to "
                            "change its color. The chips above show only one color. Add a person and try it.")
            step["reach"] = step["pulse"] = ""
        if s["id"] == "load" and present:
            step["action"] = ""
            step["body"] = ("The demo people are loaded: made-up people and jobs, all named Example. The tour clears "
                            "them at the end." if loaded else
                            "Demo people are already here, because you loaded them yourself. The tour will leave "
                            "them alone, and you can remove them any time in Settings.")
        if s["id"] == "finish":
            if mode == FULL and loaded:
                body = ("That is the tour. Clearing the demo data removes the made-up people and jobs, and also any "
                        "demo person you only gave a color to. A backup is saved first.")
                if touched:
                    names = ", ".join(t["name"] for t in touched)
                    body += (f" You worked with {names}, so the tour keeps them "
                             "as your own unless you tick the box to delete.")
                step["body"] = body
                step["cta"] = "Clear the demo data and start"
            else:
                step["cta"] = "Finish the tour" if mode == FULL else "Done"
        out.append(step)
    return out


def payload(store: Store) -> dict[str, Any] | None:
    """What the browser needs to draw the tour, or None when no tour is running."""
    st = state(store)
    if not st["active"]:
        return None
    steps = steps_for(store, st["mode"])
    people, jobs = sample_ids(store)
    demo = bool(st["loaded"] and (people or jobs))
    return {"mode": st["mode"], "step": min(st["step"], len(steps) - 1), "steps": steps, "demo": demo,
            "extras": {"people": worked_with(store) if demo else [], "searches": demo_searches(store) if demo else []}}


def set_step(store: Store, step: int) -> int | None:
    """Remember the step the user is on. None when no tour is running."""
    st = state(store)
    if not st["active"]:
        return None
    n = max(0, min(step, len(steps_for(store, st["mode"], at=step)) - 1))
    store.set_settings(tour_step=str(n))
    return n


def load_demo(store: Store) -> int | None:
    """Full tour only: add the demo people, remember that the tour added them, and move to the next step.

    Demo data the user loaded themselves before the tour is never counted as the tour's, so the tour never
    clears it. Returns how many people were added, or None when this isn't a full tour.
    """
    st = state(store)
    if not st["active"] or st["mode"] != FULL:
        return None
    people, jobs = sample_ids(store)
    added = 0 if (people or jobs) else load_sample_data(store)
    steps = steps_for(store, FULL)
    at = next((i for i, s in enumerate(steps) if s["id"] == "load"), st["step"])
    loaded = st["loaded"] or bool(added)
    stamp = {"tour_loaded_at": now_iso()} if added else {}
    store.set_settings(tour_loaded="1" if loaded else "0", tour_step=str(min(at + 1, len(steps) - 1)), **stamp)
    return added


def end(store: Store, delete_people: set[int] | None = None,
        delete_searches: set[int] | None = None) -> dict[str, Any] | None:
    """Finish or skip: clear the demo people the tour loaded (full tour only), then close the tour for good.

    Never clears demo data the tour didn't load, and remove_sample_data keeps anything the user worked with.
    The recruiter may also ask to delete the demo people they worked with (delete_people) and the saved searches
    they made during the demo (delete_searches). Only ids the tour itself offered are honoured. The backup that
    remove_sample_data takes comes first, so a deleted person is in it.
    Returns remove_sample_data's result plus "deleted_people" and "deleted_searches" (names), or None when
    nothing was cleared. If the backup before clearing fails this raises ValueError and the tour stays open, so
    the user can try again.
    """
    st = state(store)
    removed = None
    if st["active"] and st["mode"] == FULL and st["loaded"]:
        offered_people = {w["id"]: w["name"] for w in worked_with(store)}
        offered_searches = {x["id"]: x["name"] for x in demo_searches(store)}
        removed = remove_sample_data(store)
        removed["deleted_people"] = []
        for cid in sorted((delete_people or set()) & set(offered_people)):
            try:
                store.delete_candidate(cid)
                removed["deleted_people"].append(offered_people[cid])
            except NotFound:
                pass
        removed["deleted_searches"] = list(removed.get("deleted_searches") or [])  # made on the demo people
        for sid in sorted((delete_searches or set()) & set(offered_searches)):
            try:
                store.delete_saved_search(sid)
                removed["deleted_searches"].append(offered_searches[sid])
            except NotFound:
                pass
    store.set_settings(tour_active="0", tour_loaded="0", tour_step="0", tour_seen="1", tour_loaded_at="")
    return removed


def reset(store: Store) -> None:
    """Forget that the tour ever ran, so the next first run starts it again. Leaves any demo data alone."""
    store.set_settings(tour_active="0", tour_mode="full", tour_step="0", tour_loaded="0", tour_seen="0",
                       tour_loaded_at="")
