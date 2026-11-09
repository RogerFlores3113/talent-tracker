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
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
from collections import deque
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


def quit_instruction(platform: str | None = None) -> str:
    """How to really quit Claude Desktop. Closing its window leaves it running, so it never rereads its settings."""
    platform = platform or sys.platform
    if platform == "darwin":
        return ("Quit Claude Desktop completely (Claude menu > Quit Claude, or Cmd+Q). Closing the window isn't "
                "enough, because Claude keeps running in the menu bar. Then open it again.")
    if platform == "win32":
        return ("Quit Claude Desktop completely (right-click the Claude icon by the clock and choose Quit). Closing "
                "the window isn't enough, because Claude keeps running in the system tray. Then open it again.")
    return "Quit Claude Desktop completely, then open it again. Closing the window isn't enough."


def claude_where_to_look() -> str:
    return ("Then open Settings > Developer: Talent Search should be listed there as running. Its tools are under "
            "the + button (Search and tools) in a chat.")


# Where to look once connected, per app.
NEXT_STEPS = {
    "Claude Desktop": f"{quit_instruction()} {claude_where_to_look()}",
    "Codex": "Restart Codex, then type /mcp to see Talent Search in the list.",
    "LM Studio": "Restart LM Studio, then open the Program tab on the right of a chat and turn on mcp/talent-search.",
}


def _next_step(app_name: str) -> str:
    if app_name == "Claude Desktop":
        return f"{quit_instruction()} {claude_where_to_look()}"
    return NEXT_STEPS.get(app_name, f"Restart {app_name} to load Talent Search.")


# ------------------------------------------------- where is the app running from
def launch_problem(spec: LaunchSpec | None = None, *, platform: str | None = None,
                   frozen: bool | None = None, exe: str | None = None) -> str | None:
    """A plain-English problem with where the app runs from (so another app couldn't start it later), or None."""
    platform = platform or sys.platform
    frozen = is_frozen() if frozen is None else frozen
    if not frozen:
        return None
    if platform == "darwin":
        where = (exe or (spec.command if spec else None) or sys.executable).replace("\\", "/")
        if "/AppTranslocation/" in where:
            return ("macOS is running Talent Search from a temporary copy, so Claude Desktop wouldn't be able to "
                    "start it later. Quit Talent Search, drag it into your Applications folder in Finder, open it "
                    "from there, then press Connect again.")
        if where.startswith("/Volumes/"):
            return ("Talent Search is running straight from a disk image or drive, so Claude Desktop wouldn't be "
                    "able to start it later. Quit Talent Search, drag it into your Applications folder in Finder, "
                    "open it from there, then press Connect again.")
    elif platform == "win32":
        main_exe = Path(exe or sys.executable)
        if not main_exe.with_name("talent-search-mcp.exe").exists():
            return ("This copy of Talent Search is incomplete: talent-search-mcp.exe is missing next to it. "
                    "Download it again and unzip the whole folder before opening it.")
    return None


def quarantine_note(platform: str | None = None, exe: str | None = None) -> str | None:
    """Best effort (Mac, frozen): downloaded apps carry a quarantine mark that can stop Claude Desktop starting them."""
    platform = platform or sys.platform
    if platform != "darwin" or not (is_frozen() or exe):
        return None
    path = exe or sys.executable
    bundle = path.split(".app/")[0] + ".app" if ".app/" in path else path
    try:
        done = subprocess.run(["xattr", "-p", "com.apple.quarantine", bundle], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return ("If Claude Desktop says Talent Search failed to start, open Talent Search once from Applications "
            "with right-click > Open first.")


def app_running(app: str = "claude") -> bool | None:
    """Is that app running right now? Best effort, never raises. None when we can't tell."""
    try:
        if app != "claude":
            return None
        if sys.platform == "darwin":
            cmd = ["pgrep", "-x", "Claude"]
        elif sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe", "/NH"], capture_output=True,
                                 text=True, timeout=5).stdout
            return "claude.exe" in out.lower()
        else:
            return None  # no official Claude Desktop here, and a process called "claude" is usually Claude Code
        return subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


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
            "ok": True, "next_step": _next_step(app_name), **extra}


def _refused(path: Path, problem: str) -> dict[str, Any]:
    return {"path": str(path), "installed": True, "ok": False, "problem": problem, "backup": None}


