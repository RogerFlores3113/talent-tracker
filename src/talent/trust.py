"""How far each AI app is trusted.

Every AI app that connects says its name in its hello. The first time one is seen it is listed here (table
ai_apps) with a trust level the recruiter can change in Settings:

- read_only: it can search and read, and every write is refused.
- suggest: every write it makes waits on Review as a request (kind "tool_call"); Keep runs the same tool call once.
- trusted: today's rules (Cautious mode, the hourly limits, Undo).

New apps start as trusted, unless Settings says new apps start at Suggest only. The name is the app's own claim,
so it is shown as what the app calls itself. Nothing here is an MCP tool: the AI app can't change its own level."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from contextvars import ContextVar
from typing import Any

from . import review
from .store import AI_KEPT, NotFound, Store, now_iso, quoted

LEVELS = ("read_only", "suggest", "trusted")
LABELS = {"read_only": "Read only", "suggest": "Suggest only", "trusted": "Trusted"}
EXPLAIN = {"read_only": "Can search and read. Every write is refused.",
           "suggest": "Every write waits on Review until you keep it.",
           "trusted": "Writes follow today's rules: Cautious mode, the hourly limits and Undo."}
NEW_KEY = "ai_new_apps_trust"  # settings row: "suggest" when new apps start at Suggest only
UNNAMED = "Unnamed app"
LEGACY_UNNAMED = "(no name given)"
RANK = {"read_only": 0, "suggest": 1, "trusted": 2}
SHOW_CHARS = 200  # held arguments longer than this fold behind Show all on Review
WRITES_PREFIX = "ai_app_writes:"  # settings rows: how many writes each app has made
# While a kept request runs: skip the trust check, and Cautious mode (the recruiter just said yes).
REPLAY: ContextVar[bool] = ContextVar("talent_trust_replay", default=False)
REPLAY_SESSION: ContextVar[str | None] = ContextVar("talent_trust_replay_session", default=None)
# Tools the recruiter's presses on the Shortlist card use (they carry their own key), never held here. A Read only
# app is refused them, and so is a Suggest only app (it could take its own key and keep its own proposals).
CARD_TOOLS = frozenset({"keep_prospect", "pass_prospect", "keep_prospects", "pass_prospects", "shortlist_card_key"})
# The one write a Suggest only app may still make: it puts people on the Shortlist, where the recruiter decides.
SUGGEST_OK = frozenset({"propose_candidates"})
READ_ONLY_TEXT = ("No change made. The recruiter has set this AI app to read only in Talent Search, so it can search "
                  "and read but not change anything. Tell them what you wanted to change and let them do it.")
SUGGEST_CARD_TEXT = ("No change made. This AI app is set to Suggest only in Talent Search, so it can't use Keep or Pass. "
                     "The recruiter keeps or passes people on the Shortlist page, or can set this app to Trusted in "
                     "Settings > AI apps.")
HOLD_CAP_CHARS = 20_000  # the most a held request may carry, as JSON
SUGGEST_WAIT = ("Nothing changed yet: this app is set to Suggest only, so the recruiter keeps or passes it in the app "
                "(Review). Don't send it again.")
_seen: dict[str, float] = {}
_LOCK = threading.Lock()


def new_apps_level(store: Store) -> str:
    """The switch on Settings: do new apps start at Suggest only."""
    return "suggest" if store.setting(NEW_KEY) == "suggest" else "trusted"


def effective_new_level(store: Store) -> str:
    """What a name not seen before starts at: Suggest only when the switch is on, or when any saved app is set
    below Trusted (so restricting one app makes unknown newcomers wait)."""
    if new_apps_level(store) == "suggest":
        return "suggest"
    return "suggest" if store._row("SELECT 1 AS n FROM ai_apps WHERE trust != 'trusted' LIMIT 1") else "trusted"


def set_new_apps(store: Store, level: str) -> None:
    with store._write():
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                            " SET value = excluded.value", (NEW_KEY, "suggest" if level == "suggest" else "trusted"))


def _name(name: str | None) -> str:
    return " ".join(str(name or "").split())[:60] or UNNAMED


def _find(store: Store, name: str) -> dict[str, Any] | None:
    """The saved row for this name, ignoring case and spacing (an unnamed app is one row)."""
    names = [name.casefold()] + ([LEGACY_UNNAMED] if name == UNNAMED else [])
    for r in store._rows("SELECT * FROM ai_apps ORDER BY id"):
        if " ".join(r["name"].split()).casefold() in names:
            return r
    return None


def _stricter(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return a if RANK.get(a["trust"], 2) <= RANK.get(b["trust"], 2) else b


def touch(store: Store, name: str | None, bound: dict[str, Any] | None = None) -> dict[str, Any]:
    """Note that this AI app was seen now (listing it the first time) and return its row. Last seen is written at
    most once a minute per app. bound is one MCP process's memory: the first app name it sees binds, and a later
    different name in the same process keeps the stricter of the two levels (renaming doesn't escape a level)."""
    name = _name(name)
    row = _find(store, name)
    first = None
    if bound is not None and bound.get("id") is not None:
        first = store._row("SELECT * FROM ai_apps WHERE id = ?", (bound["id"],))
        if first is not None and row is not None and row["id"] == first["id"]:
            first = None
    if first is not None:  # a different name than the one this process started with
        return _stricter(first, row) if row is not None else first
    now = time.monotonic()
    if row is None:
        with store._write():
            store._conn.execute("INSERT OR IGNORE INTO ai_apps(name, first_seen, last_seen, trust) VALUES (?, ?, ?, ?)",
                                (name, now_iso(), now_iso(), effective_new_level(store)))
        row = _find(store, name)
        with _LOCK:
            _seen[f"{id(store)}:{name}"] = now
    else:
        key = f"{id(store)}:{name}"
        with _LOCK:
            fresh = now - _seen.get(key, -1e9) < 60
            if not fresh:
                _seen[key] = now
        if not fresh:
            with store._write():
                store._conn.execute("UPDATE ai_apps SET last_seen = ? WHERE id = ?", (now_iso(), row["id"]))
            row = store._row("SELECT * FROM ai_apps WHERE id = ?", (row["id"],))
    if bound is not None and row is not None:
        bound["id"] = row["id"]
    return row


def count_write(store: Store, app_id: int) -> None:
    """One more write made by this app (a held one counts too), for the count on Settings."""
    key = f"{WRITES_PREFIX}{app_id}"
    with store._write():
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE"
                            " SET value = CAST(value AS INTEGER) + 1", (key,))


