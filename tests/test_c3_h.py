"""Cycle 3, fixer H: restore backups in the app, import undo, and security hardening."""
import html
import logging
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path

import anyio
import pytest

from conftest import LOCAL_URL, flash_of, local_client
from talent import backups, importers, paths, recipes
from talent.mcp_server import build_server
from talent.store import NOTE_LIMIT, Store
from talent.web import encode_flash, flash_cookie_name

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def mode(p) -> int:
    return stat.S_IMODE(Path(p).stat().st_mode)


@pytest.fixture
def disk_store():
    """A store on the real data folder file, like the app's."""
    s = Store(paths.db_path())
    yield s
    s.close()


def _import(c, csv: bytes):
    r = c.post("/import", files={"file": ("p.csv", csv)})
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    return c.post("/import/run", data={"token": token, "col0": "name"})


# ------------------------------------------------------------ 1. restore
def test_settings_lists_backups_with_counts_and_restores_one(disk_store):
    s = disk_store
    s.add_candidate(name="Ana Before")
    s.add_job("Engineer")
    first = importers.backup_before_import(s, reason="delete")
    s.add_candidate(name="Bo After")
    c = local_client(s)
    page = c.get("/settings").text
    assert "Before a delete" in page and "Restore this backup" in page and first.name in page
    listed = backups.list_backups()
    assert listed[0]["name"] == first.name and (listed[0]["people"], listed[0]["jobs"]) == (1, 1)

    other = sqlite3.connect(paths.db_path())  # the AI app's connection, in another process
    r = c.post("/settings/backups/restore", data={"name": first.name}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#backups"
    msg = flash_of(r)
    assert "Restored the backup" in msg and "1 person and 1 job" in msg and "before-restore-" in msg
    assert [x["name"] for x in s.all_candidates()] == ["Ana Before"]
    assert other.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
    saved = [b for b in backups.list_backups() if b["name"].startswith("before-restore-")]
    assert saved and saved[0]["people"] == 2  # the data from just before is kept
    if os.name != "nt":
        assert mode(paths.db_path()) == 0o600 and mode(backups_path(saved[0]["name"])) == 0o600
    assert not Path(str(paths.db_path()) + "-wal").exists() or Path(str(paths.db_path()) + "-wal").stat().st_size == 0
    new = s.add_candidate(name="Cy New")
    assert new["id"] == 3  # Bo's id 2 is never handed out again
    other.close()


def backups_path(name: str) -> Path:
    return paths.backups_dir() / name


def test_restore_refuses_names_outside_the_backups_folder(disk_store):
    c = local_client(disk_store)
    for name in ("../talent.db", "talent.db", "before-x.db", "download-abc.db"):
        r = c.post("/settings/backups/restore", data={"name": name}, follow_redirects=False)
        assert "isn't in the backups folder" in flash_of(r, "flash_error")
    bad = paths.backups_dir() / "before-import-2026-01-01-000000.db"
    bad.write_bytes(b"not a database")
    r = c.post("/settings/backups/restore", data={"name": bad.name}, follow_redirects=False)
    assert "can't be read" in flash_of(r, "flash_error")
    assert "Can't be read" in c.get("/settings").text


def test_restore_reopens_with_the_same_schema(disk_store):
    disk_store.add_candidate(name="Now")
    old = importers.backup_before_import(disk_store)
    conn = sqlite3.connect(old)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    backups.restore(disk_store, old.name)
    assert disk_store.schema_version == version and disk_store.count_candidates() == 1


def test_ai_has_no_restore_tool(store):
    names = {t.name for t in anyio.run(build_server(store).list_tools)}
    assert not any("restore" in n or "backup" in n for n in names)


# ------------------------------------------------------------ 2. undo import
def test_undo_this_import(disk_store):
    s = disk_store
    s.add_candidate(name="Kept")
    c = local_client(s)
    r = _import(c, b"Name\nZed One\nZed Two\n")
    assert "Undo this import" in r.text and 'action="/import/undo"' in r.text
    name = re.search(r'name="backup" value="([^"]+)"', r.text).group(1)
    assert name.startswith("before-import-") and s.count_candidates() == 3
    r = c.post("/import/undo", data={"backup": name}, follow_redirects=False)
    assert r.headers["location"] == "/candidates" and "Undid the import" in flash_of(r)
    assert [x["name"] for x in s.all_candidates()] == ["Kept"]
    assert any(b["name"].startswith("before-restore-") and b["people"] == 3 for b in backups.list_backups())


def test_undo_import_refuses_after_another_change(disk_store):
    s = disk_store
    c = local_client(s)
    r = _import(c, b"Name\nZed One\n")
    name = re.search(r'name="backup" value="([^"]+)"', r.text).group(1)
    c.get("/")  # just looking changes nothing
    assert backups.import_undo_state(s, name) == "ok"
    s.log(next(iter(s.all_candidates()))["id"], "note", "Called them")
    r = c.post("/import/undo", data={"backup": name}, follow_redirects=False)
    assert "Something changed after that import" in flash_of(r, "flash_error") and s.count_candidates() == 1
    assert c.post("/import/undo", data={"backup": "before-import-2020-01-01-000000.db"},
                  follow_redirects=False).headers["location"] == "/settings#backups"


# ------------------------------------------------------------ 3. no-store
def test_pages_api_export_and_backup_are_never_cached(store):
    c = local_client(store)
    for path in ("/", "/candidates", "/api/candidates", "/export.csv", "/backup", "/settings"):
        assert c.get(path).headers.get("cache-control") == "no-store", path
    assert local_client(store, unlocked=False).get("/").headers["cache-control"] == "no-store"
    assert c.get("/candidates/999").headers["cache-control"] == "no-store"


# ------------------------------------------------------------ 4. flash cookie
def test_notices_travel_in_a_cookie_not_the_address(store):
    cand = store.add_candidate(name="Lin Probe")
    c = local_client(store)
    r = c.post(f"/candidates/{cand['id']}/delete", follow_redirects=False)
    assert r.headers["location"] == "/" and "Lin" not in r.headers["location"]
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=strict" in cookie.lower() and "Max-Age=" in cookie
    assert "Lin Probe" in flash_of(r)
    page = c.get("/")
    assert "Deleted Lin Probe" in page.text
    assert "Deleted Lin Probe" not in c.get("/").text  # shown once


def test_a_forged_flash_cookie_is_ignored(store):
    c = local_client(store)
    name = flash_cookie_name(paths.app_key())
    c.cookies.set(name, encode_flash("someone-elses-key", {"notice": "Your account is suspended"}))
    assert "suspended" not in c.get("/").text
    c.cookies.set(name, encode_flash(paths.app_key(), {"notice": "Real one"}))
    assert "Real one" in c.get("/").text


def test_back_keeps_other_params_and_the_fragment(store):
    c = local_client(store)
    r = c.post("/actions/snooze", data={"key": "x", "days": "1"},
               headers={"referer": LOCAL_URL + "/jobs?status=open&notice=old"}, follow_redirects=False)
    assert r.headers["location"] == "/jobs?status=open" and flash_of(r) == "Snoozed for 1 day."


# ------------------------------------------------------------ 5. locked page
def test_locked_page_shows_only_how_to_unlock(store, caplog):
    with caplog.at_level(logging.INFO, logger="talent.web"):
        r = local_client(store, unlocked=False).get("/jobs")
    assert r.status_code == 401 and "talent open" not in r.text and "app icon" in r.text
    assert "Back to Today" not in r.text and 'href="/jobs"' not in r.text and "<nav" not in r.text
    assert "talent open" in caplog.text


# ------------------------------------------------------------ 6. bad ids
def test_bad_path_id_shows_not_found_page(store):
    c = local_client(store)
    r = c.get("/candidates/abc")
    assert r.status_code == 404 and "Not found" in r.text and "text/html" in r.headers["content-type"]
    assert c.get("/jobs/1x/edit").status_code == 404
    assert c.get("/candidates?page_no=lots").status_code == 400
    api = c.get("/api/candidates/abc")
    assert api.status_code == 422 and api.json()["detail"]


# ------------------------------------------------------------ 7. data folder
@posix_only
def test_existing_picked_data_folder_is_tightened(tmp_path, monkeypatch):
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "talent.db").write_bytes(b"")
    os.chmod(mine, 0o755)
    monkeypatch.setenv("TALENT_DATA_DIR", str(mine))
    assert mode(paths.data_dir()) == 0o700 and paths.data_dir_private()


