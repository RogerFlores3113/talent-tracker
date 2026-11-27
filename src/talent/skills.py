"""Talent Search skills for AI apps (Claude Code, Codex, Claude Desktop).

A "skill" is a folder with a SKILL.md (YAML frontmatter with name and description, then instructions) that an
AI app reads to learn how to do a job well. We ship a small set in the repo's skills/ folder (bundled into the
packaged app).

Every connected app gets them with the connection: the MCP server lists them as playbooks in its instructions,
serves each one with get_playbook and offers each as a prompt (playbooks()). Nothing to add or upload, and they
follow the app to each new version. Copying them as files is an optional extra, where the apps look:

  Claude Code   ~/.claude/skills/<name>/SKILL.md      (personal skills)
  Codex         ~/.agents/skills/<name>/SKILL.md      (user skills)
  Folders the app installed are brought up to date when it starts (refresh_installed()). build_plugin() makes the
  same skills (and the starter slash commands from commands/) as a Claude plugin file for `talent install-skills --plugin`, and the repo carries a marketplace file
  at its root (.claude-plugin/), so `claude plugin marketplace add RogerFlores3113/talent-tracker` works too.

Formats checked against the apps' own docs:
  https://code.claude.com/docs/en/skills
  https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
  https://support.claude.com/en/articles/12512198-creating-custom-skills
  https://learn.chatgpt.com/docs/build-skills  (OpenAI's Codex skills page; developers.openai.com redirects to it)

Safety rules for what we write:
  * Every folder we install is named talent-search-*, so we never touch the user's own skills.
  * Every folder we create gets a small label file (the marker) that also records a fingerprint of each file we
    wrote. We only replace or remove folders that carry it.
  * A file the user edited since we wrote it (its fingerprint no longer matches) is never overwritten or deleted.
    It is kept and reported as "edited by you, kept".
  * A folder with the same name but no marker, or one that is a symlink, is left alone and reported.
  * Nothing is written until the user has seen the exact list of files and confirmed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from . import __version__

PREFIX = "talent-search-"
MARKER = ".installed-by-talent-search"
TARGETS = ("claude-code", "codex")
TARGET_LABELS = {"claude-code": "Claude Code", "codex": "Codex"}
PLUGIN_NAME = "talent-search"
PLUGIN_FILE = "talent-search.plugin"
MARKETPLACE_NAME = "talent-tracker"
REPO = "RogerFlores3113/talent-tracker"
PLUGIN_DESCRIPTION = ("Skills that teach your AI app to use Talent Search well: sourcing, triage, interviews, "
                      "outreach, pipeline reviews, feedback and decisions.")
PLUGIN_MANIFEST: dict[str, Any] = {
    "name": PLUGIN_NAME,
    "description": PLUGIN_DESCRIPTION,
    "author": {"name": "Talent Search"},
    "homepage": f"https://github.com/{REPO}",
    "keywords": ["recruiting", "hiring", "candidates", "talent-search"],
}
# Typed inside Claude Code (the app's Connect page shows these)
CLAUDE_CODE_SLASH_COMMANDS = (f"/plugin marketplace add {REPO}", f"/plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}")
CLAUDE_CODE_PLUGIN_COMMANDS = (f"claude plugin marketplace add {REPO}",
                               f"claude plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}")


# ------------------------------------------------------------------ where things are
def source_dir() -> Path:
    """The bundled skills/ folder: next to the app when packaged, the repo's skills/ when run from source."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
        return base / "skills"
    return Path(__file__).resolve().parents[2] / "skills"


def commands_dir() -> Path:
    """The bundled commands/ folder (starter slash commands), found the same way as skills/."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
        return base / "commands"
    return Path(__file__).resolve().parents[2] / "commands"


def home_dir() -> Path:
    """The user's home folder. TALENT_SKILLS_HOME overrides it (tests use this so they never touch the real one)."""
    override = os.environ.get("TALENT_SKILLS_HOME")
    return Path(override) if override else Path.home()


def target_dir(target: str) -> Path:
    if target == "claude-code":
        return home_dir() / ".claude" / "skills"
    if target == "codex":
        return home_dir() / ".agents" / "skills"
    raise ValueError(f"Unknown target: {target}")


def display_path(path: Path) -> str:
    """A path with the home folder written as ~, the way people expect to read it."""
    try:
        return "~/" + path.relative_to(home_dir()).as_posix()
    except ValueError:
        return str(path)