def apps(store: Store) -> list[dict[str, Any]]:
    """The AI apps seen so far, most recently seen first, each with a label and what its level means."""
    out = []
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    for r in store._rows("SELECT * FROM ai_apps ORDER BY last_seen DESC, id DESC"):
        w = store.setting(f"{WRITES_PREFIX}{r['id']}")
        out.append({**r, "label": LABELS.get(r["trust"], r["trust"]), "explain": EXPLAIN.get(r["trust"], ""),
                    "writes": int(w) if str(w or "").isdigit() else 0, "is_new": str(r["first_seen"]) >= day_ago})
    return out


def set_trust(store: Store, app_id: int, level: str) -> dict[str, Any]:
    if level not in LEVELS:
        raise ValueError("Pick Read only, Suggest only or Trusted.")
    row = store._row("SELECT * FROM ai_apps WHERE id = ?", (app_id,))
    if row is None:
        raise NotFound("That AI app isn't listed any more. Reload the page.")
    with store._write():
        store._conn.execute("UPDATE ai_apps SET trust = ? WHERE id = ?", (level, app_id))
    return {**row, "trust": level}


def wait_note(store: Store) -> str:
    """Why a Suggest only request is waiting: the app's level, plus Cautious mode only when that is on too."""
    return review.WAIT_NOTE if review.cautious(store) else SUGGEST_WAIT


