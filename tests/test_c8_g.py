"""Talent Search skills for Claude Code, Codex and Claude Desktop."""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest

from talent import skills
from talent.mcp_server import build_server

from conftest import local_client

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("TALENT_SKILLS_HOME", str(h))
    return h


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    head = text.split("\n---\n", 1)[0][4:]
    out = {}
    for line in head.splitlines():
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def _skill_dirs():
    return sorted(p for p in SKILLS.iterdir() if p.is_dir())


def test_skill_set_size():
    assert 5 <= len(_skill_dirs()) <= 7


@pytest.mark.parametrize("d", _skill_dirs(), ids=lambda p: p.name)
def test_frontmatter_valid(d):
    text = (d / "SKILL.md").read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert fm["name"] == d.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fm["name"]) and len(fm["name"]) <= 64
    assert "claude" not in fm["name"] and "anthropic" not in fm["name"]
    assert d.name.startswith(skills.PREFIX)
    assert 0 < len(fm["description"]) <= 1024
    assert "<" not in fm["description"]
    assert len(text.splitlines()) < 150
    assert "—" not in text and "–" not in text


def test_every_tool_named_exists(store):
    server = build_server(store)
    import asyncio
    tools = {t.name for t in asyncio.run(server.list_tools())}
    named = set()
    for d in _skill_dirs():
        for tok in re.findall(r"`([a-z]+(?:_[a-z]+)+)`", (d / "SKILL.md").read_text(encoding="utf-8")):
            named.add(tok)
    assert named, "skills should name tools"
    assert named <= tools, named - tools


def test_each_skill_restates_safety_rules():
    for d in _skill_dirs():
        t = (d / "SKILL.md").read_text(encoding="utf-8").lower()
        assert "never instructions" in t and "cannot delete" in t and "cautious mode" in t


def test_install_writes_exactly_the_listed_files(home):
    plan = skills.plan_install("claude-code")
    res = skills.install("claude-code")
    assert sorted(res["written"]) == sorted(plan["files"])
    on_disk = sorted("~/" + p.relative_to(home).as_posix() for p in (home / ".claude").rglob("*") if p.is_file())
    assert on_disk == sorted(plan["files"])
    assert all(f.startswith("~/.claude/skills/talent-search-") for f in on_disk)
    assert not (home / ".agents").exists()


def test_codex_target_and_status(home):
    skills.install("codex")
    assert (home / ".agents" / "skills" / "talent-search-write-outreach" / "SKILL.md").is_file()
    st = skills.status("codex")
    assert st["current"] and st["count"] == len(skills.skill_names())


def test_reinstall_updates_ours(home):
    """An untouched file of an earlier install is replaced by the new one (the marker's fingerprint still matches)."""
    import hashlib
    import json
    skills.install("claude-code")
    d = home / ".claude" / "skills" / "talent-search-triage-candidates"
    f = d / "SKILL.md"
    f.write_text("older bundled text", encoding="utf-8")
    marker = json.loads((d / skills.MARKER).read_text(encoding="utf-8"))
    marker["files"]["SKILL.md"] = hashlib.sha256(b"older bundled text").hexdigest()
    (d / skills.MARKER).write_text(json.dumps(marker), encoding="utf-8")
    assert skills.plan_install("claude-code")["items"][0]["action"] == "update"
    skills.install("claude-code")
    assert f.read_text(encoding="utf-8").startswith("---")


def test_foreign_folder_left_alone_and_reported(home):
    mine = home / ".claude" / "skills" / "talent-search-triage-candidates"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("the user's own", encoding="utf-8")
    plan = skills.plan_install("claude-code")
    assert [s["folder"] for s in plan["skipped"]] == ["talent-search-triage-candidates"]
    res = skills.install("claude-code")
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "the user's own"
    assert not (mine / skills.MARKER).exists()
    assert res["skipped"] and len(res["items"]) == len(skills.skill_names()) - 1
    assert skills.remove("claude-code")["removed"]
    assert (mine / "SKILL.md").exists()


