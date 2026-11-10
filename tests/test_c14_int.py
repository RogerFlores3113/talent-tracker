"""Cycle 14 integration: the in-chat views and the tools match picks the way the app does (title variants fold,
merged skills count as one)."""
from __future__ import annotations

import re
from pathlib import Path

import anyio

from talent import picks
from talent.mcp_server import build_server

KIT = Path(__file__).resolve().parent.parent / "src" / "talent" / "static" / "mcp_views" / "kit.js"


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go).structuredContent


def test_tools_fold_titles_and_count_merged_skills(store):
    a = store.add_candidate(name="Ana Ruiz", tags="postgres", current_title="Sr. Data Engineer II")["id"]
    store.add_candidate(name="Ben Cho", tags="python", current_title="Backend Developer")
    store.add_skill_alias("postgres", "postgresql")
    s = build_server(store)
    d = call(s, "search_candidates", query="", highlight=["python", "title:Data Engineer", "postgresql"])
    assert d["results"][0]["id"] == a  # the title variant and the merged skill both count: 2 of 3 beats 1 of 3
    assert d["skill_aliases"] == {"postgres": "postgresql"}
    d = call(s, "search_candidates", query="", highlight=["postgres"])  # a pick by the alias is the kept name
    assert d["highlight"][0]["name"] == "postgresql" and d["results"][0]["id"] == a
    c = call(s, "compare_candidates", candidate_ids=[a, a + 1], highlight=["postgres"])
    assert c["skill_aliases"] == {"postgres": "postgresql"} and c["highlight"][0]["name"] == "postgresql"


def test_no_aliases_sends_none(store):
    store.add_candidate(name="Ana Ruiz", tags="python")
    assert "skill_aliases" not in call(build_server(store), "search_candidates", query="")


def test_view_title_folding_uses_the_same_level_words():
    js = KIT.read_text(encoding="utf-8")
    words = set(re.search(r"const LEVEL_WORDS=new Set\(\[([^\]]*)\]\)", js).group(1).replace('"', "").split(","))
    assert words == set(picks.LEVEL_WORDS)
    assert "function foldTitle(" in js and "ALIASES=(d&&d.skill_aliases" in js
