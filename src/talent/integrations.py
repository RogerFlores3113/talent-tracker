"""One-click hookup to AI desktop apps.

The app writes its own entry into Claude Desktop's and Codex's MCP config so a
recruiter never edits JSON or TOML. Existing config is kept, one backup is saved
next to it first (name.bak, kept from before the first connect), the file keeps its permissions,
and we only ever touch our own `talent-search` entry. If the app isn't installed
(its settings folder doesn't exist) we create nothing and say so.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tomllib
from dataclasses import dataclass
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


# Where to look once connected, per app.
NEXT_STEPS = {
    "Claude Desktop": "Restart Claude Desktop, then click + and Connectors to see Talent Search.",
    "Codex": "Restart Codex, then type /mcp to see Talent Search in the list.",
    "LM Studio": "Restart LM Studio, then open the Program tab on the right of a chat and turn on mcp/talent-search.",
}


def _not_installed(path: Path, app_name: str) -> dict[str, Any] | None:
    """The app keeps its settings in path.parent; if that folder is missing the app isn't set up here."""
    if path.parent.is_dir():
        return None
    return {"path": str(path), "installed": False, "backup": None,
            "message": f"{app_name} doesn't seem to be installed on this computer. Install it, open it once, "
                       "then try again."}


def _backup(path: Path, ours_already: bool = False) -> Path | None:
    """One backup next to the file (config.json.bak). When the file already has our entry, the
    backup from before we first connected is kept, so pressing Connect again never loses it."""
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.bak")
    if ours_already and dest.exists():
        return dest
    shutil.copy2(path, dest)  # copy2 keeps the original permissions
    return dest


def _write(path: Path, text: str) -> None:
    """Replace the file in one step, keeping its permissions (new files are owner-only)."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if os.name != "nt":
        os.chmod(tmp, mode)  # os.open's mode is filtered by the umask
    tmp.replace(path)


def _done(path: Path, backup: Path | None, app_name: str, **extra: Any) -> dict[str, Any]:
    return {"path": str(path), "installed": True, "backup": str(backup) if backup else None,
            "next_step": NEXT_STEPS.get(app_name, f"Restart {app_name} to load Talent Search."), **extra}


def _connect_json(path: Path, spec: LaunchSpec, app_name: str) -> dict[str, Any]:
    """Add or refresh our entry in an `mcpServers` JSON file, keeping everything else."""
    if missing := _not_installed(path, app_name):
        return missing
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
    backup = _backup(path, ours_already=SERVER_KEY in (config.get("mcpServers") or {}))
    servers = config.setdefault("mcpServers", {})
    already = servers.get(SERVER_KEY) == spec.as_json_entry()
    servers[SERVER_KEY] = spec.as_json_entry()
    _write(path, json.dumps(config, indent=2) + "\n")
    return _done(path, backup, app_name, already_connected=already)


def _json_connected(path: Path) -> bool:
    try:
        return SERVER_KEY in json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
    except (OSError, ValueError, AttributeError):
        return False


def connect_claude(path: Path | None = None, spec: LaunchSpec | None = None) -> dict[str, Any]:
    return _connect_json(path or claude_config_path(), spec or mcp_launch_spec(), "Claude Desktop")


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
    if missing := _not_installed(path, "Codex"):
        return missing
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if original.strip():
        try:
            tomllib.loads(original)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Codex's settings file isn't valid TOML, so we left it alone ({exc}).") from exc
    backup = _backup(path, ours_already=_strip_our_section(original) != original)
    base = _strip_our_section(original).rstrip()
    text = (base + "\n\n" if base else "") + codex_snippet(spec)
    tomllib.loads(text)  # never write something Codex can't read
    _write(path, text)
    return _done(path, backup, "Codex")


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
