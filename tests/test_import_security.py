import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import LOCAL_URL, local_client
from talent import importers, integrations, paths, recipes
from talent.integrations import LaunchSpec
from talent.store import parse_when

SPEC = LaunchSpec("/opt/talent/talent-search", ["mcp"], {"TALENT_DATA_DIR": "/data"})
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")


def mode(p) -> int:
    return stat.S_IMODE(Path(p).stat().st_mode)


# ------------------------------------------------------------------ CSRF
def test_post_needs_our_own_origin(store):
    c = local_client(store)
    assert c.post("/sample-data", headers={"origin": "null"}).status_code == 403
    assert c.post("/sample-data", headers={"origin": "https://attacker.example"}).status_code == 403
    assert c.post("/sample-data", headers={"origin": "http://127.0.0.1:9999"}).status_code == 403  # other port
    assert c.post("/sample-data", headers={"origin": "http://localhost:8765"}).status_code == 403  # other origin
    assert c.post("/sample-data", follow_redirects=False).status_code == 303  # default Origin is ours


def test_post_without_origin_uses_fetch_metadata(store):
    bare = local_client(store, origin=False)  # sends no Origin
    for site in ("cross-site", "same-site"):
        assert bare.post("/sample-data", headers={"sec-fetch-site": site}).status_code == 403
    for site in ("same-origin", "none"):
        assert bare.post("/sample-data", headers={"sec-fetch-site": site}, follow_redirects=False).status_code == 303
    # Neither header (curl, scripts): allowed, a browser always sends one on a cross-site request.
    assert bare.post("/api/candidates", json={"name": "Curl"}).status_code == 201


def test_downloads_refuse_other_sites(store):
    c = local_client(store)
    for path in ("/backup", "/export.csv"):
        assert c.get(path, headers={"sec-fetch-site": "cross-site"}).status_code == 403
        assert c.get(path, headers={"sec-fetch-site": "same-site"}).status_code == 403
        assert c.get(path, headers={"sec-fetch-site": "same-origin"}).status_code == 200
        assert c.get(path, headers={"sec-fetch-site": "none"}).status_code == 200
    assert c.get("/", headers={"sec-fetch-site": "cross-site"}).status_code == 200  # a plain link to the app is fine


def test_testserver_host_is_not_local(store):
    from fastapi.testclient import TestClient
    from talent.web import create_app

    assert TestClient(create_app(store)).get("/").status_code == 403
    c = local_client(store)
    assert c.get("/", headers={"host": "[::1]"}).status_code == 200
    assert c.get("/", headers={"host": "localhost"}).status_code == 200


def test_backup_download_never_touches_system_temp(store, monkeypatch):
    seen = []
    real = tempfile.mkstemp
    monkeypatch.setattr(tempfile, "mkstemp", lambda *a, **k: seen.append(k.get("dir")) or real(*a, **k))
    r = local_client(store).get("/backup")
    assert r.status_code == 200 and r.content[:15] == b"SQLite format 3"
    assert seen and Path(seen[0]) == paths.backups_dir()


# ---------------------------------------------------------------- import
def test_parse_when_reads_spreadsheet_dates():
    from datetime import datetime, timedelta
    from talent.store import to_dt

    for v, day in [("3/5/2026", "03-05"), ("3/5/26", "03-05"), ("2026/03/05", "03-05"), ("Mar 3, 2026", "03-03"),
                   ("3 Mar 2026", "03-03"), ("March 3rd 2026", "03-03"), ("25/3/2026", "03-25"),
                   ("2026-03-05", "03-05")]:
        local = parse_when(v)
        assert to_dt(local).astimezone().strftime("%m-%d") == day, v
    today = datetime.now().astimezone().date()
    assert to_dt(parse_when("today")).astimezone().date() == today
    assert to_dt(parse_when("Yesterday")).astimezone().date() == today - timedelta(days=1)
    for bad in ("soon", "13/13/2026", "2/30/2026", "Foo 3, 2026"):
        with pytest.raises(ValueError, match="Couldn't read the date"):
            parse_when(bad)


