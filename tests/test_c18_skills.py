"""Skills come with the connection: the MCP server serves them as playbooks, so there is nothing to add or upload.
Skill files for Claude Code and Codex are an optional extra that the app keeps up to date (cycle 19)."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from talent import skills
from talent.mcp_server import build_server

from conftest import local_client

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TALENT_SKILLS_HOME", str(home))
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(dl))
    return {"home": home, "dl": dl}


def _add(store):
    """The result text only: the out-of-band refresh of the panel's own 'found' list is cut off."""
    return local_client(store).post("/connect/skills/add").text.split('<ul class="plain" id="skills-found"', 1)[0]


def test_every_skill_is_a_playbook_with_its_when_to_use_line():
    books = skills.playbooks()
    assert [b["name"] for b in books] == [n.removeprefix(skills.PREFIX) for n in skills.skill_names()]
    assert len(books) == 6 and all(b["description"] and b["title"] and b["body"].startswith("# ") for b in books)
    assert all("---" not in b["body"].split("\n", 1)[0] for b in books)  # the frontmatter is not in the text


def test_the_connection_lists_the_playbooks_and_serves_each_one(store):
    server = build_server(store)
    assert "call get_playbook" in server.instructions
    assert all(f"  - {b['name']}: " in server.instructions for b in skills.playbooks())
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert "write-outreach" in tools["get_playbook"].description and tools["get_playbook"].annotations.readOnlyHint
    r = anyio.run(server.call_tool, "get_playbook", {"name": "talent-search-write-outreach"})
    assert r.content[0].text.startswith("# Write outreach")
    prompts = {p.name for p in anyio.run(server.list_prompts)}
    assert {"write_outreach", "schedule_interviews"} <= prompts


def test_an_unknown_playbook_names_the_real_ones(store):
    server = build_server(store)
    with pytest.raises(Exception) as err:
        anyio.run(server.call_tool, "get_playbook", {"name": "make-coffee"})
    assert "pipeline-review" in str(err.value)


def test_no_app_found_says_the_connected_app_already_has_them(store):
    t = _add(store)
    assert "nowhere to add skill files" in t and "already has the skills" in t
    assert "plugin" not in t.lower() and "Done." not in t


def test_only_found_apps_are_listed(store, env):
    (env["home"] / ".claude").mkdir()
    t = _add(store)
    assert "Claude Code</strong>: added 6 skills" in t and "Codex" not in t
    assert "Already up to date." in _add(store)


def test_starting_the_app_brings_installed_skill_files_up_to_date(env):
    (env["home"] / ".claude").mkdir()
    skills.install("claude-code")
    base = skills.target_dir("claude-code")
    missing = base / "talent-search-write-outreach"
    for f in missing.iterdir():  # an older version had one skill fewer
        f.unlink()
    missing.rmdir()
    edited = base / "talent-search-pipeline-review" / "SKILL.md"
    edited.write_text("my own version\n", encoding="utf-8")
    assert skills.refresh_installed() == ["Claude Code"]
    assert (missing / "SKILL.md").is_file() and edited.read_text(encoding="utf-8") == "my own version\n"
    assert skills.refresh_installed() == []  # nothing left to do
    assert not skills.target_dir("codex").exists()  # an app it never installed into is left alone


def test_no_page_or_script_tells_people_to_upload_a_plugin(store):
    page = local_client(store).get("/connect").text
    assert "Upload plugin" not in page and "Customize" not in page and "Add marketplace" not in page
    assert "Skills come with the connection" in page and "nothing to add or upload" in page
    for name in ("install.sh", "install.ps1", "INSTALL.md", "README.md", "skills/INSTALL-SKILLS.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "Upload plugin" not in text and "Add skills" not in text, name


def test_installer_closing_text_mentions_the_restart():
    for name in ("install.sh", "install.ps1", "INSTALL.md"):
        assert "quits and reopens Claude Desktop" in (ROOT / name).read_text(encoding="utf-8"), name


def test_results_are_announced_once(store):
    t = _add(store)
    assert "aria-live" not in t and t.count('role="status"') == 1


def test_no_dashes_in_new_text(store):
    for t in (local_client(store).get("/connect").text, _add(store)):
        assert "—" not in t and "–" not in t


def test_found_list_collapses_missing_apps_into_one_quiet_line(store, env):
    c = local_client(store)
    for t in (c.get("/connect").text, c.post("/connect/skills/add").text):
        assert "Not found" not in t and "found." not in t
        assert t.count('id="skills-not-here"') == 1
        assert "Not on this computer: Claude Code, Codex." in t
        assert 'aria-label="Install the skills for Codex anyway"' in t
    (env["home"] / ".claude").mkdir()
    t = c.get("/connect").text
    assert "Not on this computer: Codex." in t and "Claude Code</strong>:" in t