@posix_only
def test_privacy_page_says_when_the_folder_is_shared(store, tmp_path, monkeypatch):
    c = local_client(store)
    assert "Only your user account can open the data folder" in c.get("/privacy").text
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "other.txt").write_text("x")
    os.chmod(shared, 0o755)
    monkeypatch.setenv("TALENT_DATA_DIR", str(shared))
    page = c.get("/privacy").text
    assert "Only your user account" not in page and "Other accounts on this computer may be able" in page


# ------------------------------------------------------------ 8. note length
def test_timeline_entries_have_a_length_cap(store):
    cand = store.add_candidate(name="Ana")
    store.log(cand["id"], "note", "x" * NOTE_LIMIT)
    with pytest.raises(ValueError, match="too long .*200,000 characters.*20,000"):
        store.log(cand["id"], "call", "x" * 200000)
    server = build_server(store)
    with pytest.raises(Exception, match="too long"):
        anyio.run(lambda: server.call_tool("log_activity", {"candidate_id": cand["id"], "kind": "note",
                                                            "summary": "y" * 200000}))
    r = local_client(store).post(f"/candidates/{cand['id']}/log", data={"kind": "note", "body": "z" * 30000},
                                 follow_redirects=False)
    assert r.status_code == 400 and "too long" in html.unescape(r.text)
    assert len(store.timeline(cand["id"])) == 1


