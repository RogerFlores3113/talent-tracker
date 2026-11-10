"""One click to connect Claude (restart included) and one click for the skills, and the page order."""
from __future__ import annotations

import sys

import pytest

from talent import integrations, skills

from conftest import local_client


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    monkeypatch.setenv("TALENT_SKILLS_HOME", str(home))
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(dl))
    root = tmp_path / "apps"
    for name in ("claude", "codex", "lmstudio"):
        (root / name).mkdir(parents=True)
    monkeypatch.setattr(integrations, "claude_config_path", lambda *a, **k: root / "claude" / "claude.json")
    monkeypatch.setattr(integrations, "codex_config_path", lambda *a, **k: root / "codex" / "config.toml")
    monkeypatch.setattr(integrations, "lmstudio_config_path", lambda *a, **k: root / "lmstudio" / "mcp.json")
    return {"home": home, "dl": dl}


class Fake:
    """Stands in for every process the restart touches. `quits` says whether Claude exits after being asked."""

    def __init__(self, running=True, quits=True):
        self.alive, self.quits, self.calls, self.launched, self.t = running, quits, [], [], 0.0

    def running(self, app="claude"):
        return self.alive

    def run(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        if self.quits:
            self.alive = False
        return 0

    def launch(self, cmd):
        self.launched.append(cmd)
        self.alive = True

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t

    def go(self, platform, command):
        return integrations.restart_claude(platform=platform, running=self.running, run=self.run, launch=self.launch,
                                           command=command, sleep=self.sleep, clock=self.clock,
                                           pids=lambda: [4242])


def test_mac_running_is_quit_then_opened():
    f = Fake()
    r = f.go("darwin", ["open", "-a", "Claude"])
    assert f.calls == [["osascript", "-e", 'tell application "Claude" to quit']]
    assert f.launched == [["open", "-a", "Claude"]]
    assert r["ok"] and r["quit"] and r["was_running"] and r["message"] == integrations.CLAUDE_DONE


def test_mac_not_running_just_opens():
    f = Fake(running=False)
    r = f.go("darwin", ["open", "-a", "Claude"])
    assert f.calls == [] and f.launched == [["open", "-a", "Claude"]]
    assert r["ok"] and not r["quit"] and r["message"] == integrations.CLAUDE_DONE


def test_windows_running_is_asked_to_close_then_started():
    f = Fake()
    r = f.go("win32", [r"C:\Claude\Claude.exe"])
    assert f.calls == [["taskkill", "/PID", "4242"]] and "/F" not in f.calls[0]
    assert f.launched == [[r"C:\Claude\Claude.exe"]] and r["ok"]


def test_windows_that_will_not_close_is_never_force_killed():
    f = Fake(quits=False)
    r = f.go("win32", [r"C:\Claude\Claude.exe"])
    assert f.calls == [["taskkill", "/PID", "4242"]]  # asked once, politely
    assert f.launched == [] and not r["ok"] and f.t >= 10
    assert r["message"].startswith("Connected") and integrations.quit_instruction("win32") in r["message"]


def test_restart_never_raises_and_is_silent_on_linux():
    assert integrations.restart_claude(platform="linux")["supported"] is False

    def boom(*a, **k):
        raise RuntimeError("no")

    r = integrations.restart_claude(platform="darwin", running=boom, command=["x"])
    assert r["ok"] is False and r["message"]


def test_no_claude_to_open_says_so():
    f = Fake(running=False)
    r = f.go("darwin", [])
    assert not r["ok"] and "Open Claude Desktop" in r["message"] and f.launched == []


# ------------------------------------------------------------------ the Connect route
def _fake_connect(monkeypatch):
    monkeypatch.setattr(integrations, "connect_claude", lambda *a, **k: {
        "ok": True, "installed": True, "path": "/x/claude.json", "entry": {"command": "x"}, "backup": None})
    monkeypatch.setattr(integrations, "test_launch", lambda *a, **k: {
        "ok": True, "tools": 5, "seconds": 0.2, "stderr_tail": [], "stray_stdout": []})


def test_connect_on_mac_restarts_and_shows_one_message(store, monkeypatch):
    _fake_connect(monkeypatch)
    calls = []
    monkeypatch.setattr(integrations, "restart_supported", lambda platform=None: True)
    monkeypatch.setattr(integrations, "restart_claude", lambda **k: calls.append(1) or {
        "supported": True, "ok": True, "quit": True, "message": integrations.CLAUDE_DONE})
    text = local_client(store).post("/connect/claude").text
    assert calls == [1] and text.count(integrations.CLAUDE_DONE) == 1
    assert "One more step" not in text and "completely" not in text.split("Technical details")[0]


def test_connect_when_claude_will_not_close_says_the_one_quit_line(store, monkeypatch):
    _fake_connect(monkeypatch)
    msg = "Connected, but Claude is still open. " + integrations.quit_instruction("win32")
    monkeypatch.setattr(integrations, "restart_supported", lambda platform=None: True)
    monkeypatch.setattr(integrations, "restart_claude", lambda **k: {"supported": True, "ok": False, "message": msg})
    text = local_client(store).post("/connect/claude").text
    assert "still open" in text and "system tray" in text and integrations.CLAUDE_DONE not in text


def test_connect_on_linux_does_not_restart(store, monkeypatch):
    _fake_connect(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(integrations, "restart_claude", lambda **k: pytest.fail("restart on Linux"))
    text = local_client(store).post("/connect/claude").text
    assert "Open Claude and start a new chat" in text and "Quit Claude Desktop" not in text
    assert text.count("start a new chat") == 1 and "Linux has no restart" in text


def test_connect_button_says_it_restarts_claude(store, monkeypatch):
    monkeypatch.setattr(integrations, "restart_supported", lambda platform=None: True)
    monkeypatch.setattr(integrations, "app_running", lambda app="claude": True)
    page = local_client(store).get("/connect").text
    assert "Connect Claude (restarts Claude)" in page and "Save anything you're typing in Claude first." in page


# ------------------------------------------------------------------ Add skills
def test_add_skills_installs_saves_reveals_opens_and_is_idempotent(store, env, monkeypatch):
    shown, opened = [], []
    monkeypatch.setattr(skills, "reveal", lambda path, platform=None: shown.append(path) or True)
    monkeypatch.setattr(skills, "claude_app_command", lambda *a, **k: ["open", "-a", "Claude"])
    monkeypatch.setattr(skills, "open_claude_app", lambda: opened.append(1) or True)
    c = local_client(store)
    r = c.post("/connect/skills/add")
    assert r.status_code == 200
    assert (env["home"] / ".claude" / "skills" / "talent-search-write-outreach" / "SKILL.md").is_file()
    assert not (env["home"] / ".agents").exists()  # Codex isn't here, so nothing was made for it
    assert (env["dl"] / "talent-search.plugin").is_file() and len(shown) == 1 and opened == [1]
    t = r.text
    assert "Claude Code</strong>: added 6 skills to your Claude Code skills folder (~/.claude/skills)" in t
    assert "Codex</strong>: not found" in t
    assert "In Claude, open Customize &gt; Plugins" in t and "Press Add, then Upload plugin" in t
    assert "Pick talent-search.plugin" in t and "Show in folder" in t and "Already up to date" not in t
    again = c.post("/connect/skills/add").text
    assert "Already up to date" in again and "is still in" in again
    assert "Upload plugin" not in again and "selected in the window" not in again
    assert len(shown) == 1 and opened == [1]  # a repeat press shows and opens nothing again


def test_add_skills_keeps_files_the_person_edited(store, env, monkeypatch):
    monkeypatch.setattr(skills, "reveal", lambda path, platform=None: True)
    c = local_client(store)
    c.post("/connect/skills/add")
    f = env["home"] / ".claude" / "skills" / "talent-search-write-outreach" / "SKILL.md"
    f.write_text("my own words\n", encoding="utf-8")
    c.post("/connect/skills/add")
    assert f.read_text(encoding="utf-8") == "my own words\n"


def test_show_in_folder_route(store, env, monkeypatch):
    shown = []
    monkeypatch.setattr(skills, "reveal", lambda path, platform=None: shown.append(path) or True)
    r = local_client(store).post("/connect/skills/plugin/reveal")
    assert r.status_code == 200 and "Shown" in r.text and shown and (env["dl"] / "talent-search.plugin").is_file()


# ------------------------------------------------------------------ the page
def test_page_order_connect_then_skills_then_try_asking_then_other_apps(store):
    page = local_client(store).get("/connect").text
    order = [page.index(x) for x in ('id="step-connect"', 'id="claude-panel"', 'id="step-skills"', 'id="skills-panel"',
                                     'id="try-asking"', 'id="other-apps"', 'id="by-hand"')]
    assert order == sorted(order)
    assert "Other apps (Claude Code, Codex, ChatGPT desktop, LM Studio, Cursor...)" in page
    for hook in ("claude-panel", "codex-panel", "claude-code-panel", "lmstudio-panel", "skills-panel",
                 "install-skills-btn", "claude-plugin", "where-cards-show", "plugin-steps", "by-hand"):
        assert f'id="{hook}"' in page, hook
    assert page.count('id="claude-panel"') == 1 and page.count('id="codex-panel"') == 1


def test_the_app_that_fits_this_computer_leads(store, tmp_path, monkeypatch):
    root = tmp_path / "apps"
    import shutil
    shutil.rmtree(root / "claude")
    page = local_client(store).get("/connect").text
    assert page.index('id="codex-panel"') < page.index('id="skills-panel"') < page.index('id="claude-panel"')
    shutil.rmtree(root / "codex")
    shutil.rmtree(root / "lmstudio")
    page = local_client(store).get("/connect").text  # nothing installed: Claude Desktop with its Get link
    assert page.index('id="claude-panel"') < page.index('id="skills-panel"') and "Get Claude Desktop" in page


def test_no_dashes_in_connect_text(store):
    c = local_client(store)
    for text in (c.get("/connect").text, c.post("/connect/skills/add").text):
        assert "\u2014" not in text and "\u2013" not in text