def skill_names(src: Path | None = None) -> list[str]:
    src = src or source_dir()
    if not src.is_dir():
        return []
    return sorted(p.name for p in src.iterdir() if p.is_dir() and p.name.startswith(PREFIX) and (p / "SKILL.md").is_file())


FRIENDLY = {
    "talent-search-source-for-a-role": ("Find people for a role", "Searches for new candidates for an open job and saves the good ones."),
    "talent-search-triage-candidates": ("Sort through candidates", "Reviews, searches, compares and moves the people you already have."),
    "talent-search-schedule-interviews": ("Plan interviews", "Logs, moves and plans interviews, including a full loop."),
    "talent-search-write-outreach": ("Write to candidates", "Drafts messages to candidates and logs that you sent them."),
    "talent-search-pipeline-review": ("Give a status update", "Sums up where each job stands, ready to paste to a client or manager."),
    "talent-search-interview-feedback-and-decide": ("Collect feedback and decide", "Gathers interviewer feedback and compares the finalists."),
}


def skill_info() -> list[dict[str, str]]:
    """Each skill with a friendly name and one line, for the Connect page."""
    out = []
    for n in skill_names():
        title, line = FRIENDLY.get(n, (n.removeprefix(PREFIX).replace("-", " ").capitalize(), ""))
        out.append({"folder": n, "title": title, "line": line})
    return out


def playbooks(src: Path | None = None) -> list[dict[str, str]]:
    """The skills as the MCP server hands them out: name, title, description (when to use it) and the instructions.
    Every connected app gets them this way, so nothing has to be added or uploaded."""
    src = src or source_dir()
    out = []
    for n in skill_names(src):
        text = (src / n / "SKILL.md").read_text(encoding="utf-8")
        head, body = "", text
        if text.startswith("---"):
            head, _, body = text[3:].partition("\n---")
        desc = next((ln.split(":", 1)[1].strip() for ln in head.splitlines() if ln.startswith("description:")), "")
        title = FRIENDLY.get(n, (n.removeprefix(PREFIX).replace("-", " ").capitalize(), ""))[0]
        out.append({"name": n.removeprefix(PREFIX), "title": title, "description": desc, "body": body.strip()})
    return out


def starter_commands(src: Path | None = None) -> list[dict[str, str]]:
    """The starter slash commands, sorted by name: name (file stem), description, argument_hint and body."""
    src = src or commands_dir()
    if not src.is_dir():
        return []
    out = []
    for path in sorted(src.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        head, body = "", text
        if text.startswith("---"):
            head, _, body = text[3:].partition("\n---")
        fields = {}
        for ln in head.splitlines():
            key, sep, val = ln.partition(":")
            if sep and key.strip() in ("description", "argument-hint"):
                fields[key.strip()] = val.strip().strip("\"'")
        out.append({"name": path.stem, "description": fields.get("description", ""),
                    "argument_hint": fields.get("argument-hint", ""), "body": body.strip()})
    return out


def _files_of(skill: str, src: Path | None = None) -> list[Path]:
    """Every file in one bundled skill, relative to the skill folder (SKILL.md first)."""
    root = (src or source_dir()) / skill
    rel = sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())
    rel.sort(key=lambda p: (p.as_posix() != "SKILL.md", p.as_posix()))
    return rel


