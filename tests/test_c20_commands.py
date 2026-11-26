"""Starter slash commands: shipped in the Claude plugin and offered as MCP prompts."""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import anyio
import pytest

from talent import skills
from talent.mcp_server import build_server

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TALENT_SKILLS_HOME", str(home))
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(dl))


def test_plugin_and_marketplace_files_match_the_code():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert plugin["name"] == skills.PLUGIN_NAME
    assert plugin == skills.PLUGIN_MANIFEST
    assert market["name"] == skills.MARKETPLACE_NAME
    assert any(p["name"] == skills.PLUGIN_NAME and p["source"] == "./" for p in market["plugins"])


def test_starter_commands_are_well_formed():
    cmds = skills.starter_commands()
    names = [c["name"] for c in cmds]
    assert names == sorted(names) and len(cmds) >= 6
    assert {"find-talent", "match-job"} <= set(names)
    for c in cmds:
        raw = (skills.commands_dir() / f"{c['name']}.md").read_text(encoding="utf-8")
        assert raw.startswith("---")
        assert c["description"] and c["argument_hint"] and "$ARGUMENTS" in c["body"]
        assert re.fullmatch(r"[a-z]+(-[a-z]+)*", c["name"])
        assert "—" not in raw and "–" not in raw


def test_commands_only_name_real_tools_and_playbooks(store):
    server = build_server(store)
    listed = anyio.run(server.list_tools)
    tools = {t.name for t in listed}
    params = {k for t in listed for k in t.inputSchema.get("properties", {})}  # `days_ahead` is an argument, not a tool
    books = {b["name"] for b in skills.playbooks()}
    for c in skills.starter_commands():
        for name in re.findall(r"`([a-z_]+)`", c["body"]):
            assert name in tools | params, f"{c['name']} names unknown tool {name}"
        for name in re.findall(r"get_playbook`\s+with\s+`([^`]+)`", c["body"]):
            assert name in books, f"{c['name']} names unknown playbook {name}"


def _prompt_text(server, name, args):
    res = anyio.run(server.get_prompt, name, args)
    return "\n".join(m.content.text for m in res.messages)


def test_each_command_is_an_mcp_prompt_with_an_optional_request(store):
    server = build_server(store)
    prompts = {p.name: p for p in anyio.run(server.list_prompts)}
    playbook_prompts = {b["name"].replace("-", "_") for b in skills.playbooks()}
    for c in skills.starter_commands():
        pname = c["name"].replace("-", "_")
        if pname in playbook_prompts:  # the playbook prompt of the same name wins; the guard skips the command
            assert pname in prompts
            continue
        p = prompts[pname]
        assert p.description == c["description"]
        assert [(a.name, a.required) for a in p.arguments] == [("request", False)]
    text = _prompt_text(server, "find_talent", {"request": "staff data engineer"})
    assert "staff data engineer" in text and "$ARGUMENTS" not in text
    empty = _prompt_text(server, "find_talent", {})
    assert "(nothing given)" in empty and "$ARGUMENTS" not in empty


def test_the_plugin_zip_carries_the_commands_and_the_skills():
    z = zipfile.ZipFile(io.BytesIO(skills.build_plugin()))
    names = set(z.namelist())
    assert ".claude-plugin/plugin.json" in names
    for c in skills.starter_commands():
        assert f"commands/{c['name']}.md" in names
    assert {"commands/find-talent.md", "commands/match-job.md"} <= names
    assert any(n.startswith("skills/talent-search-") for n in names)
    assert skills.build_plugin() == skills.build_plugin()