def test_bad_date_skips_the_cell_not_the_row(store):
    header, rows = importers.read_csv("Name,Last contacted\nAna,3/5/2026\nBo,someday\n")
    r = importers.import_rows(store, header, rows, importers.guess_mapping(header))
    assert r["added"] == 2 and r["skipped_rows"] == 0 and r["skipped"] == []
    assert r["warnings"] == ["Row 3: left Last contacted blank, couldn't read the date 'someday'"]
    bo = store.find_by_name("Bo")[0]
    assert bo["last_contacted_at"] is None


def test_row_errors_never_stop_the_import(store, monkeypatch):
    store.add_candidate(name="Ana", email="ana@x.com")
    store.add_candidate(name="Bob", linkedin_url="https://www.linkedin.com/in/bob")
    text = ("Name,Email,LinkedIn\nAna B,ana@x.com,https://www.linkedin.com/in/bob\n"
            "Cy,cy@x.com,\n")
    header, rows = importers.read_csv(text)
    r = importers.import_rows(store, header, rows, importers.guess_mapping(header))
    assert r["added"] == 1 and r["skipped_rows"] == 1
    assert r["skipped"][0].startswith("Row 2: its email and LinkedIn match two different people")

    real = store.upsert_candidate

    def flaky(**data):
        if data["name"] == "Boom":
            raise RuntimeError("disk hiccup")
        return real(**data)

    monkeypatch.setattr(store, "upsert_candidate", flaky)
    header, rows = importers.read_csv("Name\nBoom\nFine\n")
    r = importers.import_rows(store, header, rows, ["name"])
    assert r["added"] == 1 and r["skipped"] == ["Row 2: couldn't be saved (RuntimeError: disk hiccup)"]


def test_import_page_survives_a_bad_row(store):
    store.add_candidate(name="Ana", email="ana@x.com")
    store.add_candidate(name="Bob", linkedin_url="https://www.linkedin.com/in/bob")
    c = local_client(store)
    r = c.post("/import", files={"file": ("p.csv", b"Name,Email,LinkedIn\nX,ana@x.com,linkedin.com/in/bob\n")})
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    r = c.post("/import/run", data={"token": token, "col0": "name", "col1": "email", "col2": "linkedin_url"})
    assert r.status_code == 200 and "skipped 1 row" in r.text
    assert "the email is Ana&#39;s, id 1, and the LinkedIn is Bob&#39;s, id 2), so it was left alone" in r.text


def test_export_then_import_keeps_phone_and_formula_text(store, tmp_path):
    from talent.store import Store

    store.add_candidate(name="Pat", phone="+1 415 555 0100", email="pat@x.com", tags="go, rust")
    store.add_candidate(name="=cmd()", email="eq@x.com")
    text = importers.export_csv(store)
    fresh = Store(tmp_path / "fresh.db")
    header, rows = importers.read_csv(text)
    importers.import_rows(fresh, header, rows, importers.guess_mapping(header))
    assert fresh.find_by_name("Pat")[0]["phone"] == "+1 415 555 0100"
    assert fresh.find_by_name("=cmd()")[0]["email"] == "eq@x.com"
    fresh.close()


def test_tags_split_on_pipes_not_slashes(store):
    header, rows = importers.read_csv("Name,Skills\nAna,\"Go | ci/cd; SQL, Rust\"\n")
    importers.import_rows(store, header, rows, importers.guess_mapping(header))
    assert store.find_by_name("Ana")[0]["tags"] == ["Go", "ci/cd", "SQL", "Rust"]
    assert store.add_candidate(name="Bo", tags="TCP/IP|Go")["tags"] == ["TCP/IP", "Go"]


def test_notes_column_goes_on_the_timeline(store):
    header = ["Name", "Email", "Notes", "About"]
    assert importers.guess_mapping(header) == ["name", "email", "note", "about"]
    assert importers.guess_mapping(["Name", "Comments"]) == ["name", "note"]
    text = "Name,Email,Notes,About\nAna,ana@x.com,Great call on Monday,Builds compilers\n"
    for _ in range(2):  # importing the same file twice doesn't double the note
        header, rows = importers.read_csv(text)
        r = importers.import_rows(store, header, rows, importers.guess_mapping(header))
    ana = store.find_by_name("Ana")[0]
    notes = [t for t in store.timeline(ana["id"]) if t["kind"] == "note"]
    assert [n["body"] for n in notes] == ["Great call on Monday"]
    assert ana["about"] == "Builds compilers" and r["notes"] == 0


