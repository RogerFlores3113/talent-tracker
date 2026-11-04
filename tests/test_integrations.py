import json
import tomllib

import pytest

from talent import integrations
from talent.integrations import LaunchSpec

SPEC = LaunchSpec(r"C:\Program Files\Talent Search\talent-search-mcp.exe", ["mcp"], {"TALENT_DATA_DIR": r"C:\Users\r\data"})


def test_claude_creates_file(tmp_path):
    p = tmp_path / "Claude" / "claude_desktop_config.json"
    res = integrations.connect_claude(p, SPEC)
    cfg = json.loads(p.read_text())
    assert cfg["mcpServers"]["talent-search"]["command"].endswith("talent-search-mcp.exe")
    assert res["backup"] is None
    assert integrations.claude_connected(p)


def test_claude_preserves_other_settings_and_backs_up(tmp_path):
    p = tmp_path / "claude.json"
    p.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    res = integrations.connect_claude(p, SPEC)
    cfg = json.loads(p.read_text())
    assert cfg["theme"] == "dark" and "other" in cfg["mcpServers"] and "talent-search" in cfg["mcpServers"]
    assert res["backup"] and json.loads(open(res["backup"]).read())["theme"] == "dark"
    assert integrations.connect_claude(p, SPEC)["already_connected"] is True


def test_claude_refuses_to_touch_broken_json(tmp_path):
    p = tmp_path / "claude.json"
    p.write_text("{ not json")
    with pytest.raises(ValueError):
        integrations.connect_claude(p, SPEC)
    assert p.read_text() == "{ not json"


def test_codex_appends_then_replaces_our_section_only(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other"\n')
    integrations.connect_codex(p, SPEC)
    cfg = tomllib.loads(p.read_text())
    assert cfg["model"] == "gpt-5"
    assert cfg["mcp_servers"]["other"]["command"] == "other"
    ours = cfg["mcp_servers"]["talent-search"]
    assert ours["command"] == SPEC.command  # Windows backslashes survive
    assert ours["env"]["TALENT_DATA_DIR"] == SPEC.env["TALENT_DATA_DIR"]

    newer = LaunchSpec("/Applications/Talent Search.app/Contents/MacOS/Talent Search", ["mcp"], {})
    integrations.connect_codex(p, newer)
    text = p.read_text()
    assert text.count("[mcp_servers.talent-search]") == 1
    assert tomllib.loads(text)["mcp_servers"]["talent-search"]["command"] == newer.command
    assert integrations.codex_connected(p)


def test_codex_strips_our_subtables_too(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[mcp_servers.talent-search]\ncommand = "old"\n\n[mcp_servers.talent-search.env]\nA = "1"\n\n[profiles.x]\nmodel = "m"\n')
    integrations.connect_codex(p, SPEC)
    cfg = tomllib.loads(p.read_text())
    assert cfg["mcp_servers"]["talent-search"]["command"] == SPEC.command
    assert cfg["profiles"]["x"]["model"] == "m"


def test_codex_refuses_broken_toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("model = \n")
    with pytest.raises(ValueError):
        integrations.connect_codex(p, SPEC)


def test_dev_launch_spec_points_at_this_python(monkeypatch, tmp_path):
    spec = integrations.mcp_launch_spec()
    assert spec.args[-1] == "mcp"
    assert spec.env["TALENT_DATA_DIR"]  # absolute data dir so the AI app finds the same database


def test_generic_snippet_is_valid_json():
    assert "talent-search" in json.loads(integrations.generic_json_snippet(SPEC))["mcpServers"]
