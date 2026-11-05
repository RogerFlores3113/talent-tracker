"""How far each AI app is trusted.

Every AI app that connects says its name in its hello. The first time one is seen it is listed here (table
ai_apps) with a trust level the recruiter can change in Settings:

- read_only: it can search and read, and every write is refused.
- suggest: every write it makes waits on Review as a request (kind "tool_call"); Keep runs the same tool call once.
- trusted: today's rules (Cautious mode, the hourly limits, Undo).

New apps start as trusted, unless Settings says new apps start at Suggest only. The name is the app's own claim,
so it is shown as what the app calls itself. Nothing here is an MCP tool: the AI app can't change its own level."""
from __future__ import annotations

import threading
import time
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
UNNAMED = "(no name given)"
# While a kept request runs: skip the trust check, and Cautious mode (the recruiter just said yes).
REPLAY: ContextVar[bool] = ContextVar("talent_trust_replay", default=False)
REPLAY_SESSION: ContextVar[str | None] = ContextVar("talent_trust_replay_session", default=None)
# Tools the recruiter's presses on the Shortlist card use (they carry their own key), never held here.
CARD_TOOLS = frozenset({"keep_prospect", "pass_prospect", "keep_prospects", "pass_prospects", "shortlist_card_key"})
# The one write a Suggest only app may still make: it puts people on the Shortlist, where the recruiter decides.
SUGGEST_OK = frozenset({"propose_candidates"})
READ_ONLY_TEXT = ("No change made. The recruiter has set this AI app to read only in Talent Search, so it can search "
                  "and read but not change anything. Tell them what you wanted to change and let them do it.")
_seen: dict[str, float] = {}
_LOCK = threading.Lock()


def new_apps_level(store: Store) -> str:
    return "suggest" if store.setting(NEW_KEY) == "suggest" else "trusted"


def set_new_apps(store: Store, level: str) -> None:
    with store._write():
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                            " SET value = excluded.value", (NEW_KEY, "suggest" if level == "suggest" else "trusted"))


def _name(name: str | None) -> str:
    return " ".join(str(name or "").split())[:60] or UNNAMED


def touch(store: Store, name: str | None) -> dict[str, Any]:
    """Note that this AI app was seen now (listing it the first time) and return its row. Last seen is written at
    most once a minute per app."""
    name = _name(name)
    row = store._row("SELECT * FROM ai_apps WHERE name = ?", (name,))
    now = time.monotonic()
    if row is None:
        with store._write():
            store._conn.execute("INSERT OR IGNORE INTO ai_apps(name, first_seen, last_seen, trust) VALUES (?, ?, ?, ?)",
                                (name, now_iso(), now_iso(), new_apps_level(store)))
        row = store._row("SELECT * FROM ai_apps WHERE name = ?", (name,))
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
    return row


def apps(store: Store) -> list[dict[str, Any]]:
    """The AI apps seen so far, most recently seen first, each with a label and what its level means."""
    out = []
    for r in store._rows("SELECT * FROM ai_apps ORDER BY last_seen DESC, id DESC"):
        out.append({**r, "label": LABELS.get(r["trust"], r["trust"]), "explain": EXPLAIN.get(r["trust"], "")})
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


def _summary(tool: str, args: dict[str, Any]) -> str:
    bits = []
    for k, v in list(args.items())[:3]:
        if v in (None, "", [], {}):
            continue
        text = " ".join(str(v).split()) if not isinstance(v, (list, dict)) else f"{len(v)} item{'' if len(v) == 1 else 's'}"
        bits.append(f"{k.replace('_', ' ')} {quoted(text[:60]) if isinstance(v, str) else text[:60]}")
    return f"{tool.replace('_', ' ').capitalize()}" + (f" ({', '.join(bits)})" if bits else "")


def hold(store: Store, tool: str, args: dict[str, Any], session: str | None) -> dict[str, Any]:
    """Queue one write from a Suggest only app. Returns the Review row."""
    cid = args.get("candidate_id") if isinstance(args.get("candidate_id"), int) else None
    jid = args.get("job_id") if isinstance(args.get("job_id"), int) else None
    payload = {"tool": tool, "args": args}
    try:
        return review._queue(store, "tool_call", _summary(tool, args), payload, candidate_id=cid, job_id=jid,
                             session=session)
    except NotFound:  # a missing person or job: hold it without the link, Keep will say it is gone
        return review._queue(store, "tool_call", _summary(tool, args), payload, session=session)


def lines(r: dict[str, Any]) -> list[dict[str, str]]:
    return [{"label": str(k).replace("_", " "), "old": "", "new": review._short(v) or "(empty)"}
            for k, v in (r["payload"].get("args") or {}).items() if v not in (None, "")]


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
