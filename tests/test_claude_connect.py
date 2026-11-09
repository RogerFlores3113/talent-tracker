"""Connect Claude Desktop: what we write is checked, read back, launched and explained."""
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from conftest import local_client
from talent import integrations, paths
from talent.integrations import LaunchSpec


def make_script(tmp_path: Path, body: str, name: str = "srv.py") -> dict:
    script = tmp_path / name
    script.write_text(body)
    return {"command": sys.executable, "args": [str(script)], "env": {"TALENT_DATA_DIR": str(tmp_path)}}


def real_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "talent-search"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def spec_for(exe: Path) -> LaunchSpec:
    return LaunchSpec(str(exe), ["mcp"], {"TALENT_DATA_DIR": str(paths.data_dir().resolve())})


# ------------------------------------------------------------- writer
def test_writer_creates_file_and_keeps_everything_else(tmp_path):
    p = tmp_path / "Claude" / "claude_desktop_config.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"preferences": {"x": 1}, "mcpServers": {"other": {"command": "o"}}}))
    res = integrations.connect_claude(p, spec_for(real_exe(tmp_path)))
    cfg = json.loads(p.read_text())
    assert res["ok"] and res["read_back"]
    assert cfg["preferences"] == {"x": 1} and cfg["mcpServers"]["other"] == {"command": "o"}
    assert cfg["mcpServers"]["talent-search"] == res["entry"]
    fresh = tmp_path / "C2" / "c.json"
    fresh.parent.mkdir()
    integrations.connect_claude(fresh, spec_for(real_exe(tmp_path)))
    assert fresh.exists()


