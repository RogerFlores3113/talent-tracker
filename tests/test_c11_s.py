"""One-click skills install: the Claude plugin file, app detection, the combined install, the Connect page."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from talent import skills
from talent.cli import main

from conftest import LOCAL_URL, local_client

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("TALENT_SKILLS_HOME", str(h))
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(dl))
    return h


def test_plugin_zip_layout():
    z = zipfile.ZipFile(io.BytesIO(skills.build_plugin()))
    names = z.namelist()
    manifests = [n for n in names if n.endswith("plugin.json")]
    assert manifests == [".claude-plugin/plugin.json"]
    manifest = json.loads(z.read(".claude-plugin/plugin.json"))
    assert manifest["name"] == "talent-search" and "version" not in manifest
    assert manifest["author"] == {"name": "Talent Search"}
    for n in skills.skill_names():
        assert f"skills/{n}/SKILL.md" in names
        assert z.read(f"skills/{n}/SKILL.md") == (REPO / "skills" / n / "SKILL.md").read_bytes()
    assert len([n for n in names if n.endswith("SKILL.md")]) == 6
    assert not any(skills.MARKER in n for n in names)


def test_plugin_is_deterministic():
    assert skills.build_plugin() == skills.build_plugin()


def test_repo_marketplace_files_agree_with_the_app():
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    built = json.loads(zipfile.ZipFile(io.BytesIO(skills.build_plugin())).read(".claude-plugin/plugin.json"))
    assert plugin["name"] == built["name"] == market["plugins"][0]["name"]
    assert plugin["description"] == built["description"]
    assert market["plugins"][0]["source"] == "./" and market["name"] == skills.MARKETPLACE_NAME
    assert f"talent-search@{market['name']}" in skills.CLAUDE_CODE_PLUGIN_COMMANDS[1]
    for d in (REPO / "skills").iterdir():
        if d.is_dir():
            text = (d / "SKILL.md").read_text()
            assert f"\nname: {d.name}\n" in text


def test_detection_with_fake_homes(home):
    assert skills.detected_targets() == []
    (home / ".claude").mkdir()
    assert skills.detected_targets() == ["claude-code"]
    (home / ".codex").mkdir()
    assert skills.detected_targets() == ["claude-code", "codex"]


def test_combined_preview_and_install(home):
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    mine = home / ".claude" / "skills" / "my-own-skill"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine")
    plan = skills.plan_all()
    assert plan["targets"] == ["claude-code", "codex"] and plan["counts"]["new"] > 0
    assert not (home / ".agents").exists()  # a preview writes nothing
    res = skills.install_all(plan["targets"])
    assert len(res["results"]) == 2
    assert (home / ".agents" / "skills" / "talent-search-write-outreach" / "SKILL.md").is_file()
    assert (mine / "SKILL.md").read_text() == "mine"  # the person's own folder is untouched


def test_combined_install_never_touches_user_owned_folder(home):
    (home / ".claude").mkdir()
    clash = home / ".claude" / "skills" / "talent-search-write-outreach"
    clash.mkdir(parents=True)
    (clash / "SKILL.md").write_text("theirs")
    plan = skills.plan_all()
    assert plan["skipped"]
    skills.install_all(["claude-code"])
    assert (clash / "SKILL.md").read_text() == "theirs"


def test_write_plugin_to_downloads(tmp_path):
    p = skills.write_plugin()
    assert p == tmp_path / "Downloads" / "talent-search.plugin" and zipfile.is_zipfile(p)
    assert p.read_bytes() == skills.build_plugin()
    p2 = skills.write_plugin(tmp_path / "x")
    assert p2.name == "talent-search.plugin" or p2.name == "x"


def test_reveal_is_best_effort(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(skills.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    f = tmp_path / "a.plugin"
    assert skills.reveal(f, "darwin") and calls[-1][:2] == ["open", "-R"]
    assert skills.reveal(f, "win32") and calls[-1][0] == "explorer" and calls[-1][1].startswith("/select,")
    assert skills.reveal(f, "linux") and calls[-1][0] == "xdg-open" and calls[-1][1] == str(tmp_path)

    def boom(*a, **k):
        raise OSError("no")
    monkeypatch.setattr(skills.subprocess, "Popen", boom)
    assert skills.reveal(f, "linux") is False


def test_claude_app_command(tmp_path):
    assert skills.claude_app_command("linux") is None
    exe = tmp_path / "AnthropicClaude" / "Claude.exe"
    exe.parent.mkdir()
    exe.write_text("")
    assert skills.claude_app_command("win32", {"LOCALAPPDATA": str(tmp_path)}) == [str(exe)]
    assert skills.claude_app_command("win32", {"LOCALAPPDATA": str(tmp_path / "none")}) is None


def test_routes(store, home, tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "reveal", lambda path, platform=None: True)
    c = local_client(store)
    page = c.get("/connect").text
    assert "Install skills" in page and "Get the Claude plugin" in page and "Not found on this computer" in page
    assert "Install anyway" in page and "Older way: upload skills one at a time" in page
    assert "claude plugin marketplace add RogerFlores3113/talent-tracker" in page
    assert "talent-search@talent-tracker" in page and "Customize" in page
    r = c.get("/connect/skills.plugin")
    assert r.status_code == 200 and zipfile.is_zipfile(io.BytesIO(r.content))
    # nothing found: honest message, and nothing written
    r = c.post("/connect/skills/all/preview-install")
    assert r.status_code == 200 and "No AI app was found" in r.text
    (home / ".claude").mkdir()
    r = c.post("/connect/skills/all/preview-install")
    assert "Claude Code" in r.text and "talent-search-write-outreach/SKILL.md" in r.text
    assert not (home / ".claude" / "skills").exists()
    r = c.post("/connect/skills/all/install", data={"targets": "claude-code"})
    assert "Start a new chat" in r.text and (home / ".claude" / "skills").is_dir()
    # install anyway names a target that isn't found
    r = c.post("/connect/skills/all/preview-install", data={"targets": "codex"})
    assert "Codex" in r.text and not (home / ".agents").exists()
    assert c.post("/connect/skills/all/nope").status_code == 404
    r = c.post("/connect/skills/plugin/save")
    assert r.status_code == 200 and "talent-search.plugin" in r.text
    assert (tmp_path / "Downloads" / "talent-search.plugin").is_file()
    assert c.post("/connect/skills/claude-app/open").status_code == 200
    # the old routes still work
    assert c.post("/connect/skills/codex/preview-install").status_code == 200
    assert c.get("/connect/skills.zip").status_code == 200


def test_tour_does_not_autostart_on_connect(store):
    c = local_client(store)
    assert 'id="tour-data"' not in c.get("/connect").text


def test_cli_plugin_and_default(home, tmp_path, capsys):
    main(["install-skills", "--plugin", str(tmp_path / "out.plugin")])
    assert zipfile.is_zipfile(tmp_path / "out.plugin") and not (home / ".claude").exists()
    main(["install-skills", "--plugin"])
    assert (tmp_path / "Downloads" / "talent-search.plugin").is_file()
    main(["install-skills", "--yes"])  # nothing found
    assert "No Claude Code or Codex found" in capsys.readouterr().out
    (home / ".codex").mkdir()
    main(["install-skills", "--yes"])
    assert (home / ".agents" / "skills" / "talent-search-write-outreach").is_dir() and not (home / ".claude" / "skills").exists()


def test_page_details_labels_and_friendly_names(store):
    c = local_client(store)
    page = c.get("/connect").text
    for label in ("Copy Claude Code command", "Copy marketplace name", "Copy Claude Code plugin command 1"):
        assert f'aria-label="{label}"' in page
    assert "Find people for a role" in page and "talent-search-source-for-a-role" in page
    assert page.index("Claude app (desktop or claude.ai)") < page.index("Claude Code and Codex")
    assert "<details id=\"claude-code-steps\">" in page
    assert len(skills.skill_info()) == 6 and all(k["line"] for k in skills.skill_info())


def test_confirm_is_a_labelled_group(store, home):
    (home / ".claude").mkdir()
    c = local_client(store)
    r = c.post("/connect/skills/all/preview-install")
    assert 'role="group" aria-label="Add skills to ~/.claude/skills"' in r.text
    r = c.post("/connect/skills/claude-code/preview-install")
    assert 'role="group" aria-label="Add skills to ~/.claude/skills"' in r.text
    assert "Start a new chat" in c.post("/connect/skills/claude-code/install").text


def test_ending_the_tour_stays_on_the_page(store):
    c = local_client(store)
    c.post("/tour/start", data={"mode": "short"}, follow_redirects=False)
    r = c.post("/tour/skip", headers={"Referer": LOCAL_URL + "/connect"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/connect")