def size_problem(args: dict[str, Any]) -> str | None:
    """A plain sentence when a held request is too big to put on Review, else None."""
    try:
        size = len(json.dumps(args, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return "No change made. That request could not be read."
    if size > HOLD_CAP_CHARS:
        return (f"No change made. That request is too long for the recruiter to review ({size:,} characters; the limit "
                f"is {HOLD_CAP_CHARS:,}). Send less text, or ask the recruiter to add it in the app.")
    return None


def _summary(tool: str, args: dict[str, Any]) -> str:
    bits = []
    for k, v in list(args.items())[:3]:
        if v in (None, "", [], {}):
            continue
        text = " ".join(str(v).split()) if not isinstance(v, (list, dict)) else f"{len(v)} item{'' if len(v) == 1 else 's'}"
        bits.append(f"{k.replace('_', ' ')} {quoted(text[:60]) if isinstance(v, str) else text[:60]}")
    return f"{tool.replace('_', ' ').capitalize()}" + (f" ({', '.join(bits)})" if bits else "")


def hold(store: Store, tool: str, args: dict[str, Any], session: str | None, app: str | None = None) -> dict[str, Any]:
    """Queue one write from a Suggest only app. Returns the Review row. ValueError when it is too big."""
    if (bad := size_problem(args)):
        raise ValueError(bad)
    cid = args.get("candidate_id") if isinstance(args.get("candidate_id"), int) else None
    jid = args.get("job_id") if isinstance(args.get("job_id"), int) else None
    payload = {"tool": tool, "args": args, **({"app": app} if app else {})}
    if tool == "set_candidate_color" and cid is not None:
        _replace_older_color_holds(store, cid, args, app)
    try:
        return review._queue(store, "tool_call", _summary(tool, args), payload, candidate_id=cid, job_id=jid,
                             session=session,
                             app=app or "")
    except NotFound:  # a missing person or job: hold it without the link, Keep will say it is gone
        return review._queue(store, "tool_call", _summary(tool, args), payload, session=session, app=app or "")


def _replace_older_color_holds(store: Store, cid: int, args: dict[str, Any], app: str | None) -> None:
    """A newer color for the same person replaces this app's older waiting one, so Review never stacks two."""
    with store._write():
        for r in store._rows("SELECT id, payload FROM ai_review WHERE status = 'pending' AND kind = 'tool_call'"
                             " AND candidate_id = ?", (cid,)):
            try:
                old = json.loads(r["payload"] or "{}")
            except ValueError:
                continue
            if old.get("tool") != "set_candidate_color" or (old.get("app") or "") != (app or ""):
                continue
            if old.get("args") == args:
                continue  # the same request again is deduplicated by the queue itself
            store._conn.execute("UPDATE ai_review SET status = 'passed', decided_at = ?, result = ? WHERE id = ?",
                                (now_iso(), "Replaced by a newer color for the same person", r["id"]))


def lines(r: dict[str, Any]) -> list[dict[str, str]]:
    """One line per held argument. A long value shows its first SHOW_CHARS characters, "and N more characters",
    and the whole text in "full" (Review folds it behind Show all)."""
    out = []
    for k, v in (r["payload"].get("args") or {}).items():
        if v in (None, ""):
            continue
        full = (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else " ".join(str(v).split()))
        line = {"label": str(k).replace("_", " "), "old": "", "new": full or "(empty)"}
        if len(full) > SHOW_CHARS:
            line.update({"new": full[:SHOW_CHARS], "more": len(full) - SHOW_CHARS, "full": full})
        out.append(line)
    return out


def replay(store: Store, r: dict[str, Any]) -> str:
    """Keep: run the held tool call once, as the recruiter's choice, in its own AI session. ValueError if it can't run."""
    import asyncio

    from .mcp_server import build_server
    tool, args = r["payload"].get("tool"), r["payload"].get("args") or {}
    out: dict[str, Any] = {}

    def run() -> None:
        token, stoken, ktoken = REPLAY.set(True), REPLAY_SESSION.set(r.get("session")), AI_KEPT.set(True)
        try:
            server = build_server(store)
            res = asyncio.run(server.call_tool(tool, args))
            res = res[0] if isinstance(res, tuple) else res
            content = res if isinstance(res, list) else getattr(res, "content", None) or []
            out["text"] = " ".join(getattr(c, "text", "") for c in content).strip().splitlines()[0:1]
        except Exception as exc:  # the tool's own plain message
            out["error"] = str(exc)
        finally:
            REPLAY.reset(token)
            REPLAY_SESSION.reset(stoken)
            AI_KEPT.reset(ktoken)

    t = threading.Thread(target=run)
    t.start()
    t.join()
    if "error" in out:
        raise ValueError(out["error"].replace("Error executing tool " + str(tool) + ": ", ""))
    return r["summary"] + (f". {out['text'][0]}" if out.get("text") else "")
