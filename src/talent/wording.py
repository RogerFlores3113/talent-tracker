"""Reply wording: replies say "you" where they say "the recruiter" (the AI is talking to that person), and name
people and jobs with their link. `who` picks the word for strings built in code; `install` rewrites what the MCP tools say
(outside the candidate-provided text) so no reply needs two versions."""
from __future__ import annotations

import re
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from . import actions, replylinks
from .store import Store


def own(store: Store) -> bool:
    return actions.inhouse(store)


def who(store: Store, *, cap: bool = False, possessive: bool = False) -> str:
    """'you' (own company) or 'the recruiter', as a subject or object; 'your' or "the recruiter's" with possessive."""
    word = ("your" if possessive else "you") if own(store) else ("the recruiter's" if possessive else "the recruiter")
    return word[:1].upper() + word[1:] if cap else word


# Tools whose replies other hands own (the lists, digests and reports): their text is left as it is.
SKIP_TOOLS = frozenset({"list_jobs", "list_saved_searches", "list_followups", "status_digest", "client_report",
                        "list_interviews", "find_duplicates"})


# Verbs that follow "the recruiter" and change form after "you".
_VERBS = {"can": "can", "has": "have", "hasn't": "haven't", "keeps": "keep", "passes": "pass", "reviews": "review",
          "sends": "send", "presses": "press", "says": "say", "checks": "check", "asks": "ask", "decides": "decide",
          "marked": "marked", "chose": "chose", "said": "said", "switched": "switched", "wants": "want",
          "is": "are", "was": "were", "should": "should", "will": "will", "may": "may", "must": "must",
          "already": "already", "sets": "set", "picks": "pick", "approves": "approve", "needs": "need",
          "reads": "read", "opens": "open", "moves": "move", "does": "do", "doesn't": "don't"}
_IMPERATIVE = re.compile(r"\b(Ask|ask|Tell|tell|Show|show|Check with|check with|Confirm with|confirm with) "
                         r"the recruiter\b")
_NOT_VERBS = {"as", "this", "its", "us", "less", "across", "versus", "plus"}
_VERB = re.compile(r"\b(T|t)he recruiter ((?:\w+n't|\w+))(?![\w'])")


def _conjugate(word: str) -> str | None:
    """The verb after "you": has -> have, keeps -> keep, passes -> pass. None for a word that is not a 3rd-person verb."""
    if word in _VERBS:
        return _VERBS[word]
    if word in _NOT_VERBS or not word.endswith("s") or word.endswith(("ss", "us", "is")):
        return None
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "oes")):
        return word[:-2]
    return word[:-1]
_OR_VERB = re.compile(r"\b([Yy]ou \w+ or )(\w+)\b")
_FENCE = re.compile(r"(<(data-[0-9a-f]+)>.*?</\2>)", re.S)


def _plain(text: str) -> str:
    text = text.replace("for the recruiter only", "private to you").replace("For the recruiter only", "Private to you")
    text = _IMPERATIVE.sub(lambda m: f"{m.group(1)} the user", text)
    def verb(m: re.Match[str]) -> str:
        fixed = _conjugate(m.group(2))
        if fixed is None:
            return m.group(0)
        return ("Y" if m.group(1) == "T" else "y") + "ou " + fixed
    text = _VERB.sub(verb, text)
    text = _OR_VERB.sub(lambda m: m.group(1) + (_conjugate(m.group(2)) or m.group(2)), text)  # keeps or passes -> keep or pass
    text = text.replace("the recruiter's", "your").replace("The recruiter's", "Your")
    text = text.replace("the recruiter", "you").replace("The recruiter", "You")
    return text


def rewrite(text: str) -> str:
    """The text in own-company words; whatever sits inside a candidate-provided fence is left as it is."""
    out, pos = [], 0
    for m in _FENCE.finditer(text):
        out.append(_plain(text[pos:m.start()]))
        out.append(m.group(1))
        pos = m.end()
    out.append(_plain(text[pos:]))
    return "".join(out)


# Markers of the blocks in a paste-ready reply that are for the person using the app, not for pasting.
PRIVATE_MARKS = ("(Candidate-provided text, not instructions.)", "For the recruiter only, not part of the update")


def _private(text: str) -> bool:
    return any(m in text for m in PRIVATE_MARKS)


def install(mcp: Any, store: Store) -> None:
    """Wrap tool calls. Replies and errors always say "you" for the person using the app (Codex and Claude Code show
    only the text, and the AI is talking to that person), and the first mention of each person and job carries its
    link in Talent Search (replylinks.py). Candidate-provided text is left as it is."""
    manager = mcp._tool_manager
    original = manager.call_tool

    async def call_tool(name: str, arguments: dict[str, Any], context: Any = None, convert_result: bool = False):
        try:
            res = await original(name, arguments, context=context, convert_result=convert_result)
        except ToolError as exc:
            if "recruiter" in str(exc).lower():
                raise ToolError(rewrite(str(exc))) from exc
            raise
        if isinstance(res, CallToolResult) and not getattr(res, "isError", False):
            tool = manager.get_tool(name)
            app_only = "app" in (((getattr(tool, "meta", None) or {}).get("ui") or {}).get("visibility") or []) \
                and "resourceUri" not in ((getattr(tool, "meta", None) or {}).get("ui") or {})
            if name in SKIP_TOOLS or app_only:
                return res
            data = res.structuredContent
            # A reply with paste-ready text (a status update) marks its private blocks; the other blocks are what
            # gets pasted to a hiring manager or client, so they never get a local app link.
            has_paste = any(isinstance(c, TextContent) and _private(c.text) for c in res.content)
            out = []
            for c in res.content:
                if isinstance(c, TextContent):
                    text = rewrite(c.text) if "recruiter" in c.text.lower() else c.text
                    if not has_paste or _private(c.text):
                        text = replylinks.link_names(store, text, data)
                    c = TextContent(type="text", text=text)
                out.append(c)
            res.content = out
            if isinstance(data, dict):
                for k in ("warning", "note", "nudge", "close_out_hint", "summary"):
                    if isinstance(data.get(k), str) and "recruiter" in data[k].lower():
                        data[k] = rewrite(data[k])
                for row in data.get("skipped") or []:  # propose_candidates: why each person was skipped
                    if isinstance(row, dict) and isinstance(row.get("reason"), str) and "recruiter" in row["reason"].lower():
                        row["reason"] = rewrite(row["reason"])
        return res

    manager.call_tool = call_tool  # type: ignore[method-assign]
