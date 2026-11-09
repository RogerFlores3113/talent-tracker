"""Talent Search skills for AI apps (Claude Code, Codex, Claude Desktop).

A "skill" is a folder with a SKILL.md (YAML frontmatter with name and description, then instructions) that an
AI app reads to learn how to do a job well. We ship a small set in the repo's skills/ folder (bundled into the
packaged app) and can copy them where the apps look:

  Claude Code   ~/.claude/skills/<name>/SKILL.md      (personal skills)
  Codex         ~/.agents/skills/<name>/SKILL.md      (user skills)
  Claude Desktop has no skills folder on disk. Skills are uploaded as a zip under Customize > Skills, so for it
  we only build a zip.

Formats checked against the apps' own docs:
  https://code.claude.com/docs/en/skills
  https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
  https://support.claude.com/en/articles/12512198-creating-custom-skills
  https://learn.chatgpt.com/docs/build-skills  (OpenAI's Codex skills page; developers.openai.com redirects to it)

Safety rules for what we write:
  * Every folder we install is named talent-search-*, so we never touch the user's own skills.
  * Every folder we create gets a marker file. We only replace or remove folders that carry it.
  * A folder with the same name but no marker is left alone and reported.
  * Nothing is written until the user has seen the exact list of files and confirmed.
"""
from __future__ import annotations

import io
import json
import os
import shutil
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


def _files_of(skill: str, src: Path | None = None) -> list[Path]:
    """Every file in one bundled skill, relative to the skill folder (SKILL.md first)."""
    root = (src or source_dir()) / skill
    rel = sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())
    rel.sort(key=lambda p: (p.as_posix() != "SKILL.md", p.as_posix()))
    return rel


# ------------------------------------------------------------------ marker
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


# ------------------------------------------------------------------ plan / install / remove
def plan_install(target: str) -> dict[str, Any]:
    """What installing would do, without writing anything: the files, and any folder we'd leave alone."""
    base = target_dir(target)
    items, files, skipped = [], [], []
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
        paths = [folder / rel for rel in _files_of(name)] + [folder / MARKER]
        items.append({"folder": name, "action": action, "files": [display_path(p) for p in paths]})
        files.extend(display_path(p) for p in paths)
    return {"target": target, "label": TARGET_LABELS[target], "base": display_path(base), "items": items,
            "files": files, "skipped": skipped, "version": __version__}


def install(target: str) -> dict[str, Any]:
    """Copy our skills into the app's folder. Only creates or updates folders we made; reports the rest."""
    plan = plan_install(target)
    base = target_dir(target)
    src = source_dir()
    written: list[str] = []
    for item in plan["items"]:
        name = item["folder"]
        folder = base / name
        folder.mkdir(parents=True, exist_ok=True)
        for rel in _files_of(name):
            dest = folder / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((src / name / rel).read_bytes())
            written.append(display_path(dest))
        marker = folder / MARKER
        marker.write_text(json.dumps({"app": "talent-search", "version": __version__, "skill": name}) + "\n",
                          encoding="utf-8")
        written.append(display_path(marker))
    return {**plan, "written": written}


def _our_folders(target: str) -> list[Path]:
    base = target_dir(target)
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and _is_ours(p))


def plan_remove(target: str) -> dict[str, Any]:
    folders = _our_folders(target)
    files = []
    for f in folders:
        files.extend(display_path(p) for p in sorted(f.rglob("*")) if p.is_file())
    return {"target": target, "label": TARGET_LABELS[target], "base": display_path(target_dir(target)),
            "folders": [display_path(f) for f in folders], "files": files}


def remove(target: str) -> dict[str, Any]:
    """Delete only folders that carry our marker. Files someone added inside one of them are kept."""
    removed, kept = [], []
    bundled = set(skill_names())
    for folder in _our_folders(target):
        known = {MARKER, "SKILL.md"}
        if folder.name in bundled:
            known |= {r.as_posix() for r in _files_of(folder.name)}
        for rel in sorted(known):
            try:
                (folder / rel).unlink()
            except OSError:
                pass
        for d in sorted((p for p in folder.rglob("*") if p.is_dir()), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            folder.rmdir()
            removed.append(display_path(folder))
        except OSError:
            kept.append(display_path(folder))
    return {"target": target, "label": TARGET_LABELS[target], "removed": removed, "kept": kept}


def status(target: str) -> dict[str, Any]:
    """Installed state for the Connect page."""
    ours = _our_folders(target)
    versions = sorted({str((_read_marker(f) or {}).get("version") or "") for f in ours} - {""})
    have = {f.name for f in ours}
    want = set(skill_names())
    return {"target": target, "label": TARGET_LABELS[target], "base": display_path(target_dir(target)),
            "installed": sorted(have), "count": len(have), "total": len(want), "versions": versions,
            "current": bool(have) and have >= want and versions == [__version__],
            "missing": sorted(want - have)}


# ------------------------------------------------------------------ zip for Claude Desktop
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


ZIP_README = """Talent Search skills for Claude Desktop

1. Unzip this file. You get one zip per skill.
2. In Claude, open Customize > Skills and add each of the zip files (do not unzip them again).
3. Make sure each skill is switched on.

Uploading skills needs a Claude plan that allows custom skills.
"""

DESKTOP_STEPS = (
    "Claude Desktop reads skills from an upload, not from a folder on this computer. Download the zip, unzip it "
    "once, then in Claude open Customize > Skills and add each small zip inside. Switch each one on.",
)


def write_zip(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_zip())
    return path