def test_writer_verifies_by_reading_back(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    monkeypatch.setattr(integrations, "_write", lambda path, text: path.write_text('{"mcpServers": {}}'))
    with pytest.raises(ValueError, match="read it back"):
        integrations.connect_claude(p, spec_for(real_exe(tmp_path)))


@pytest.mark.parametrize("command,needle", [
    ("/private/var/folders/ab/xyz/T/AppTranslocation/1234-UUID/d/Talent Search.app/Contents/MacOS/Talent Search",
     "temporary copy"),
    ("/Volumes/Talent Search/Talent Search.app/Contents/MacOS/Talent Search", "disk image"),
])
def test_writer_refuses_translocated_and_mounted_paths(tmp_path, monkeypatch, command, needle):
    monkeypatch.setattr(integrations, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "platform", "darwin")
    p = tmp_path / "c.json"
    p.write_text("{}")
    res = integrations.connect_claude(p, LaunchSpec(command, ["mcp"], {}))
    assert res["ok"] is False and needle in res["problem"]
    assert p.read_text() == "{}"
    assert not p.with_name("c.json.bak").exists()


def test_launch_problem_cases(tmp_path):
    ci = "/private/var/folders/zz/T/tmpabc/Talent Search.app/Contents/MacOS/Talent Search"
    assert integrations.launch_problem(platform="darwin", frozen=True, exe=ci) is None
    assert integrations.launch_problem(platform="darwin", frozen=False, exe="/Volumes/x/y") is None
    assert "Applications folder" in integrations.launch_problem(platform="darwin", frozen=True, exe="/Volumes/x/y")
    main = tmp_path / "Talent Search.exe"
    main.write_text("")
    assert "unzip the whole folder" in integrations.launch_problem(platform="win32", frozen=True, exe=str(main))
    (tmp_path / "talent-search-mcp.exe").write_text("")
    assert integrations.launch_problem(platform="win32", frozen=True, exe=str(main)) is None


def test_broken_json_untouched(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{ nope")
    with pytest.raises(ValueError):
        integrations.connect_claude(p, spec_for(real_exe(tmp_path)))
    assert p.read_text() == "{ nope"


def test_claude_connected_needs_a_real_command(tmp_path):
    p = tmp_path / "Claude" / "c.json"
    p.parent.mkdir()
    gone = LaunchSpec(str(tmp_path / "nowhere"), ["mcp"], {"TALENT_DATA_DIR": str(paths.data_dir().resolve())})
    integrations.connect_claude(p, gone)
    assert not integrations.claude_connected(p)
    integrations.connect_claude(p, spec_for(real_exe(tmp_path)))
    assert integrations.claude_connected(p)


# --------------------------------------------------------- check_entry
def test_check_entry_problems(tmp_path):
    good = spec_for(real_exe(tmp_path)).as_json_entry()
    assert integrations.check_entry(good) == []
    assert any("no command" in p for p in integrations.check_entry({**good, "command": ""}))
    assert any("full path" in p for p in integrations.check_entry({**good, "command": "talent"}))
    assert any("isn't there" in p for p in integrations.check_entry({**good, "command": str(tmp_path / "no")}))
    plain = tmp_path / "plain"
    plain.write_text("x")
    plain.chmod(0o644)
    if os.name != "nt":
        assert any("no permission" in p for p in integrations.check_entry({**good, "command": str(plain)}))
    assert any("arguments" in p for p in integrations.check_entry({**good, "args": "mcp"}))
    assert any("data folder" in p for p in integrations.check_entry({**good, "env": {}}))
    assert any("different data folder" in p
               for p in integrations.check_entry({**good, "env": {"TALENT_DATA_DIR": str(tmp_path / "other")}}))


# ------------------------------------------------------ stdio handshake
def test_real_server_handshake(tmp_path):
    entry = {"command": sys.executable, "args": ["-m", "talent", "mcp"], "env": {"TALENT_DATA_DIR": str(tmp_path)}}
    res = integrations.test_launch(entry, timeout=60)
    assert res["ok"], res
    assert res["tools"] > 10 and res["server_name"]
    assert "answered with" in integrations.result_line(res)


def test_launch_missing_command(tmp_path):
    res = integrations.test_launch({"command": str(tmp_path / "nope"), "args": []})
    assert not res["ok"] and "isn't there any more" in res["error"]


def test_launch_exits_immediately(tmp_path):
    res = integrations.test_launch(make_script(tmp_path, "import sys; print('boom', file=sys.stderr); sys.exit(3)"),
                                   timeout=10)
    assert not res["ok"] and "exit code 3" in res["error"]
    assert "boom" in res["stderr_tail"]


def test_launch_banner_on_stdout_then_silence(tmp_path):
    res = integrations.test_launch(
        make_script(tmp_path, "import time\nprint('Welcome to my server!', flush=True)\ntime.sleep(30)"), timeout=3)
    assert not res["ok"] and "didn't answer within 3 seconds" in res["error"]
    assert res["stray_stdout"] == ["Welcome to my server!"]


def test_launch_hung_server_is_killed(tmp_path):
    pidfile = tmp_path / "pid"
    body = f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)"
    res = integrations.test_launch(make_script(tmp_path, body), timeout=2)
    assert not res["ok"]
    pid = int(pidfile.read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
            dead = False
        except OSError:
            dead = True
        if dead:
            break
        time.sleep(0.25)
    assert dead
    assert res["seconds"] < 10


# --------------------------------------------------------- self-repair
def point_at_stale(tmp_path, monkeypatch):
    p = tmp_path / "Claude" / "claude_desktop_config.json"
    p.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(integrations, "claude_config_path", lambda: p)
    monkeypatch.setattr(integrations, "lmstudio_config_path", lambda: tmp_path / "lm" / "mcp.json")
    monkeypatch.setattr(integrations, "codex_config_path", lambda: tmp_path / "cx" / "config.toml")
    exe = tmp_path / "talent-search"
    if not exe.exists():
        exe = real_exe(tmp_path)
    monkeypatch.setattr(integrations, "mcp_launch_spec", lambda: spec_for(exe))
    return p, exe


def test_repair_rewrites_a_stale_path(tmp_path, monkeypatch):
    p, exe = point_at_stale(tmp_path, monkeypatch)
    stale = {"command": str(tmp_path / "old" / "talent-search"), "args": ["mcp"], "env": {"TALENT_DATA_DIR": "/x"}}
    p.write_text(json.dumps({"mcpServers": {"talent-search": stale, "other": {"command": "o"}}}))
    assert integrations.repair_stale_entries() == ["claude"]
    cfg = json.loads(p.read_text())["mcpServers"]
    assert cfg["talent-search"]["command"] == str(exe) and cfg["other"] == {"command": "o"}
    assert p.with_name(p.name + ".bak").exists()


def test_repair_never_adds_an_entry(tmp_path, monkeypatch):
    p, _ = point_at_stale(tmp_path, monkeypatch)
    p.write_text(json.dumps({"mcpServers": {}}))
    assert integrations.repair_stale_entries() == []
    assert json.loads(p.read_text()) == {"mcpServers": {}}


def test_repair_leaves_healthy_entry_alone(tmp_path, monkeypatch):
    p, exe = point_at_stale(tmp_path, monkeypatch)
    integrations.connect_claude(p, spec_for(exe))
    before = p.stat().st_mtime_ns
    assert integrations.repair_stale_entries() == []
    assert p.stat().st_mtime_ns == before


def test_repair_skips_temporary_copies(tmp_path, monkeypatch):
    p, _ = point_at_stale(tmp_path, monkeypatch)
    p.write_text(json.dumps({"mcpServers": {"talent-search": {"command": "/gone", "args": [], "env": {}}}}))
    monkeypatch.setattr(integrations, "launch_problem", lambda *a, **k: "temporary")
    assert integrations.repair_stale_entries() == []


# ----------------------------------------------------------------- web
def web_setup(tmp_path, monkeypatch, connected=True, running=False):
    p, exe = point_at_stale(tmp_path, monkeypatch)
    p.unlink(missing_ok=True)
    if connected:
        integrations.connect_claude(p, spec_for(exe))
    monkeypatch.setattr(integrations, "app_running", lambda app="claude": running)
    monkeypatch.setattr(integrations, "claude_logs_dir", lambda: tmp_path / "logs")
    return p


def test_connect_page_connected_shows_path_and_test_button(store, tmp_path, monkeypatch):
    p = web_setup(tmp_path, monkeypatch)
    html = local_client(store).get("/connect").text
    assert f"<code>{p}</code>" in html and "Test connection" in html and "What was written" in html


def test_connect_page_broken_entry_offers_repair(store, tmp_path, monkeypatch):
    p = web_setup(tmp_path, monkeypatch, connected=False)
    p.write_text(json.dumps({"mcpServers": {"talent-search": {"command": "/gone/x", "args": [], "env": {}}}}))
    html = local_client(store).get("/connect").text
    assert "there any more" in html and "Repair Claude Desktop" in html


def test_connect_page_running_note(store, tmp_path, monkeypatch):
    web_setup(tmp_path, monkeypatch, running=True)
    assert "Claude Desktop is open." in local_client(store).get("/connect").text
    web_setup(tmp_path, monkeypatch, running=False)
    assert "Claude Desktop is open." not in local_client(store).get("/connect").text


def test_test_route_healthy_and_missing(store, tmp_path, monkeypatch):
    web_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(integrations, "test_launch", lambda entry, timeout=30.0: {
        "ok": True, "seconds": 1.8, "tools": 24, "stderr_tail": [], "stray_stdout": [], "error": None})
    html = local_client(store).post("/connect/claude/test").text
    assert "answered with 24 tools in 1.8 s" in html
    web_setup(tmp_path, monkeypatch, connected=False)
    html = local_client(store).post("/connect/claude/test").text
    assert "isn't in Claude Desktop's settings file any more" in html and "Quit Claude Desktop" in html


def test_connect_route_reports_path_readback_restart_and_check(store, tmp_path, monkeypatch):
    p = web_setup(tmp_path, monkeypatch, connected=False)
    monkeypatch.setattr(integrations, "test_launch", lambda entry, timeout=30.0: {
        "ok": False, "seconds": 0.2, "tools": 0, "stderr_tail": ["Traceback boom"], "stray_stdout": [],
        "error": "It stopped right away (exit code 1), before it said hello."})
    html = local_client(store).post("/connect/claude").text
    assert str(p) in html and "read the file back" in html
    assert "Quit Claude Desktop completely" in html and "Settings &gt; Developer" in html
    assert "The check failed" in html and "Traceback boom" in html