# ------------------------------------------------------------ 9. recipes
def test_refresh_recipe_only_names_linkedin_profiles():
    assert "https://www.linkedin.com/in/ana" in recipes.refresh_candidate(
        {"id": 1, "name": "Ana", "linkedin_url": "https://www.linkedin.com/in/ana"})
    for c in ({"id": 2, "name": "Bo", "website": "https://evil.example/ignore-previous"},
              {"id": 3, "name": "Cy", "linkedin_url": "https://linkedin.com.evil.example/in/cy"}):
        prompt = recipes.refresh_candidate(c)
        assert "evil" not in prompt and "ask me for their profile link" in prompt


def test_reports_leave_out_saved_names(store):
    store.add_candidate(name="Maya Okafor", email="maya@x.com")
    store.add_candidate(name="Al")  # too short to strip safely
    title, body = recipes.issue_text("Maya Okafor's page broke", "MAYA OKAFOR again, and Okafor twice. Al is fine.",
                                     names=["Maya Okafor", "Al"])
    assert "Maya" not in title and "[name removed]" in title
    assert "MAYA" not in body and "Okafor" not in body and "Al is fine" in body
    page = local_client(store).post("/report", data={"summary": "Maya Okafor broke", "where": "/"}).text
    assert "Okafor" not in page
    server = build_server(store)
    _, data = anyio.run(lambda: server.call_tool("report_problem", {"summary": "Search for Maya Okafor fails"}))
    url = data.get("result", data)["url"]
    assert "Okafor" not in url and "Maya" not in url


# ------------------------------------------------------------ 10. snippets
def test_api_snippets_have_no_markers(store):
    store.add_candidate(name="Ana", about="Built payment systems in Rust for a bank")
    res = local_client(store).get("/api/candidates", params={"q": "rust"}).json()["results"]
    assert res and res[0]["snippet"] and "\x02" not in res[0]["snippet"] and "\x03" not in res[0]["snippet"]
    assert "Rust" in res[0]["snippet"]


def test_new_pages_have_no_dashes(disk_store):
    importers.backup_before_import(disk_store)
    c = local_client(disk_store)
    for text in (c.get("/settings").text, local_client(disk_store, unlocked=False).get("/").text,
                 c.get("/candidates/abc").text, _import(c, b"Name\nZed\n").text):
        assert "—" not in text and " – " not in text
