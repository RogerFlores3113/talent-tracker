"""Cycle 20: a toast when a newer version is out, a Settings switch to stop checking, and Update now."""
from __future__ import annotations

import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import flash_of, local_client
from talent import __version__, updates


def _newer() -> str:
    major, minor, patch = updates.parse(__version__)
    return f"{major}.{minor}.{patch + 1}"


def test_versions_compare_as_numbers():
    assert updates.parse("v0.10.2") == (0, 10, 2) and updates.parse("latest") is None
    assert updates.newer("0.10.0", "0.9.9") and not updates.newer("0.2.2", "0.2.2") and not updates.newer(None)


def test_no_toast_until_github_names_a_newer_version(store):
    c = local_client(store)
    assert 'id="update-toast"' not in c.get("/").text
    store.set_settings(update_latest=__version__)
    assert 'id="update-toast"' not in c.get("/").text
    store.set_settings(update_latest=_newer())
    today = c.get("/").text
    assert 'id="update-toast"' in today and f"Version {_newer()} is available (you have {__version__})." in today
    assert updates.notes_url(_newer()) in today and 'aria-label="Close"' in today
    assert 'action="/update/dismiss"' in today
    for page in ("/candidates", "/jobs", "/settings"):  # every page, not only Today
        assert f"Version {_newer()} is available" in c.get(page).text, page


def test_not_now_hides_it_until_the_next_version(store):
    c = local_client(store)
    store.set_settings(update_latest=_newer())
    c.post("/update/dismiss", data={"version": _newer()}, follow_redirects=False)
    assert 'id="update-toast"' not in c.get("/").text
    major, minor, patch = updates.parse(_newer())
    store.set_settings(update_latest=f"{major}.{minor}.{patch + 1}")
    assert 'id="update-toast"' in c.get("/").text


def test_the_switch_turns_it_off_and_says_nothing_is_contacted(store):
    c = local_client(store)
    store.set_settings(update_latest=_newer())
    assert 'name="update_check" value="1" checked' in c.get("/settings").text  # on by default
    msg = flash_of(c.post("/settings/updates", data={}, follow_redirects=False))
    assert "contacts nothing at all" in msg and store.setting("update_check") == "0"
    assert 'id="update-toast"' not in c.get("/").text and not updates.enabled(store)
    c.post("/settings/updates", data={"update_check": "1"}, follow_redirects=False)
    assert updates.enabled(store)


def test_check_now_reports_what_github_said(store, monkeypatch):
    c = local_client(store)
    monkeypatch.setattr(updates, "fetch_latest", lambda timeout=5.0: None)
    assert "Could not reach GitHub" in flash_of(c.post("/update/check", follow_redirects=False))
    assert store.setting("update_checked_at")  # an offline computer isn't asked again right away
    monkeypatch.setattr(updates, "fetch_latest", lambda timeout=5.0: __version__)
    assert "You have the newest version" in flash_of(c.post("/update/check", follow_redirects=False))
    store.set_settings(update_dismissed=_newer())
    monkeypatch.setattr(updates, "fetch_latest", lambda timeout=5.0: _newer())
    assert f"Talent Search {_newer()} is out" in flash_of(c.post("/update/check", follow_redirects=False))
    assert 'id="update-toast"' in c.get("/").text  # asked on purpose, so Not now no longer hides it


def test_a_check_is_due_once_a_day(store):
    assert updates.due(store)
    updates.check(store, fetch=lambda: None)
    assert not updates.due(store)
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert updates.due(store, now=later)


def test_only_the_installed_app_in_its_usual_place_updates_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    exe = tmp_path / "Programs" / "Talent Search" / "Talent Search.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    assert updates.can_update_here(str(exe), frozen=True, platform="win32")
    assert not updates.can_update_here(str(exe), frozen=False, platform="win32")  # run from source
    assert not updates.can_update_here(str(tmp_path / "Elsewhere" / "Talent Search.exe"), frozen=True, platform="win32")
    home = Path.home() / ".local" / "share" / "talent-search-app" / "Talent Search"
    assert updates.can_update_here(str(home / "talent-search"), frozen=True, platform="linux")


def test_update_runs_the_readme_installer_in_its_own_process(tmp_path):
    calls = []
    updates.start_update("win32", popen=lambda cmd, **kw: calls.append((cmd, kw)), log_path=tmp_path / "u.log")
    cmd, kw = calls[-1]
    assert cmd[:2] == ["powershell", "-NoProfile"] and cmd[-1].startswith("irm https://raw.githubusercontent.com/")
    assert cmd[-1].endswith("/install.ps1 | iex") and kw["stdin"] == subprocess.DEVNULL
    assert kw["cwd"] == tempfile.gettempdir()  # not the app's folder, which the installer replaces
    updates.start_update("darwin", popen=lambda cmd, **kw: calls.append((cmd, kw)))
    cmd, kw = calls[-1]
    assert cmd[:2] == ["/bin/sh", "-c"] and "install.sh" in cmd[2] and kw["start_new_session"]


def test_update_now_starts_it_only_where_it_can(store, monkeypatch):
    c = local_client(store)
    store.set_settings(update_latest=_newer())
    started = []
    monkeypatch.setattr(updates, "start_update", lambda **kw: started.append(kw))
    monkeypatch.setattr(updates, "can_update_here", lambda *a, **k: False)
    page = c.get("/").text
    assert 'action="/update/now"' not in page and "To update, run the install command again" not in page
    assert 'href="https://github.com/RogerFlores3113/talent-tracker/releases/latest"' in page
    assert "can't update itself" in flash_of(c.post("/update/now", follow_redirects=False), "flash_error")
    monkeypatch.setattr(updates, "can_update_here", lambda *a, **k: True)
    assert 'action="/update/now"' in c.get("/").text
    assert f"Updating to {_newer()}" in flash_of(c.post("/update/now", follow_redirects=False))
    assert len(started) == 1 and started[0]["log_path"].name == "update.log"


def test_privacy_page_names_the_update_check(store):
    page = local_client(store).get("/privacy").text
    assert "The update check is the one thing it fetches" in page and "stays offline" not in page
    assert "never sent anywhere" not in page and f"<p>Talent Search v{__version__}</p>" in page  # the footer


def test_the_windows_installer_steps_out_of_the_folder_it_replaces():
    ps1 = (Path(__file__).resolve().parents[1] / "install.ps1").read_text(encoding="utf-8")
    block = ps1.split("Replacing the existing copy", 1)[1].split("New-Item -ItemType Directory -Path $Target", 1)[0]
    assert block.index("Set-Location -LiteralPath $env:TEMP") < block.index("Remove-Item -LiteralPath $Target")
    assert "[Environment]::CurrentDirectory = $env:TEMP" in block  # PowerShell's location alone isn't enough
    assert "$try -lt 10" in block and "Set-Location -LiteralPath $wasLocation" in block
