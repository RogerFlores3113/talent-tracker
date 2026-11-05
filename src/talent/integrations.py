"""One-click hookup to AI desktop apps.

The app writes its own entry into Claude Desktop's and Codex's MCP config so a
recruiter never edits JSON or TOML. Existing config is kept, a backup is saved
next to it first, and we only ever touch our own `talent-search` entry.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import data_dir, is_frozen

SERVER_KEY = "talent-search"


@dataclass
class LaunchSpec:
    command: str
    args: list[str]
    env: dict[str, str]

    def as_json_entry(self) -> dict[str, Any]:
        return {"command": self.command, "args": self.args, "env": self.env}


def mcp_launch_spec() -> LaunchSpec:
    """How an AI app should start our MCP server on this machine."""
    env = {"TALENT_DATA_DIR": str(data_dir().resolve())}
    if is_frozen():
        exe = Path(sys.executable)
        if sys.platform == "win32":
            # Windows ships a separate console build for stdio; the main exe is windowed.
            console = exe.with_name("talent-search-mcp.exe")
            return LaunchSpec(str(console if console.exists() else exe), ["mcp"], env)
        return LaunchSpec(str(exe), ["mcp"], env)
    return LaunchSpec(sys.executable, ["-m", "talent", "mcp"], env)


# ----------------------------------------------------------------- Claude
def claude_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, dest)
    return dest


def _connect_json(path: Path, spec: LaunchSpec, app_name: str) -> dict[str, Any]:
    """Add or refresh our entry in an `mcpServers` JSON file, keeping everything else."""
    config: dict[str, Any] = {}
    if path.exists() and path.read_text(encoding="utf-8").strip():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{app_name}'s settings file isn't valid JSON, so we left it alone ({exc.msg}, line {exc.lineno})."
            ) from exc
        if not isinstance(config, dict):
            raise ValueError(f"{app_name}'s settings file has an unexpected shape, so we left it alone.")
    backup = _backup(path)
    servers = config.setdefault("mcpServers", {})
    already = servers.get(SERVER_KEY) == spec.as_json_entry()
    servers[SERVER_KEY] = spec.as_json_entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return {"path": str(path), "backup": str(backup) if backup else None, "already_connected": already}


def _json_connected(path: Path) -> bool:
    try:
        return SERVER_KEY in json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
    except (OSError, ValueError, AttributeError):
        return False


def connect_claude(path: Path | None = None, spec: LaunchSpec | None = None) -> dict[str, Any]:
    return _connect_json(path or claude_config_path(), spec or mcp_launch_spec(), "Claude")


def claude_connected(path: Path | None = None) -> bool:
    return _json_connected(path or claude_config_path())


# -------------------------------------------------------------- LM Studio
def lmstudio_config_path() -> Path:
    """LM Studio (0.3.17+) reads MCP servers from ~/.lmstudio/mcp.json, same shape as Claude's."""
    home = os.environ.get("LMSTUDIO_HOME")
    return (Path(home) if home else Path.home() / ".lmstudio") / "mcp.json"


def connect_lmstudio(path: Path | None = None, spec: LaunchSpec | None = None) -> dict[str, Any]:
    return _connect_json(path or lmstudio_config_path(), spec or mcp_launch_spec(), "LM Studio")


def lmstudio_connected(path: Path | None = None) -> bool:
    return _json_connected(path or lmstudio_config_path())


# ------------------------------------------------------------------ Codex
def codex_config_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "config.toml"


def codex_snippet(spec: LaunchSpec | None = None) -> str:
    spec = spec or mcp_launch_spec()
    # JSON string escaping is valid TOML basic-string escaping (handles Windows backslashes).
    args = ", ".join(json.dumps(a) for a in spec.args)
    env = ", ".join(f"{k} = {json.dumps(v)}" for k, v in spec.env.items())
    return (
        f"[mcp_servers.{SERVER_KEY}]\n"
        f"command = {json.dumps(spec.command)}\n"
        f"args = [{args}]\n"
        f"env = {{ {env} }}\n"
    )


# Matches our table and any sub-table of it, e.g. [mcp_servers.talent-search.env].
_SECTION_RE = re.compile(rf"^\[mcp_servers\.(?:{re.escape(SERVER_KEY)}|\"{re.escape(SERVER_KEY)}\")(?:\.[^\]]+)?\]\s*$")


def _strip_our_section(text: str) -> str:
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if _SECTION_RE.match(stripped):
            skipping = True
            continue
        if skipping and stripped.startswith("["):
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


def connect_codex(path: Path | None = None, spec: LaunchSpec | None = None) -> dict[str, Any]:
    path = path or codex_config_path()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if original.strip():
        try:
            tomllib.loads(original)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Codex's settings file isn't valid TOML, so we left it alone ({exc}).") from exc
    backup = _backup(path)
    base = _strip_our_section(original).rstrip()
    text = (base + "\n\n" if base else "") + codex_snippet(spec)
    tomllib.loads(text)  # never write something Codex can't read
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return {"path": str(path), "backup": str(backup) if backup else None}


def codex_connected(path: Path | None = None) -> bool:
    path = path or codex_config_path()
    try:
        return SERVER_KEY in tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {})
    except (OSError, tomllib.TOMLDecodeError):
        return False


def generic_json_snippet(spec: LaunchSpec | None = None) -> str:
    """For any other MCP app that uses the common `mcpServers` JSON shape (Cursor, LM Studio...)."""
    spec = spec or mcp_launch_spec()
    return json.dumps({"mcpServers": {SERVER_KEY: spec.as_json_entry()}}, indent=2)
