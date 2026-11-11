"""Review fixes for Connect: platform-proof tests, one short result, Windows matches Claude Desktop only,
a quiet CLI, and one result on the page at a time."""
from __future__ import annotations

import json
import sys
from html import unescape

import pytest

from talent import integrations, skills
from talent.cli import main

from conftest import local_client


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("TALENT_SKILLS_HOME", str(home))
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(dl))
    cfg = tmp_path / "claude" / "claude.json"
    cfg.parent.mkdir()
    monkeypatch.setattr(integrations, "claude_config_path", lambda *a, **k: cfg)
    return {"home": home, "dl": dl, "cfg": cfg}


def _ok_check(monkeypatch):
    monkeypatch.setattr(integrations, "test_launch", lambda *a, **k: {
        "ok": True, "tools": 55, "seconds": 0.3, "stderr_tail": [], "stray_stdout": [], "error": None})


# ---------------------------------------------------------------- A: the suite never touches a real Claude
def test_conftest_pins_restart_and_process_checks():
    assert integrations.restart_supported() is False
    assert integrations.restart_supported("darwin") and integrations.restart_supported("win32")
    assert not integrations.restart_supported("linux")
    assert integrations.app_running("claude") is None


@pytest.mark.parametrize("plat", ["linux", "darwin", "win32"])
def test_connect_result_is_one_short_message_on_every_platform(store, monkeypatch, plat):
    _ok_check(monkeypatch)
    monkeypatch.setattr(sys, "platform", plat)
    text = unescape(local_client(store).post("/connect/claude").text)
    assert text.count(integrations.HEALTHY_LINE) == 1
    assert "read the file back" not in text and "One more step" not in text and "If Claude Desktop was open" not in text
    visible = text.split("<details")[0]
    assert "55 tools" not in visible and "55 tools" in text  # the count lives in the technical details
    assert integrations.quit_instruction(plat) in text and text.count(integrations.quit_instruction(plat)) == 1
    if plat == "linux":
        assert "Quit Claude Desktop" not in text and "Linux has no restart" in text


def test_connect_success_swaps_the_connect_button_and_moves_focus(store, monkeypatch):
    _ok_check(monkeypatch)
    text = local_client(store).post("/connect/claude").text
    assert 'id="claude-actions" hx-swap-oob="true"' in text and "Test connection" in text and "Reconnect" in text
    assert 'id="connect-done" role="status" tabindex="-1" autofocus' in text
    assert "Connect Claude" not in text


# ---------------------------------------------------------------- C: Test connection has its own words
def test_test_connection_does_not_claim_anything_was_added(store, env, monkeypatch):
    _ok_check(monkeypatch)
    integrations.connect_claude(env["cfg"])
    text = unescape(local_client(store).post("/connect/claude/test").text)
    assert integrations.TEST_OK_LINE in text and "Added to Claude Desktop" not in text


# ---------------------------------------------------------------- H: one result at a time
def test_each_press_clears_the_other_result_areas(store, monkeypatch):
    _ok_check(monkeypatch)
    c = local_client(store)
    connect = c.post("/connect/claude").text
    assert 'id="skills-add-result" hx-swap-oob="true"></div>' in connect
    assert 'id="claude-result" hx-swap-oob="true"' not in connect
    add = c.post("/connect/skills/add").text
    assert 'id="claude-result" hx-swap-oob="true"></div>' in add
    assert 'id="skills-add-result" hx-swap-oob="true"' not in add
    test = c.post("/connect/claude/test").text
    assert 'id="skills-add-result" hx-swap-oob="true"></div>' in test


# ---------------------------------------------------------------- D: the command line says each thing once
def test_cli_restart_on_already_connected_says_each_thing_once(env, monkeypatch, capsys):
    _ok_check(monkeypatch)
    integrations.connect_claude(env["cfg"])
    monkeypatch.setattr(integrations, "restart_supported", lambda platform=None: True)
    monkeypatch.setattr(integrations, "restart_claude", lambda **k: {"message": integrations.CLAUDE_DONE})
    main(["connect-claude", "--restart"])
    out = capsys.readouterr().out
    assert "Already set up" in out and "Added to Claude Desktop" not in out and "Read it back" not in out
    assert "quit it completely" not in out and out.count("Quit Claude Desktop") == 0
    assert out.count(integrations.CLAUDE_DONE) == 1


