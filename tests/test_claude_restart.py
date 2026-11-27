"""Claude Desktop from the Microsoft Store: reopen it, force it closed when it hides in the tray, and know when it
hasn't loaded our entry yet. Nothing here touches a real process."""
from __future__ import annotations

import os
import sys

from talent import integrations, skills

STORE_FAMILY = "Claude_pzs8sxrjxfjjc"
EXE = r"C:\Program Files\WindowsApps\Claude_1.0_x64__pzs8sxrjxfjjc\app\claude.exe"


# ------------------------------------------------------------ how to open Claude
def test_store_install_opens_through_its_app_id(tmp_path):
    cmd = skills.claude_app_command("win32", {"LOCALAPPDATA": str(tmp_path / "none")}, run=lambda c: STORE_FAMILY + "\r\n")
    assert cmd == ["explorer.exe", f"shell:AppsFolder\\{STORE_FAMILY}!Claude"]


def test_classic_exe_wins_over_the_store(tmp_path):
    exe = tmp_path / "AnthropicClaude" / "Claude.exe"
    exe.parent.mkdir()
    exe.write_text("")
    assert skills.claude_app_command("win32", {"LOCALAPPDATA": str(tmp_path)}, run=lambda c: STORE_FAMILY) == [str(exe)]


def test_no_claude_anywhere_is_none(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "none")}
    assert skills.claude_app_command("win32", env, run=lambda c: "") is None

    def boom(cmd):
        raise OSError("no powershell")

    assert skills.claude_app_command("win32", env, run=boom) is None


# ------------------------------------------------------------ restart on Windows
class Tray:
    """Claude keeps running after a polite quit. `dies_on` is the call that really stops it."""

    def __init__(self, dies_on="force"):
        self.alive, self.dies_on, self.calls, self.launched, self.t = True, dies_on, [], [], 0.0

    def running(self, app="claude"):
        return self.alive

    def run(self, cmd, timeout=10.0):
        self.calls.append(cmd)
        if self.dies_on == "force" and "/F" in cmd:
            self.alive = False
        return 0

    def launch(self, cmd):
        self.launched.append(cmd)
        self.alive = True

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t

    def go(self):
        return integrations.restart_claude(platform="win32", running=self.running, run=self.run, launch=self.launch,
                                           command=["explorer.exe", "shell:AppsFolder\\x!Claude"], sleep=self.sleep,
                                           clock=self.clock, pids=lambda: [4242])


def test_claude_hiding_in_the_tray_is_forced_closed_then_opened():
    f = Tray()
    r = f.go()
    assert f.calls == [["taskkill", "/PID", "4242"], ["taskkill", "/F", "/T", "/PID", "4242"]]
    assert f.launched == [["explorer.exe", "shell:AppsFolder\\x!Claude"]]
    assert r["quit"] is True and r["opened"] is True and r["ok"] is True and r["message"] == integrations.CLAUDE_DONE


def test_claude_that_survives_the_force_is_reported_and_not_launched():
    f = Tray(dies_on="never")
    r = f.go()
    assert f.calls == [["taskkill", "/PID", "4242"], ["taskkill", "/F", "/T", "/PID", "4242"]]
    assert f.launched == [] and r["ok"] is False and r["opened"] is False
    assert r["message"] == "Connected, but Claude is still open. " + integrations.quit_instruction("win32")


# ------------------------------------------------------------ logs and "hasn't loaded us yet"
def test_windows_logs_dir_prefers_the_new_local_folder(tmp_path, monkeypatch):
    local = tmp_path / "Local"
    logs = local / "Claude" / "Logs"
    logs.mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert integrations.claude_logs_dir() == logs


