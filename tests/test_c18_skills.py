"""Add skills, honest and as close to one click as Claude allows (panel 18 notes)."""
from __future__ import annotations

import re
import subprocess

import pytest

from talent import skills

from conftest import local_client


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TALENT_SKILLS_HOME", str(home))
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(dl))
    monkeypatch.setattr(skills, "reveal", lambda path, platform=None: True)
    monkeypatch.setattr(skills, "claude_app_command", lambda *a, **k: None)
    return {"home": home, "dl": dl}


def _add(store):
    """The result text only: the out-of-band refresh of the panel's own 'found' list is cut off."""
    return local_client(store).post("/connect/skills/add").text.split('<ul class="plain" id="skills-found"', 1)[0]


def test_no_app_found_hides_not_found_and_never_says_done(store):
    t = _add(store)
    assert "not found" not in t and "Claude Code</strong>" not in t and "Codex</strong>" not in t
    assert "Done." not in t
    assert "Saved <code>talent-search.plugin</code> to your Downloads folder" in t
    assert "Last step in Claude: Customize &gt; Plugins &gt; Add &gt; Upload plugin, then pick talent-search.plugin" in t


def test_only_found_apps_are_listed_and_pending_step_stays(store, env):
    (env["home"] / ".claude").mkdir()
    t = _add(store)
    assert "Claude Code</strong>: added 6 skills" in t and "Codex" not in t
    assert "Done." not in t and "Last step in Claude" in t


def test_first_run_is_saved_not_still_and_repeat_is_already_saved(store):
    c = local_client(store)
    first = c.post("/connect/skills/add").text
    assert "is still in" not in first and "is already saved in" not in first
    again = c.post("/connect/skills/add").text
    assert "is already saved in your Downloads folder" in again and "is still in" not in again
    assert "Done." not in again and "Upload plugin" in again  # we can't know it was uploaded


def test_the_click_saves_reveals_opens_and_offers_the_path(store, env, monkeypatch):
    shown, opened = [], []
    monkeypatch.setattr(skills, "reveal", lambda path, platform=None: shown.append(path) or True)
    monkeypatch.setattr(skills, "claude_app_command", lambda *a, **k: ["open", "-a", "Claude"])
    monkeypatch.setattr(skills, "open_claude_app", lambda: opened.append(1) or True)
    t = _add(store)
    assert shown == [env["dl"] / "talent-search.plugin"] and opened == [1]
    assert "Claude is opening" in t and "Open Claude" in t
    assert f'data-copy-text="{env["dl"] / "talent-search.plugin"}"' in t and "Copy file path" in t


def test_open_claude_never_kills_anything(monkeypatch):
    calls = []
    monkeypatch.setattr(skills, "claude_app_command", lambda *a, **k: ["open", "-a", "Claude"])
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    assert skills.open_claude_app() and calls == [["open", "-a", "Claude"]]


def test_page_says_it_honestly_with_visible_steps_and_marketplace(store):
    page = local_client(store).get("/connect").text
    assert "Two steps, one click each" not in page
    assert "one click here, then one upload in Claude" in page
    folded = page.split('id="skills-other-ways"', 1)[1]
    assert page.index('id="plugin-steps"') < page.index('id="skills-other-ways"')
    assert 'id="plugin-steps"' not in folded
    steps = page.split('id="plugin-steps"', 1)[1].split("</ol>", 1)[0]
    assert "Customize, then Plugins" in steps and "Add, then Upload plugin" in steps and "Pick talent-search.plugin" in steps
    assert "Or add the marketplace (gets updates)" in page
    pre = re.search(r'<pre class="code wrap" id="marketplace-text">([^<]*)</pre>', page)
    assert pre and pre.group(1) == "RogerFlores3113/talent-tracker"
    assert 'data-copy-from="marketplace-text"' in page


def test_results_are_announced_once(store):
    c = local_client(store)
    page = c.get("/connect").text
    assert 'aria-live="polite"' not in page.split('id="step-skills"', 1)[1].split('id="try-asking"', 1)[0]
    for t in (_add(store), c.post("/connect/skills/plugin/save").text):
        assert 'aria-live' not in t
    assert t.count('role="status"') == 1
    assert 'role="status"' in c.post("/connect/skills/plugin/reveal").text


def test_installer_closing_text_mentions_the_restart():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert "quits and reopens Claude Desktop" in (root / "install.sh").read_text(encoding="utf-8")
    assert "quits and reopens Claude Desktop" in (root / "install.ps1").read_text(encoding="utf-8")
    assert "quits and reopens Claude Desktop" in (root / "INSTALL.md").read_text(encoding="utf-8")


def test_no_dashes_in_new_text(store):
    for t in (local_client(store).get("/connect").text, _add(store)):
        assert "—" not in t and "–" not in t


def test_found_list_collapses_missing_apps_into_one_quiet_line(store, env):
    c = local_client(store)
    for t in (c.get("/connect").text, c.post("/connect/skills/add").text):
        assert "Not found" not in t and "found." not in t
        assert t.count('id="skills-not-here"') == 1
        assert "Not on this computer: Claude Code, Codex." in t
        assert 'aria-label="Install the skills for Codex anyway"' in t
    (env["home"] / ".claude").mkdir()
    t = c.get("/connect").text
    assert "Not on this computer: Codex." in t and "Claude Code</strong>:" in t