def test_every_import_backs_up_first(store):
    store.add_candidate(name="Before")
    header, rows = importers.read_csv("Name\nAfter\n")
    r = importers.import_rows(store, header, rows, ["name"])
    backup = Path(r["backup"])
    assert backup.parent == paths.backups_dir() and backup.exists()
    from talent.store import Store
    old = Store(backup)
    assert [c["name"] for c in old.all_candidates()] == ["Before"]
    old.close()
    if os.name != "nt":
        assert mode(backup) == 0o600 and mode(backup.parent) == 0o700


def test_backups_are_pruned(store):
    for i in range(4):
        importers.backup_before_import(store, keep=2)
    assert len(list(paths.backups_dir().glob("before-import-*.db"))) == 2


def test_import_result_page_mentions_backup(store):
    c = local_client(store)
    r = c.post("/import", files={"file": ("p.csv", b"Name\nZed\n")})
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    r = c.post("/import/run", data={"token": token, "col0": "name"})
    assert "Added 1" in r.text and "backed up before this import" in r.text and "Undo this import" in r.text
    assert "—" not in r.text


# ------------------------------------------------------------ temp files
@posix_only
def test_upload_is_kept_privately_then_deleted(store):
    c = local_client(store)
    r = c.post("/import", files={"file": ("p.csv", b"Name\nZed\n")})
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    held = paths.imports_dir() / f"{token}.csv"
    assert held.exists() and mode(held) == 0o600 and mode(held.parent) == 0o700
    assert held.resolve().parent.parent == paths.data_dir().resolve()
    c.post("/import/run", data={"token": token, "col0": "name"})
    assert not held.exists()


def test_abandoned_upload_expires_after_an_hour(store):
    c = local_client(store)
    r = c.post("/import", files={"file": ("p.csv", b"Name\nZed\n")})
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    held = paths.imports_dir() / f"{token}.csv"
    old = time.time() - 3700
    os.utime(held, (old, old))
    assert "expired" in c.post("/import/run", data={"token": token, "col0": "name"}).text
    assert not held.exists() and store.count_candidates() == 0


# ----------------------------------------------------------- permissions
@posix_only
def test_data_dir_and_database_are_owner_only():
    d = paths.data_dir()
    assert mode(d) == 0o700
    db = paths.db_path()
    assert db.exists() and mode(db) == 0o600
    from talent.store import Store
    s = Store(db)
    s.add_candidate(name="Locked")
    s.close()
    assert mode(db) == 0o600


@posix_only
def test_existing_db_is_tightened(tmp_path):
    db = paths.data_dir() / "talent.db"
    db.touch()
    os.chmod(db, 0o644)
    paths.db_path()
    assert mode(db) == 0o600


# --------------------------------------------------------------- connect
def test_connect_does_nothing_when_app_is_missing(tmp_path):
    for fn, path in ((integrations.connect_claude, tmp_path / "Claude" / "claude_desktop_config.json"),
                     (integrations.connect_codex, tmp_path / ".codex" / "config.toml"),
                     (integrations.connect_lmstudio, tmp_path / ".lmstudio" / "mcp.json")):
        res = fn(path, SPEC)
        assert res["installed"] is False and "doesn't seem to be installed" in res["message"]
        assert not path.parent.exists()


def test_connect_page_says_app_is_missing(store, tmp_path, monkeypatch):
    monkeypatch.setattr("talent.integrations.lmstudio_config_path", lambda: tmp_path / ".lmstudio" / "mcp.json")
    c = local_client(store)
    page = c.get("/connect").text
    assert "Connect LM Studio" in page and "Connect Claude Desktop" in page and "Connect Codex" in page
    r = c.post("/connect/lmstudio")
    assert "LM Studio doesn&#39;t seem to be installed" in r.text and "Nothing was changed" in r.text
    assert not (tmp_path / ".lmstudio").exists()