def _connected(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(integrations, "restart_supported", lambda platform=None: True)
    cfg = tmp_path / "claude_desktop_config.json"
    assert integrations.connect_claude(cfg)["ok"]
    logs = tmp_path / "Logs"
    logs.mkdir()
    return cfg, logs


def test_no_entry_needs_no_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert integrations.claude_needs_restart(tmp_path / "claude_desktop_config.json", tmp_path) is False


def test_entry_with_no_server_log_needs_a_restart(tmp_path, monkeypatch):
    cfg, logs = _connected(tmp_path, monkeypatch)
    assert integrations.claude_needs_restart(cfg, logs) is True


def test_server_log_newer_than_the_config_means_claude_loaded_it(tmp_path, monkeypatch):
    cfg, logs = _connected(tmp_path, monkeypatch)
    log = logs / "mcp-server-talent-search.log"
    log.write_text("started")
    os.utime(cfg, (1000, 1000))
    os.utime(log, (2000, 2000))
    assert integrations.claude_needs_restart(cfg, logs) is False


def test_config_newer_than_the_server_log_needs_a_restart(tmp_path, monkeypatch):
    cfg, logs = _connected(tmp_path, monkeypatch)
    log = logs / "mcp-server-talent-search.log"
    log.write_text("started")
    os.utime(log, (1000, 1000))
    os.utime(cfg, (2000, 2000))
    assert integrations.claude_needs_restart(cfg, logs) is True


# ------------------------------------------------------------ connecting at startup
def _json_app(tmp_path, monkeypatch):
    monkeypatch.setattr(integrations, "is_frozen", lambda: True)
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    monkeypatch.setattr(integrations, "claude_config_path", lambda *a, **k: claude / "claude_desktop_config.json")
    monkeypatch.setattr(integrations, "codex_config_path", lambda *a, **k: codex / "config.toml")
    return claude, codex


def test_connect_if_missing_writes_entries_for_installed_apps(tmp_path, monkeypatch):
    claude, codex = _json_app(tmp_path, monkeypatch)
    claude.mkdir()
    codex.mkdir()
    assert integrations.connect_if_missing() == ["Claude Desktop", "Codex"]
    assert integrations.entry_for("claude") is not None and integrations.entry_for("codex") is not None


def test_connect_if_missing_skips_an_app_that_is_not_installed(tmp_path, monkeypatch):
    claude, codex = _json_app(tmp_path, monkeypatch)
    codex.mkdir()
    assert integrations.connect_if_missing() == ["Codex"]
    assert not claude.exists()


def test_connect_if_missing_leaves_an_existing_entry_alone(tmp_path, monkeypatch):
    claude, codex = _json_app(tmp_path, monkeypatch)
    claude.mkdir()
    codex.mkdir()
    old = '{"mcpServers": {"talent-search": {"command": "/gone/talent"}}}'
    (claude / "claude_desktop_config.json").write_text(old)
    toml = '[mcp_servers.talent-search]\ncommand = "/gone/talent"\n'
    (codex / "config.toml").write_text(toml)
    assert integrations.connect_if_missing() == []
    assert (claude / "claude_desktop_config.json").read_text() == old
    assert (codex / "config.toml").read_text() == toml


def test_connect_if_missing_does_nothing_in_a_developer_run(tmp_path, monkeypatch):
    claude, _ = _json_app(tmp_path, monkeypatch)
    monkeypatch.setattr(integrations, "is_frozen", lambda: False)
    claude.mkdir()
    assert integrations.connect_if_missing() == []
    assert not (claude / "claude_desktop_config.json").exists()


# ------------------------------------------------------------ the restart toast and route
def test_restart_toast_shows_only_when_claude_needs_a_restart(store, monkeypatch):
    from conftest import local_client

    c = local_client(store)
    monkeypatch.setattr(integrations, "claude_needs_restart", lambda *a, **k: False)
    assert 'id="restart-toast"' not in c.get("/").text
    monkeypatch.setattr(integrations, "claude_needs_restart", lambda *a, **k: True)
    page = c.get("/").text
    assert 'id="restart-toast"' in page and "Claude Desktop doesn&#39;t see Talent Search yet." in page.replace("'", "&#39;")
    assert 'action="/connect/claude/restart"' in page and "Restart Claude</button>" in page


def test_restart_route_restarts_and_flashes_the_message(store, monkeypatch):
    from conftest import flash_of, local_client

    monkeypatch.setattr(integrations, "restart_claude",
                        lambda **k: {"supported": True, "ok": True, "message": integrations.CLAUDE_DONE})
    r = local_client(store).post("/connect/claude/restart", follow_redirects=False)
    assert r.status_code == 303 and flash_of(r) == integrations.CLAUDE_DONE
    monkeypatch.setattr(integrations, "restart_claude",
                        lambda **k: {"supported": True, "ok": False, "message": "Connected, but Claude is still open."})
    r = local_client(store).post("/connect/claude/restart", follow_redirects=False)
    assert flash_of(r, "flash_error") == "Connected, but Claude is still open."
