"""Own-company wording: when Settings says the person hires for their own company, replies say "you" where they
say "the recruiter". `who` picks the word for strings built in code; `install` rewrites what the MCP tools say
(outside the candidate-provided text) so no reply needs two versions."""
from __future__ import annotations

import re
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from . import actions
from .store import Store


def own(store: Store) -> bool:
    return actions.inhouse(store)


def who(store: Store, *, cap: bool = False, possessive: bool = False) -> str:
    """'you' (own company) or 'the recruiter', as a subject or object; 'your' or "the recruiter's" with possessive."""
    word = ("your" if possessive else "you") if own(store) else ("the recruiter's" if possessive else "the recruiter")
    return word[:1].upper() + word[1:] if cap else word


# Verbs that follow "the recruiter" and change form after "you".
_VERBS = {"can": "can", "has": "have", "hasn't": "haven't", "keeps": "keep", "passes": "pass", "reviews": "review",
          "sends": "send", "presses": "press", "says": "say", "checks": "check", "asks": "ask", "decides": "decide",
          "marked": "marked", "chose": "chose", "said": "said", "switched": "switched", "wants": "want",
          "is": "are", "was": "were", "should": "should", "will": "will", "may": "may", "must": "must",
          "already": "already", "sets": "set", "picks": "pick", "approves": "approve", "needs": "need",
          "reads": "read", "opens": "open", "moves": "move", "does": "do", "doesn't": "don't"}
_IMPERATIVE = re.compile(r"\b(Ask|ask|Tell|tell|Show|show|Check with|check with|Confirm with|confirm with) "
                         r"the recruiter\b")
_VERB = re.compile(r"\b(T|t)he recruiter (" + "|".join(sorted(map(re.escape, _VERBS), key=len, reverse=True)) + r")\b")
_FENCE = re.compile(r"(<(data-[0-9a-f]+)>.*?</\2>)", re.S)


def _plain(text: str) -> str:
    text = text.replace("for the recruiter only", "private to you").replace("For the recruiter only", "Private to you")
    text = _IMPERATIVE.sub(lambda m: f"{m.group(1)} the user", text)
    text = _VERB.sub(lambda m: ("Y" if m.group(1) == "T" else "y") + "ou " + _VERBS[m.group(2)], text)
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


def install(mcp: Any, store: Store) -> None:
    """Wrap tool calls: with Settings on own company, replies and errors are rewritten with `rewrite`."""
    manager = mcp._tool_manager
    original = manager.call_tool

    async def call_tool(name: str, arguments: dict[str, Any], context: Any = None, convert_result: bool = False):
        try:
            res = await original(name, arguments, context=context, convert_result=convert_result)
        except ToolError as exc:
            if own(store) and "recruiter" in str(exc).lower():
                raise ToolError(rewrite(str(exc))) from exc
            raise
        if isinstance(res, CallToolResult) and own(store):
            res.content = [TextContent(type="text", text=rewrite(c.text)) if isinstance(c, TextContent)
                           and "recruiter" in c.text.lower() else c for c in res.content]
            data = res.structuredContent
            if isinstance(data, dict):
                for k in ("warning", "note", "nudge", "close_out_hint", "summary"):
                    if isinstance(data.get(k), str) and "recruiter" in data[k].lower():
                        data[k] = rewrite(data[k])
        return res

    manager.call_tool = call_tool  # type: ignore[method-assign]