def test_cli_without_restart_prints_the_next_step_once(env, monkeypatch, capsys):
    _ok_check(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    main(["connect-claude"])
    out = capsys.readouterr().out
    assert out.count("Quit Claude Desktop completely") == 1 and integrations.HEALTHY_LINE in out


def test_install_skills_up_to_date_in_a_script_exits_zero(env, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as refused:
        main(["install-skills", "--claude-code"])  # new files, nobody to ask: a real refusal
    assert refused.value.code == 1
    main(["install-skills", "--claude-code", "--yes"])
    capsys.readouterr()
    main(["install-skills", "--claude-code"])  # all current: no exit at all
    out = capsys.readouterr().out
    assert "Already up to date" in out and "Nothing installed yet" not in out
    assert max(len(line) for line in out.splitlines()) < 200 and len(out.splitlines()) < 12


# ---------------------------------------------------------------- E: Add skills wording
def test_add_skills_names_the_codex_folder_and_only_mentions_a_window_that_opened(store, env, monkeypatch):
    (env["home"] / ".codex").mkdir()
    monkeypatch.setattr(skills, "reveal", lambda path, platform=None: False)  # Linux with no file manager
    monkeypatch.setattr(sys, "platform", "linux")
    t = local_client(store).post("/connect/skills/add").text
    assert "added 6 skills to your Codex skills folder (~/.agents/skills)" in t and "Folder:" not in t
    assert "window that just opened" not in t and "opened that folder" not in t
    (env["dl"] / skills.PLUGIN_FILE).unlink()
    r = skills.add_everywhere(reveal_fn=lambda p: True, platform="linux")
    assert r["shown"] and not r["selected"]
    r = skills.add_everywhere(reveal_fn=lambda p: True, platform="darwin")
    assert r["plugin_repeat"] and not r["shown"]  # a repeat opens nothing
    (env["dl"] / skills.PLUGIN_FILE).unlink()
    r = skills.add_everywhere(reveal_fn=lambda p: True, platform="darwin")
    assert r["shown"] and r["selected"]


# ---------------------------------------------------------------- F: the page
def test_connect_page_folds_and_wording(store):
    page = local_client(store).get("/connect").text
    assert "Step 2 above adds them to Codex too" in page and "Step 2 below" not in page
    assert page.count('id="skills-other-ways"') == 1
    ask = page.index('id="skills-ask-ai"')
    assert page.index("INSTALL-SKILLS.md") > ask and "Ask Claude Code to install the Talent Search skills" in page
    assert page.index('id="install-skills-btn"') < page.index('id="skills-other-ways"') < ask
    assert "There is no restart step on Linux" in page or "no restart" in page.lower()


# ---------------------------------------------------------------- G: Windows matches Claude Desktop only
DESKTOP = {"Id": 10, "Path": r"C:\Users\r\AppData\Local\AnthropicClaude\app-1.2\claude.exe"}
PROGRAMS = {"Id": 11, "Path": r"C:\Users\r\AppData\Local\Programs\Claude\Claude.exe"}
STORE = {"Id": 12, "Path": r"C:\Program Files\WindowsApps\Claude_1.0_x64__abc\app\Claude.exe"}
CODE = {"Id": 99, "Path": r"C:\Users\r\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"}


def test_windows_pids_keep_desktop_and_ignore_claude_code():
    ps = lambda rows: (lambda: json.dumps(rows))
    assert integrations.windows_desktop_pids(ps([DESKTOP, CODE, PROGRAMS, STORE])) == [10, 11, 12]
    assert integrations.windows_desktop_pids(ps(CODE)) == []  # one process: PowerShell sends an object, not a list
    assert integrations.windows_desktop_pids(ps(DESKTOP)) == [10]
    assert integrations.windows_desktop_pids(ps([{"Id": 5, "Path": None}])) == []
    assert integrations.windows_desktop_pids(lambda: "") == []
    assert integrations.windows_desktop_pids(lambda: "not json") is None

    def boom():
        raise OSError("no powershell")

    assert integrations.windows_desktop_pids(boom) is None


def test_windows_restart_only_closes_desktop_by_pid(monkeypatch):
    rows = [DESKTOP, CODE]
    alive = {"v": True}
    calls, launched = [], []

    def run(cmd, timeout=10.0):
        calls.append(cmd)
        alive["v"] = False
        return 0

    pids = lambda: integrations.windows_desktop_pids(lambda: json.dumps(rows))
    r = integrations.restart_claude(platform="win32", running=lambda app="claude": alive["v"], run=run,
                                    launch=launched.append, command=[r"C:\Claude\Claude.exe"], sleep=lambda s: None,
                                    clock=lambda: 0.0, pids=pids)
    assert calls == [["taskkill", "/PID", "10"]] and r["ok"] and launched
    assert all("/F" not in c and "/IM" not in c and "99" not in c for c in calls)


def test_windows_app_running_ignores_claude_code(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.undo()  # keep the real app_running (conftest stubbed it), then fake only the process list
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(integrations, "_powershell_claude_processes", lambda: json.dumps([CODE]))
    assert integrations.app_running("claude") is False
    monkeypatch.setattr(integrations, "_powershell_claude_processes", lambda: json.dumps([CODE, DESKTOP]))
    assert integrations.app_running("claude") is True