# ------------------------------------------------------------------ marker
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str | None:
    """Fingerprint of a real file, or None when it is missing, unreadable or a symlink (never followed)."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return _sha(path.read_bytes())
    except OSError:
        return None


def _read_marker(folder: Path) -> dict[str, Any] | None:
    marker = folder / MARKER
    try:
        if folder.is_symlink() or marker.is_symlink() or not marker.is_file():
            return None
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("app") == "talent-search" else None


def _is_ours(folder: Path) -> bool:
    return folder.name.startswith(PREFIX) and _read_marker(folder) is not None


def _recorded(folder: Path) -> dict[str, str]:
    """The fingerprints the marker holds: {relative path: sha256}. An older marker holds none."""
    files = (_read_marker(folder) or {}).get("files")
    return {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {}


def _edited(folder: Path, rel: str, recorded: dict[str, str]) -> bool:
    """True when this file is there and is not what we wrote. A missing file isn't edited. With no recorded
    fingerprint (an older marker) a file that differs from the bundled one counts as edited, so it is kept."""
    dest = folder / rel
    if not (dest.exists() or dest.is_symlink()):
        return False
    have = _file_sha(dest)
    if have is None:
        return True  # a symlink or something odd where our file should be: not ours to replace
    want = recorded.get(rel)
    if want is None:
        want = _file_sha(source_dir() / folder.name / rel)
    return have != want


# ------------------------------------------------------------------ plan / install / remove
def plan_install(target: str) -> dict[str, Any]:
    """What installing would do, without writing anything: the files, the ones the user edited (kept as they
    are), and any folder we'd leave alone."""
    base = target_dir(target)
    items, files, skipped, edited = [], [], [], []
    labels: dict[str, str] = {}  # each file: "new", "updated" or "unchanged"
    for name in skill_names():
        folder = base / name
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            skipped.append({"folder": name, "path": display_path(folder), "reason": "something else is already there"})
            continue
        if folder.exists() and not _is_ours(folder):
            skipped.append({"folder": name, "path": display_path(folder),
                            "reason": "a folder with this name is already there and Talent Search didn't make it"})
            continue
        action = "update" if folder.exists() else "new"
        rec = _recorded(folder) if folder.exists() else {}
        keep = [rel for rel in _files_of(name) if folder.exists() and _edited(folder, rel.as_posix(), rec)]
        kept = {r.as_posix() for r in keep}
        paths = [folder / rel for rel in _files_of(name) if rel.as_posix() not in kept] + [folder / MARKER]
        kept_paths = [display_path(folder / r) for r in keep]
        changed_any = False
        for rel in _files_of(name):
            if rel.as_posix() in kept:
                continue
            dest = folder / rel
            here = _file_sha(dest)
            label = "new" if here is None else "unchanged" if here == _sha((source_dir() / name / rel).read_bytes()) else "updated"
            changed_any = changed_any or label != "unchanged"
            labels[display_path(dest)] = label
        mark = _read_marker(folder) if folder.exists() else None
        labels[display_path(folder / MARKER)] = (
            "unchanged" if mark and mark.get("version") == __version__ and not changed_any and not keep
            else "new" if not folder.exists() else "updated")
        items.append({"folder": name, "action": action, "files": [display_path(p) for p in paths],
                      "edited": kept_paths})
        files.extend(display_path(p) for p in paths)
        edited.extend(kept_paths)
    return {"target": target, "label": TARGET_LABELS[target], "base": display_path(base), "items": items,
            "files": files, "skipped": skipped, "edited": edited, "version": __version__, "labels": labels}


def install(target: str) -> dict[str, Any]:
    """Copy our skills into the app's folder. Only creates or updates folders we made, never overwrites a file the
    user edited, and reports the rest."""
    plan = plan_install(target)
    base = target_dir(target)
    src = source_dir()
    written: list[str] = []
    for item in plan["items"]:
        name = item["folder"]
        folder = base / name
        old = _recorded(folder) if folder.exists() else {}
        kept = {p.split(name + "/", 1)[1] for p in item["edited"]}
        folder.mkdir(parents=True, exist_ok=True)
        fingerprints: dict[str, str] = {}
        for rel in _files_of(name):
            key = rel.as_posix()
            data = (src / name / rel).read_bytes()
            if key in kept:  # the user's version stays; the marker keeps what we last wrote, so it stays "edited"
                fingerprints[key] = old.get(key) or _sha(data)
                continue
            dest = folder / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            fingerprints[key] = _sha(data)
            written.append(display_path(dest))
        marker = folder / MARKER
        marker.write_text(json.dumps({"app": "talent-search", "version": __version__, "skill": name,
                                      "files": fingerprints}, sort_keys=True) + "\n", encoding="utf-8")
        written.append(display_path(marker))
    return {**plan, "written": written}


def refresh_installed() -> list[str]:
    """When the app starts: bring the skill folders it installed before up to this version (new skills added,
    files the user edited kept), so an update never leaves old skills behind. Apps it never installed into are
    left alone. Returns the labels of the apps it updated."""
    done = []
    for t in TARGETS:
        if not _our_folders(t):
            continue
        plan = plan_install(t)
        stale = any(v != "unchanged" for k, v in plan["labels"].items() if not k.endswith(MARKER)) or any(
            (_read_marker(target_dir(t) / i["folder"]) or {}).get("version") != __version__ for i in plan["items"])
        if stale:  # a file to add or update, or a marker from an older version (an edited file alone is not stale)
            install(t)
            done.append(TARGET_LABELS[t])
    return done


def _our_folders(target: str) -> list[Path]:
    base = target_dir(target)
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and _is_ours(p))