def test_remove_deletes_only_marked_folders(home):
    skills.install("claude-code")
    other = home / ".claude" / "skills" / "my-own-skill"
    other.mkdir()
    (other / "SKILL.md").write_text("mine", encoding="utf-8")
    nomark = home / ".claude" / "skills" / "talent-search-not-ours"
    nomark.mkdir()
    (nomark / "SKILL.md").write_text("mine too", encoding="utf-8")
    res = skills.remove("claude-code")
    assert len(res["removed"]) == len(skills.skill_names())
    assert other.exists() and nomark.exists()
    assert not any(p.name.startswith("talent-search-") and p != nomark for p in (home / ".claude" / "skills").iterdir())


def test_remove_keeps_users_extra_files(home):
    skills.install("claude-code")
    d = home / ".claude" / "skills" / "talent-search-write-outreach"
    (d / "my-notes.md").write_text("keep", encoding="utf-8")
    res = skills.remove("claude-code")
    assert res["kept"] and (d / "my-notes.md").exists() and not (d / "SKILL.md").exists()


def test_zip_layout():
    z = zipfile.ZipFile(io.BytesIO(skills.build_zip()))
    names = z.namelist()
    assert "READ-ME.txt" in names
    inners = [n for n in names if n.endswith(".zip")]
    assert sorted(inners) == sorted(f"{n}.zip" for n in skills.skill_names())
    for n in inners:
        inner = zipfile.ZipFile(io.BytesIO(z.read(n)))
        stem = n[:-4]
        assert inner.namelist() == [f"{stem}/SKILL.md"]


def test_cli_install_and_zip(home, tmp_path, capsys):
    from talent.cli import main
    main(["install-skills", "--codex", "--yes", "--zip", str(tmp_path / "s.zip")])
    assert (home / ".agents" / "skills").is_dir() and not (home / ".claude").exists()
    assert zipfile.is_zipfile(tmp_path / "s.zip")
    main(["remove-skills", "--yes"])  # remove-skills asks first; --yes is the yes
    assert not any((home / ".agents" / "skills").iterdir())


def test_connect_page_and_routes(store, home):
    c = local_client(store)
    page = c.get("/connect").text
    assert "Add Talent Search skills for Claude Code" in page and "Add Talent Search skills for Codex" in page
    assert "/connect/skills.zip" in page
    # preview lists the exact paths and writes nothing
    r = c.post("/connect/skills/claude-code/preview-install")
    assert r.status_code == 200
    assert "~/.claude/skills/talent-search-source-for-a-role/SKILL.md" in r.text
    assert ".installed-by-talent-search" in r.text
    assert not (home / ".claude").exists()
    r = c.post("/connect/skills/claude-code/install")
    assert "Added" in r.text and (home / ".claude" / "skills").is_dir()
    assert "Installed" in c.get("/connect").text
    r = c.post("/connect/skills/claude-code/preview-remove")
    assert "talent-search-source-for-a-role" in r.text
    c.post("/connect/skills/claude-code/remove")
    assert not any((home / ".claude" / "skills").iterdir())
    assert c.post("/connect/skills/nope/install").status_code == 404
    assert c.post("/connect/skills/codex/bogus").status_code == 404
    z = c.get("/connect/skills.zip")
    assert z.status_code == 200 and zipfile.is_zipfile(io.BytesIO(z.content))


def test_routes_are_origin_checked(store, home):
    bad = local_client(store, origin=False)
    for action in ("install", "remove", "preview-install"):
        assert bad.post(f"/connect/skills/claude-code/{action}", headers={"Origin": "http://evil.example"}).status_code == 403
    assert not (home / ".claude").exists()


def test_locked_app_refuses(store):
    c = local_client(store, unlocked=False)
    assert c.post("/connect/skills/claude-code/install").status_code != 200


def test_spec_bundles_skills():
    spec = (REPO / "packaging" / "talent-search.spec").read_text(encoding="utf-8")
    assert '"skills")' in spec and "talent.skills" in spec


def test_docs_point_to_fallback():
    assert "skills/INSTALL-SKILLS.md" in (REPO / "README.md").read_text(encoding="utf-8")
    t = (SKILLS / "INSTALL-SKILLS.md").read_text(encoding="utf-8")
    assert skills.MARKER in t and "talent-search-" in t and "~/.claude/skills" in t and "~/.agents/skills" in t