def _connect_json(path: Path, spec: LaunchSpec, app_name: str) -> dict[str, Any]:
    """Add or refresh our entry in an `mcpServers` JSON file, keeping everything else. Reads it back to check."""
    if missing := _not_installed(path, app_name):
        return missing
    if problem := launch_problem(spec):
        return _refused(path, problem)
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
    try:
        back = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_KEY]
    except (OSError, ValueError, KeyError, TypeError):
        back = None
    if back != spec.as_json_entry():
        raise ValueError(f"We wrote {app_name}'s settings file, but when we read it back Talent Search's entry "
                         "wasn't what we meant to write. Nothing more was changed. Use the settings under "
                         "\u201cBy hand\u201d instead.")
    return _done(path, backup, app_name, already_connected=already, read_back=True, entry=spec.as_json_entry(),
                 running=app_running("claude") if app_name == "Claude Desktop" else None,
                 quarantine=quarantine_note() if app_name == "Claude Desktop" else None)


def _json_entry(path: Path) -> dict[str, Any] | None:
    """Our entry in an `mcpServers` JSON file, or None (missing file, broken JSON, no entry)."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}).get(SERVER_KEY)
    except (OSError, ValueError, AttributeError):
        return None
    return entry if isinstance(entry, dict) else None


def connect_claude(path: Path | None = None, spec: LaunchSpec | None = None) -> dict[str, Any]:
    return _connect_json(path or claude_config_path(), spec or mcp_launch_spec(), "Claude Desktop")


def claude_connected(path: Path | None = None) -> bool:
    """Our entry is there and its command exists and looks right."""
    entry = _json_entry(path or claude_config_path())
    return entry is not None and not check_entry(entry)


# -------------------------------------------------------------- LM Studio
def lmstudio_config_path() -> Path:
    """LM Studio (0.3.17+) reads MCP servers from ~/.lmstudio/mcp.json, same shape as Claude's."""
    home = os.environ.get("LMSTUDIO_HOME")
    return (Path(home) if home else Path.home() / ".lmstudio") / "mcp.json"


def connect_lmstudio(path: Path | None = None, spec: LaunchSpec | None = None) -> dict[str, Any]:
    return _connect_json(path or lmstudio_config_path(), spec or mcp_launch_spec(), "LM Studio")


def lmstudio_connected(path: Path | None = None) -> bool:
    entry = _json_entry(path or lmstudio_config_path())
    return entry is not None and not check_entry(entry)


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
    if problem := launch_problem(spec or mcp_launch_spec()):
        return _refused(path, problem)
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
    want = (spec or mcp_launch_spec()).as_json_entry()
    try:
        back = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"][SERVER_KEY]
    except (OSError, ValueError, KeyError):
        back = None
    if not back or {k: back.get(k) for k in ("command", "args", "env")} != want:
        raise ValueError("We wrote Codex's settings file, but when we read it back Talent Search's entry wasn't "
                         "what we meant to write. Use the settings under \u201cBy hand\u201d instead.")
    return _done(path, backup, "Codex", read_back=True, entry=want)


def _codex_entry(path: Path) -> dict[str, Any] | None:
    try:
        entry = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {}).get(SERVER_KEY)
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return None
    return dict(entry) if isinstance(entry, dict) else None


def codex_connected(path: Path | None = None) -> bool:
    entry = _codex_entry(path or codex_config_path())
    return entry is not None and not check_entry(entry)


def generic_json_snippet(spec: LaunchSpec | None = None) -> str:
    """For any other MCP app that uses the common `mcpServers` JSON shape (Cursor, LM Studio...)."""
    spec = spec or mcp_launch_spec()
    return json.dumps({"mcpServers": {SERVER_KEY: spec.as_json_entry()}}, indent=2)