def _split_folder(folder: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """(files we wrote and can delete, files the user edited, other files in the folder that are not ours)."""
    rec = _recorded(folder)
    bundled = {r.as_posix() for r in _files_of(folder.name)} if folder.name in set(skill_names()) else set()
    ours = set(rec) | bundled | {"SKILL.md"}
    mine, edited, extra = [], [], []
    for p in sorted(folder.rglob("*")):
        if p.is_dir() and not p.is_symlink():
            continue
        rel = p.relative_to(folder).as_posix()
        if rel == MARKER:
            continue
        if rel not in ours:
            extra.append(p)
        elif _edited(folder, rel, rec):
            edited.append(p)
        else:
            mine.append(p)
    return mine, edited, extra


def plan_remove(target: str) -> dict[str, Any]:
    """What removing would do: the folders that go entirely, the files kept because the user edited or added
    them (and the folders that stay because of that)."""
    folders, files, left, edited = [], [], [], []
    for f in _our_folders(target):
        mine, ed, extra = _split_folder(f)
        files.extend(display_path(p) for p in mine)
        edited.extend(display_path(p) for p in ed)
        if ed or extra:
            left.append({"folder": display_path(f), "files": [display_path(p) for p in ed + extra]})
        else:
            folders.append(display_path(f))
    return {"target": target, "label": TARGET_LABELS[target], "base": display_path(target_dir(target)),
            "folders": folders, "files": files, "edited": edited, "left": left}


def remove(target: str) -> dict[str, Any]:
    """Delete only files we wrote and that are unchanged, inside folders that carry our marker. A file the user
    edited, or added, is kept. Its folder stays and loses the marker, so it is the user's own from then on, and the
    result says so."""
    removed, kept, edited = [], [], []
    for folder in _our_folders(target):
        mine, ed, extra = _split_folder(folder)
        for p in mine:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            (folder / MARKER).unlink()  # what is left, if anything, is the user's own now
        except OSError:
            pass
        for d in sorted((p for p in folder.rglob("*") if p.is_dir() and not p.is_symlink()), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        if ed or extra:
            kept.append(display_path(folder))
            edited.extend(display_path(p) for p in ed)
            continue
        try:
            folder.rmdir()
            removed.append(display_path(folder))
        except OSError:
            kept.append(display_path(folder))
    return {"target": target, "label": TARGET_LABELS[target], "removed": removed, "kept": kept, "edited": edited}


def status(target: str) -> dict[str, Any]:
    """Installed state for the Connect page."""
    ours = _our_folders(target)
    versions = sorted({str((_read_marker(f) or {}).get("version") or "") for f in ours} - {""})
    have = {f.name for f in ours}
    want = set(skill_names())
    plan = plan_install(target)
    return {"target": target, "label": TARGET_LABELS[target], "base": display_path(target_dir(target)),
            "installed": sorted(have), "count": len(have), "total": len(want), "versions": versions,
            "current": bool(have) and have >= want and versions == [__version__],
            "missing": sorted(want - have), "skipped": len(plan["skipped"]),
            "edited": len(plan["edited"])}


# ------------------------------------------------------------------ the Claude app plugin
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def _zip_add(z: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    z.writestr(info, data)


def build_plugin() -> bytes:
    """The Claude app plugin: one zip with .claude-plugin/plugin.json, skills/<name>/... for every skill and
    commands/<name>.md for every starter command.
    The same bytes every time (fixed dates, sorted names). No version field, so versions are kept elsewhere."""
    src = source_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        _zip_add(z, ".claude-plugin/plugin.json",
                 (json.dumps(PLUGIN_MANIFEST, indent=2) + "\n").encode("utf-8"))
        for name in skill_names():
            for rel in _files_of(name):
                _zip_add(z, f"skills/{name}/{rel.as_posix()}", (src / name / rel).read_bytes())
        cmds = commands_dir()
        for c in starter_commands(cmds):
            _zip_add(z, f"commands/{c['name']}.md", (cmds / f"{c['name']}.md").read_bytes())
    return buf.getvalue()


def downloads_dir() -> Path:
    """The person's Downloads folder. TALENT_DOWNLOADS_DIR overrides it (tests). Falls back to the app's data folder."""
    override = os.environ.get("TALENT_DOWNLOADS_DIR")
    if override:
        return Path(override)
    try:
        from platformdirs import user_downloads_dir

        d = Path(user_downloads_dir())
        if d.is_dir():
            return d
    except Exception:
        pass
    from .paths import data_dir

    return data_dir()


def write_plugin(path: Path | None = None) -> Path:
    """Write the plugin file. With no path, into Downloads as talent-search.plugin. A folder path gets that name."""
    path = Path(path) if path else downloads_dir() / PLUGIN_FILE
    if path.is_dir():
        path = path / PLUGIN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_plugin())
    return path


def _powershell_store_family(name: str) -> str:
    cmd = ["powershell", "-NoProfile", "-Command", f"(Get-AppxPackage -Name {name}).PackageFamilyName"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout


def claude_app_command(platform: str | None = None, env: dict[str, str] | None = None, run=None) -> list[str] | None:
    """How to start the Claude desktop app, or None when it isn't found (or on Linux, which has none).
    run(name) returns PowerShell's text for the Store package family; injectable for tests."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform == "darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            if (base / "Claude.app").is_dir():
                return ["open", "-a", "Claude"]
    elif platform == "win32":
        local = env.get("LOCALAPPDATA")
        if local:
            for exe in (Path(local) / "AnthropicClaude" / "Claude.exe", Path(local) / "Programs" / "Claude" / "Claude.exe"):
                if exe.is_file():
                    return [str(exe)]
        try:  # the Microsoft Store build has no fixed exe path, but it opens by its package family and app id
            family = (run or _powershell_store_family)("Claude").strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if family:
            return ["explorer.exe", f"shell:AppsFolder\\{family}!Claude"]
    return None


def detect(target: str) -> bool:
    """Is that app on this computer? Its settings folder exists or its command is on the PATH. With
    TALENT_SKILLS_HOME set (tests) only the folder counts, so a real install on the machine can't leak in."""
    folder = home_dir() / (".claude" if target == "claude-code" else ".codex")
    if folder.is_dir():
        return True
    if os.environ.get("TALENT_SKILLS_HOME"):
        return False
    return shutil.which("claude" if target == "claude-code" else "codex") is not None


def detected_targets() -> list[str]:
    return [t for t in TARGETS if detect(t)]


def plan_all(targets: list[str] | None = None) -> dict[str, Any]:
    """One combined preview. targets None means every app found on this computer."""
    chosen = [t for t in TARGETS if t in (targets if targets is not None else detected_targets())]
    plans = [plan_install(t) for t in chosen]
    counts = {k: sum(1 for p in plans for v in p["labels"].values() if v == k) for k in ("new", "updated", "unchanged")}
    return {"targets": chosen, "plans": plans, "counts": counts,
            "missing": [{"target": t, "label": TARGET_LABELS[t]} for t in TARGETS if not detect(t)],
            "files": [f for p in plans for f in p["files"]],
            "edited": [f for p in plans for f in p["edited"]],
            "skipped": [s for p in plans for s in p["skipped"]]}


def install_all(targets: list[str]) -> dict[str, Any]:
    chosen = [t for t in TARGETS if t in targets]
    results = [install(t) for t in chosen]
    return {"targets": chosen, "results": results, "written": [w for r in results for w in r["written"]],
            "skipped": [s for r in results for s in r["skipped"]], "edited": [e for r in results for e in r["edited"]]}


def add_everywhere() -> dict[str, Any]:
    """The optional 'Add skills': also install the skills as files into every app found here (only talent-search-*
    folders we made, files the person edited are kept). Connected apps already get them as playbooks."""
    found = detected_targets()
    plan = plan_all(found)
    current = bool(plan["plans"]) and plan["counts"]["new"] == 0 and plan["counts"]["updated"] == 0
    done = install_all(found) if found else {"results": [], "skipped": [], "edited": [], "written": []}
    apps: list[dict[str, Any]] = []
    for t in TARGETS:
        if t not in found:
            apps.append({"target": t, "label": TARGET_LABELS[t], "state": "missing",
                         "line": "not found on this computer, so nothing was added."})
            continue
        p = next(x for x in plan["plans"] if x["target"] == t)
        n = sum(1 for v in p["labels"].values() if v != "unchanged")
        line = ("already up to date." if n == 0 else
                f"added {len(p['items'])} skills to your {TARGET_LABELS[t]} skills folder ({display_path(target_dir(t))}).")
        apps.append({"target": t, "label": TARGET_LABELS[t], "state": "current" if n == 0 else "added", "line": line})
    return {"apps": apps, "up_to_date": current, "found": bool(found), "skipped": done["skipped"],
            "edited": done["edited"]}
