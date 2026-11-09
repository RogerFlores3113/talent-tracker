"""Talent Search skills for AI apps (Claude Code, Codex, Claude Desktop).

A "skill" is a folder with a SKILL.md (YAML frontmatter with name and description, then instructions) that an
AI app reads to learn how to do a job well. We ship a small set in the repo's skills/ folder (bundled into the
packaged app) and can copy them where the apps look:

  Claude Code   ~/.claude/skills/<name>/SKILL.md      (personal skills)
  Codex         ~/.agents/skills/<name>/SKILL.md      (user skills)
  The Claude app (desktop and claude.ai) has no skills folder on disk. It installs a plugin: one .plugin (zip)
  file holding .claude-plugin/plugin.json and skills/<name>/SKILL.md, added under Customize > Plugins > Add >
  Upload plugin. build_plugin() makes that file. The repo also carries the same plugin and a marketplace file at its
  root (.claude-plugin/), so `claude plugin marketplace add RogerFlores3113/talent-tracker` works too.

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
ZIP_NAME = "talent-search-skills.zip"
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
    """The Claude app plugin: one zip with .claude-plugin/plugin.json and skills/<name>/... for every skill.
    The same bytes every time (fixed dates, sorted names). No version field, so versions are kept elsewhere."""
    src = source_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        _zip_add(z, ".claude-plugin/plugin.json",
                 (json.dumps(PLUGIN_MANIFEST, indent=2) + "\n").encode("utf-8"))
        for name in skill_names():
            for rel in _files_of(name):
                _zip_add(z, f"skills/{name}/{rel.as_posix()}", (src / name / rel).read_bytes())
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


def reveal(path: Path, platform: str | None = None) -> bool:
    """Show the file in the system file manager. Best effort: never raises, False when it could not."""
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            cmd = ["open", "-R", str(path)]
        elif platform == "win32":
            cmd = ["explorer", f"/select,{path}"]
        else:
            cmd = ["xdg-open", str(Path(path).parent)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def claude_app_command(platform: str | None = None, env: dict[str, str] | None = None) -> list[str] | None:
    """How to start the Claude desktop app, or None when it isn't found (or on Linux, which has none)."""
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
    return None


def open_claude_app() -> bool:
    cmd = claude_app_command()
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ which apps are here, and all at once
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


# ------------------------------------------------------------------ zip for Claude Desktop (the older way)
def build_zip() -> bytes:
    """One zip holding one zip per skill. Claude's Customize > Skills takes one skill per zip, and each zip has
    the skill folder as its root (talent-search-x/SKILL.md)."""
    src = source_dir()
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as z:
        for name in skill_names():
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as zi:
                for rel in _files_of(name):
                    zi.write(src / name / rel, f"{name}/{rel.as_posix()}")
            z.writestr(f"{name}.zip", inner.getvalue())
        z.writestr("READ-ME.txt", ZIP_README)
    return outer.getvalue()


def build_skill_zip(name: str) -> bytes:
    """One skill as its own zip (the skill folder is the root), for Claude Desktop's Customize > Skills."""
    if name not in skill_names():
        raise KeyError(name)
    src = source_dir()
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as zi:
        for rel in _files_of(name):
            zi.write(src / name / rel, f"{name}/{rel.as_posix()}")
    return inner.getvalue()


ZIP_README = """Talent Search skills for Claude Desktop (the older way)

1. Unzip this file. You get one zip per skill.
2. In Claude, open Customize > Skills and add each of the zip files (do not unzip them again).
3. Make sure each skill is switched on.

Easier: use talent-search.plugin instead. One upload under Customize > Plugins > Add > Upload plugin.
"""

PLUGIN_STEPS = (
    "Open Claude.",
    "Go to Customize, then Plugins, then Add, then Upload plugin.",
    f"Pick {PLUGIN_FILE}.",
)

DESKTOP_STEPS = (
    "Claude Desktop reads skills from an upload, not from a folder on this computer. Download the zip, unzip it "
    "once, then in Claude open Customize > Skills and add each small zip inside. Switch each one on. Or download "
    "just the skills you want, one zip each, and add those.",
)


def write_zip(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_zip())
    return path