# ------------------------------------------------------- checking an entry
def check_entry(entry: dict[str, Any]) -> list[str]:
    """Plain-English problems with a config entry (ours, as found in an app's settings file)."""
    problems: list[str] = []
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        problems.append("The entry has no command to start.")
    else:
        looks_abs = os.path.isabs(command) or bool(re.match(r"^[A-Za-z]:[\\/]", command))
        if not looks_abs:
            problems.append(f"The command isn't a full path ({command}), so the app may not find it.")
        elif not os.path.isfile(command):
            problems.append(f"The program isn't there any more: {command}")
        elif not os.access(command, os.X_OK):
            problems.append(f"The program can't be run (no permission): {command}")
    if not isinstance(entry.get("args"), list):
        problems.append("The entry's arguments are missing or not a list.")
    env = entry.get("env")
    folder = env.get("TALENT_DATA_DIR") if isinstance(env, dict) else None
    if not folder:
        problems.append("The entry doesn't say which data folder to use (TALENT_DATA_DIR).")
    else:
        try:
            same = Path(folder).resolve() == data_dir().resolve()
        except OSError:
            same = False
        if not same:
            problems.append(f"The entry uses a different data folder than this app ({folder}).")
    return problems


_MINIMAL_KEYS = ("HOME", "USERPROFILE", "USER", "USERNAME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "APPDATA",
                 "LOCALAPPDATA")


def _minimal_env(extra: dict[str, Any] | None) -> dict[str, str]:
    """The small environment a desktop app hands its servers (no shell profile, no venv, short PATH)."""
    env = {k: os.environ[k] for k in _MINIMAL_KEYS if k in os.environ}
    if os.name != "nt":
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env.update({str(k): str(v) for k, v in (extra or {}).items()})
    return env


def _stop(proc: subprocess.Popen) -> None:
    for stream in (proc.stdin,):
        try:
            if stream:
                stream.close()
        except OSError:
            pass
    if proc.poll() is None:
        try:
            proc.kill() if os.name == "nt" else proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass


def test_launch(entry: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """Start an entry the way a desktop app does and speak MCP to it by hand. Never raises."""
    out: dict[str, Any] = {"ok": False, "seconds": 0.0, "server_name": None, "server_version": None, "tools": 0,
                           "error": None, "stderr_tail": [], "stray_stdout": []}
    started = time.monotonic()
    command = entry.get("command") if isinstance(entry, dict) else None
    if not isinstance(command, str) or not command:
        out["error"] = "The entry has no command to start."
        return out
    argv = [command, *[str(a) for a in (entry.get("args") or [])]]
    stderr_lines: deque[str] = deque(maxlen=200)
    lines: queue.Queue[str | None] = queue.Queue()
    proc: subprocess.Popen | None = None

    def finish() -> dict[str, Any]:
        out["seconds"] = round(time.monotonic() - started, 2)
        out["stderr_tail"] = list(stderr_lines)[-20:]
        return out

    try:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=str(Path.home()), env=_minimal_env(entry.get("env")), shell=False,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1)
        except FileNotFoundError:
            out["error"] = f"The program isn't there any more: {command}"
            return finish()
        except PermissionError:
            out["error"] = f"The program can't be run (no permission): {command}"
            return finish()
        except OSError as exc:
            out["error"] = f"The program couldn't be started: {exc}"
            return finish()

        def pump_out() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    lines.put(line.rstrip("\r\n"))
            except (OSError, ValueError):
                pass
            lines.put(None)

        def pump_err() -> None:
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    stderr_lines.append(line.rstrip("\r\n"))
            except (OSError, ValueError):
                pass

        threading.Thread(target=pump_out, daemon=True).start()
        threading.Thread(target=pump_err, daemon=True).start()
        deadline = started + timeout

        def send(message: dict[str, Any]) -> bool:
            try:
                proc.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
                proc.stdin.flush()  # type: ignore[union-attr]
                return True
            except (OSError, ValueError):
                return False

        def failure(waiting_for: str) -> str:
            code = proc.poll()
            if code is None:
                try:
                    code = proc.wait(timeout=1)
                except subprocess.SubprocessError:
                    code = None
            if code is not None:
                return f"It stopped right away (exit code {code}), before it said hello."
            msg = f"It started but didn't answer within {timeout:g} seconds ({waiting_for})."
            if out["stray_stdout"]:
                msg += (" It printed text that isn't part of the protocol, which gets in Claude Desktop's way: "
                        + out["stray_stdout"][0][:160])
            return msg

        def reply_for(request_id: int, waiting_for: str) -> dict[str, Any] | None:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    out["error"] = failure(waiting_for)
                    return None
                try:
                    line = lines.get(timeout=min(left, 0.5))
                except queue.Empty:
                    continue
                if line is None:
                    out["error"] = failure(waiting_for)
                    return None
                try:
                    msg = json.loads(line)
                except ValueError:
                    if line.strip():
                        out["stray_stdout"].append(line)
                    continue
                if isinstance(msg, dict) and msg.get("id") == request_id:
                    if "error" in msg:
                        out["error"] = f"The program answered with an error: {msg['error']}"
                        return None
                    return msg.get("result") or {}

        if not send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "talent-search-check", "version": "1"}}}):
            out["error"] = failure("starting up")
            return finish()
        init = reply_for(1, "saying hello")
        if init is None:
            return finish()
        info = init.get("serverInfo") or {}
        out["server_name"], out["server_version"] = info.get("name"), info.get("version")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = reply_for(2, "listing its tools")
        if tools is None:
            return finish()
        out["tools"] = len(tools.get("tools") or [])
        out["ok"] = out["tools"] > 0
        if not out["ok"]:
            out["error"] = "It started and answered, but it offered no tools."
        return finish()
    except Exception as exc:  # never let a check crash the app
        out["error"] = f"The check itself hit a problem: {exc}"
        return finish()
    finally:
        if proc is not None:
            _stop(proc)