@posix_only
def test_connect_keeps_one_backup_and_the_file_mode(tmp_path):
    p = tmp_path / "Claude" / "claude_desktop_config.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"theme": "dark"}))
    os.chmod(p, 0o600)
    for _ in range(3):
        res = integrations.connect_claude(p, SPEC)
    assert mode(p) == 0o600
    assert sorted(f.name for f in p.parent.iterdir()) == ["claude_desktop_config.json", "claude_desktop_config.json.bak"]
    assert res["backup"].endswith(".json.bak") and mode(res["backup"]) == 0o600
    assert "click + and Connectors" in res["next_step"]

    toml = tmp_path / ".codex" / "config.toml"
    toml.parent.mkdir()
    toml.write_text('model = "x"\n')
    os.chmod(toml, 0o640)
    integrations.connect_codex(toml, SPEC)
    integrations.connect_codex(toml, SPEC)
    assert mode(toml) == 0o640 and sorted(f.name for f in toml.parent.iterdir()) == ["config.toml", "config.toml.bak"]


@posix_only
def test_new_config_file_is_owner_only(tmp_path):
    p = tmp_path / ".lmstudio" / "mcp.json"
    p.parent.mkdir()
    integrations.connect_lmstudio(p, SPEC)
    assert mode(p) == 0o600


# ----------------------------------------------------------- reporting
def test_issue_link_strips_contact_details_and_links():
    url = recipes.issue_url("Import failed for jane@acme.com", "Call +1 (415) 555-0100, see https://x.example/a "
                            "and github.com/jdoe. Happened 2026-11-05 14:30.")
    q = parse_qs(urlsplit(url).query)
    blob = q["title"][0] + q["body"][0]
    for leak in ("jane@acme.com", "555-0100", "x.example", "github.com/jdoe"):
        assert leak not in blob
    assert "[email removed]" in q["title"][0] and "2026-11-05 14:30" in blob


def test_environment_line_has_no_kernel_release(monkeypatch):
    import platform
    monkeypatch.setattr(platform, "release", lambda: "6.18.44-secret-kernel")
    line = recipes.environment_line()
    assert "secret-kernel" not in line and "Python" in line and "Talent Search" in line


def test_report_problem_tool_scrubs(store):
    import asyncio
    from talent.mcp_server import build_server

    server = build_server(store)
    res = asyncio.run(server.call_tool("report_problem", {"summary": "Broke on bob@x.com", "details": ""}))
    out = res[1] if isinstance(res, tuple) else res
    q = parse_qs(urlsplit(out["url"]).query)
    assert "bob@x.com" not in q["title"][0] + q["body"][0] and "[email removed]" in q["title"][0]


# ------------------------------------------------------- LinkedIn paste
LINKEDIN = """Jane Doe
She/Her
· 2nd
Staff Engineer at Foo Corp
San Francisco, California · Contact info
500+ connections
Message
More
About
I build distributed systems.
Mentor and speaker. …see more
Experience
Foo Corp
Phone (415) 555-0100
github.com/jdoe
"""


def test_quick_parse_reads_a_linkedin_paste():
    got = importers.quick_parse(LINKEDIN)
    assert got["name"] == "Jane Doe"
    assert (got["current_title"], got["current_company"]) == ("Staff Engineer", "Foo Corp")
    assert got["location"] == "San Francisco, California"
    assert got["phone"] == "(415) 555-0100"
    assert got["website"] == "https://github.com/jdoe"
    assert got["source"] == "LinkedIn"
    assert got["about"] == "I build distributed systems.\nMentor and speaker."


def test_quick_parse_skips_noise_and_pronouns_on_the_name_line():
    got = importers.quick_parse("Bob Lee (He/Him) · 1st\nBackend dev | Go | k8s\nGreater Boston Area\nSite: boblee.dev\n"
                                "uses socket.io daily")
    assert got["name"] == "Bob Lee" and got["current_title"] == "Backend dev"
    assert got["location"] == "Greater Boston Area" and got["website"] == "https://boblee.dev"
    assert "source" not in got
    plain = importers.quick_parse("Ana Ruiz\nDesigner\nworks with socket.io")
    assert "website" not in plain and "location" not in plain


def test_paste_page_fills_linkedin_fields(store):
    r = local_client(store).post("/paste", data={"text": LINKEDIN})
    assert 'value="Jane Doe"' in r.text and 'value="San Francisco, California"' in r.text
    assert 'value="LinkedIn"' in r.text and 'value="https://github.com/jdoe"' in r.text