def result_line(result: dict[str, Any]) -> str:
    """One sentence for a test_launch result."""
    if result.get("ok"):
        return (f"Checked: Talent Search started and answered with {result['tools']} tools in "
                f"{result['seconds']:.1f} s.")
    return f"The check failed. {result.get('error') or 'Talent Search did not answer.'}"


# ------------------------------------------------------------- Claude's logs
def claude_logs_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Claude"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "logs"
    return Path.home() / ".config" / "Claude" / "logs"


def claude_log_tail(lines: int = 15) -> list[str]:
    """The end of Claude Desktop's own log for our server, if it wrote one. Never raises."""
    try:
        folder = claude_logs_dir()
        own = folder / f"mcp-server-{SERVER_KEY}.log"
        if own.is_file():
            return own.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        general = folder / "mcp.log"
        if general.is_file():
            hits = [ln for ln in general.read_text(encoding="utf-8", errors="replace").splitlines()
                    if SERVER_KEY in ln]
            return hits[-lines:]
    except OSError:
        pass
    return []


# --------------------------------------------------------------- the whole picture
def _config_path_for(target: str) -> Path:
    return {"claude": claude_config_path, "lmstudio": lmstudio_config_path,
            "codex": codex_config_path}[target]()


def entry_for(target: str, path: Path | None = None) -> dict[str, Any] | None:
    """Our entry as found in that app's settings file, as a dict with command/args/env. None if absent."""
    path = path or _config_path_for(target)
    return _codex_entry(path) if target == "codex" else _json_entry(path)


def claude_status(path: Path | None = None, spec: LaunchSpec | None = None) -> dict[str, Any]:
    """Everything the page and the CLI say about Claude Desktop, gathered in one place."""
    path = path or claude_config_path()
    spec = spec or mcp_launch_spec()
    entry = _json_entry(path)
    others: list[str] = []
    try:
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
        others = sorted(k for k in servers if k != SERVER_KEY)
    except (OSError, ValueError, AttributeError):
        pass
    problems = check_entry(entry) if entry is not None else []
    return {
        "config_path": str(path), "config_exists": path.exists(), "installed": path.parent.is_dir(),
        "entry": entry, "entry_problems": problems,
        "matches_this_app": entry == spec.as_json_entry() if entry is not None else False,
        "launch_problem": launch_problem(spec), "claude_running": app_running("claude"),
        "other_servers": others, "log_tail": claude_log_tail(),
        "connected": entry is not None and not problems,
    }


# ---------------------------------------------------------- self-repair at start
def repair_stale_entries() -> list[str]:
    """If an AI app has our entry but its program is gone (the app was moved or updated), point it at this
    copy. Never adds an entry that isn't there, and never when this copy runs from a temporary place."""
    fixed: list[str] = []
    if launch_problem():
        return fixed
    spec = mcp_launch_spec()
    for target, writer in (("claude", connect_claude), ("lmstudio", connect_lmstudio), ("codex", connect_codex)):
        try:
            entry = entry_for(target)
            command = entry.get("command") if entry else None
            if not isinstance(command, str) or os.path.exists(command):
                continue
            if writer(spec=spec).get("ok"):
                fixed.append(target)
        except Exception:  # starting the app must never fail because of this
            continue
    return fixed
