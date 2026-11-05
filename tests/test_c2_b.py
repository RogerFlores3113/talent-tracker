"""Cycle 2, fixer B: the app window's key, open redirects, CSP, the changes page, Typesense, reports, connect."""
import json
import os
import re
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import LOCAL_URL, flash_of, local_client
from talent import actions, cli, integrations, paths, recipes
from talent.integrations import LaunchSpec
from talent.typesense_index import TypesenseIndex, env_status
from talent.web import LOCKED, key_cookie_name, safe_local_path

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
TEMPLATES = Path(paths.resource_dir()) / "templates"


def mode(p) -> int:
    return stat.S_IMODE(Path(p).stat().st_mode)


# ------------------------------------------------------------ 1. the key
@posix_only
def test_key_file_is_private_and_stable():
    key = paths.app_key()
    assert len(key) >= 32 and paths.app_key() == key
    assert mode(paths.data_dir() / "app.key") == 0o600


def test_window_is_locked_without_the_key(store):
    c = local_client(store, unlocked=False)
    r = c.get("/")
    assert r.status_code == 401 and "run talent open" in r.text and "unlock this window" in r.text
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    assert c.get("/candidates").status_code == 401
    assert c.get("/export.csv").status_code == 401 and c.get("/backup").status_code == 401
    assert c.post("/sample-data").status_code == 401
    api = c.get("/api/candidates")
    assert api.status_code == 401 and api.json()["detail"] == LOCKED
    assert c.post("/api/candidates", json={"name": "X"}).status_code == 401
    assert store.count_candidates() == 0
    assert c.get("/static/style.css").status_code == 200  # styles for the locked page itself
    assert c.get("/api/health").json() == {"ok": True, "version": c.get("/api/health").json()["version"]}


def test_header_key_works_for_scripts(store):
    c = local_client(store, unlocked=False)
    assert c.get("/api/candidates", headers={"X-Talent-Key": paths.app_key()}).status_code == 200
    assert c.get("/api/candidates", headers={"X-Talent-Key": "wrong"}).status_code == 401


def test_launch_code_sets_the_cookie_once(store):
    c = local_client(store, unlocked=False)
    code = paths.new_launch_code()
    r = c.get(f"/launch?code={code}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    name = key_cookie_name(paths.app_key())
    assert cookie.startswith(name + "=") and "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert "Max-Age=" in cookie and code not in cookie
    assert c.get("/").status_code == 200  # the browser now has the cookie
    fresh = local_client(store, unlocked=False)
    assert fresh.get(f"/launch?code={code}", follow_redirects=False).status_code == 401  # used up
    assert fresh.get("/launch?code=nope", follow_redirects=False).status_code == 401
    assert fresh.get("/launch", follow_redirects=False).status_code == 401


def test_launch_code_expires(store):
    code = paths.new_launch_code()
    old = time.time() - paths.LAUNCH_CODE_TTL - 5
    os.utime(paths.data_dir() / "launch.code", (old, old))
    assert local_client(store, unlocked=False).get(f"/launch?code={code}", follow_redirects=False).status_code == 401


def test_key_stays_out_of_links_and_the_port_file(store, monkeypatch):
    cli._write_port_file(8799)
    key = paths.app_key()
    assert paths.port_file().read_text() == "8799"
    assert key not in paths.app_url("/candidates/1")
    url = paths.launch_url(8799)
    assert url.startswith("http://127.0.0.1:8799/launch?code=") and key not in url


def test_open_and_second_launch_use_the_launch_url(monkeypatch):
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "_already_running", lambda port: port == 8811)
    cli._write_port_file(8811)
    assert cli.open_window() is True
    cli.run_app(port=8811)  # already running: just reopen the window
    assert len(opened) == 2 and all(u.startswith("http://127.0.0.1:8811/launch?code=") for u in opened)
    monkeypatch.setattr(cli, "_already_running", lambda port: False)
    assert cli.open_window() is False
    with pytest.raises(SystemExit):
        cli.main(["open"])


def test_api_cannot_delete(store):
    c = local_client(store)
    cid = c.post("/api/candidates", json={"name": "Keep"}).json()["candidate"]["id"]
    assert c.delete(f"/api/candidates/{cid}").status_code == 405
    assert store.count_candidates() == 1


# ------------------------------------------------------ 2. redirect, HEAD
@pytest.mark.parametrize("ref", [
    LOCAL_URL + "//evil.example/x", LOCAL_URL + "/\\evil.example/x",
    "https://evil.example/candidates", "http://127.0.0.1:9999/jobs", "http://localhost:8765/jobs",
])
def test_back_never_leaves_the_app(store, ref):
    r = local_client(store).post("/actions/snooze", data={"key": "x", "days": "1"}, headers={"referer": ref},
                                 follow_redirects=False)
    loc = r.headers["location"]
    assert r.status_code == 303 and urlsplit(loc).path == "/" and not loc.startswith("//")


def test_back_keeps_our_own_page(store):
    r = local_client(store).post("/actions/snooze", data={"key": "x", "days": "1"},
                                 headers={"referer": LOCAL_URL + "/jobs?status=open"}, follow_redirects=False)
    assert urlsplit(r.headers["location"]).path == "/jobs"
    assert not safe_local_path("/\t/evil.example") and not safe_local_path("//x") and not safe_local_path("x")
    assert not safe_local_path("/\\evil.example") and safe_local_path("/a/b")


def test_head_works_where_get_does(store):
    c = local_client(store)
    for path in ("/", "/candidates", "/settings", "/api/health"):
        r = c.head(path)
        assert r.status_code == 200 and r.content == b"", path


# ------------------------------------------------------------------ 3. CSP
def test_csp_header_on_every_page(store):
    c = local_client(store)
    for path in ("/", "/jobs", "/static/app.js"):
        csp = c.get(path).headers["content-security-policy"]
        assert "script-src 'self'" in csp and "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
    page = c.get("/").text
    assert '<script src="/static/htmx.min.js"' in page and '<script src="/static/app.js"' in page


def test_templates_have_no_inline_code():
    for f in TEMPLATES.glob("*.html"):
        text = f.read_text(encoding="utf-8")
        for tag, body in re.findall(r"<script\b([^>]*)>(.*?)</script>", text, re.I | re.S):
            assert "src=" in tag and not body.strip(), f.name
        assert not re.search(r"\son[a-z]+\s*=", text, re.I), f.name
        assert "javascript:" not in text.lower(), f.name


def test_app_js_keeps_the_behaviors():
    js = (Path(paths.resource_dir()) / "static" / "app.js").read_text(encoding="utf-8")
    for needle in ("data-confirm", "autosubmit", "runRecipe", "copyRecipe", "copyText", "dragstart", "moveUrl",
                   "claude://claude.ai/new?q="):
        assert needle.replace("data-", "") in js or needle in js, needle


def test_pages_use_data_attributes(store):
    c = local_client(store)
    job = store.add_job("Backend", "Acme")
    cand = store.add_candidate(name="Ana")
    store.add_to_pipeline(cand["id"], job["id"])
    jp = c.get(f"/jobs/{job['id']}").text
    assert f'data-move-url="/jobs/{job["id"]}/move"' in jp and "data-copy-text=" in jp and "data-confirm=" in jp
    assert 'data-confirm="Delete Ana? This also removes' in c.get(f"/candidates/{cand['id']}").text
    assert 'data-recipe="run"' in c.get("/recipes").text
    assert 'data-copy-from="json-snip"' in c.get("/connect").text


# ------------------------------------------------------------ 4. /changes
def test_changes_page_lists_and_filters(store):
    ana = store.add_candidate(name="Ana Diaz", email="ana@x.com")
    store.update_candidate(ana["id"], email="harvest@evil.example", by="ai")
    store.update_candidate(ana["id"], current_title="CTO", by="app")
    c = local_client(store)
    page = c.get("/changes").text
    assert "Ana Diaz" in page and "harvest@evil.example" in page and "ana@x.com" in page and "CTO" in page
    assert page.index("CTO") < page.index("harvest@evil.example")  # newest first
    ai = c.get("/changes?who=ai").text
    assert "harvest@evil.example" in ai and "CTO" not in ai
    assert "Undo all AI changes since" in ai
    assert "harvest@evil.example" in c.get("/changes?since=1").text
    assert 'href="/changes?who=ai"' in c.get("/settings").text and 'href="/changes?who=ai"' in c.get("/").text


def test_undo_one_change_from_the_changes_page(store):
    ana = store.add_candidate(name="Ana", email="ana@x.com")
    store.update_candidate(ana["id"], email="evil@x.com", by="ai")
    entry = [t for t in store.timeline(ana["id"]) if t["kind"] == "change"][0]
    c = local_client(store)
    r = c.post(f"/changes/{entry['id']}/undo", headers={"referer": LOCAL_URL + "/changes?who=ai"},
               follow_redirects=False)
    assert urlsplit(r.headers["location"]).path == "/changes"
    assert "Undid the change to Ana" in flash_of(r) and "notice" not in r.headers["location"]
    assert store.get_candidate(ana["id"])["email"] == "ana@x.com"
    again = c.post(f"/changes/{entry['id']}/undo", follow_redirects=False)
    assert "already undone" in flash_of(again, "flash_error")


def test_undo_all_ai_changes_since(store):
    ana = store.add_candidate(name="Ana", email="ana@x.com", phone="555 0100")
    bo = store.add_candidate(name="Bo", current_title="Dev")
    store.update_candidate(ana["id"], email="one@evil.example", by="ai")
    store.update_candidate(ana["id"], email="two@evil.example", phone="555 9999", by="ai")
    store.update_candidate(bo["id"], current_title="Hacker", by="ai")
    store.update_candidate(bo["id"], location="Reno", by="app")  # the recruiter's own edit stays
    c = local_client(store)
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    r = c.post("/changes/undo-ai", data={"since": since}, follow_redirects=False)
    notice = flash_of(r)
    assert notice.startswith("Undid 3 AI changes")
    a, b = store.get_candidate(ana["id"]), store.get_candidate(bo["id"])
    assert (a["email"], a["phone"]) == ("ana@x.com", "555 0100")
    assert b["current_title"] == "Dev" and b["location"] == "Reno"
    page = c.get("/changes?who=ai").text
    assert "Undo all AI changes since" not in page  # nothing left to undo
    assert "no AI changes" in flash_of(c.post("/changes/undo-ai", data={"since": since}, follow_redirects=False))
    bad = c.post("/changes/undo-ai", data={"since": "whenever"}, follow_redirects=False)
    assert flash_of(bad, "flash_error")


def test_undo_all_confirms_first(store):
    ana = store.add_candidate(name="Ana", email="ana@x.com")
    store.update_candidate(ana["id"], email="x@evil.example", by="ai")
    page = local_client(store).get("/changes?who=ai").text
    assert re.search(r'<form method="post" action="/changes/undo-ai"[^>]*data-confirm="Undo 1 change your AI app', page)


# --------------------------------------------------------- 5. Today item
def test_today_flags_ai_identity_changes(store):
    ana = store.add_candidate(name="Daniel Reyes", email="dan@x.com")
    bo = store.add_candidate(name="Bo", current_title="Dev")
    store.update_candidate(ana["id"], email="harvest@evil.example", by="ai")
    store.update_candidate(ana["id"], phone="555 1234", by="ai")
    store.update_candidate(bo["id"], current_title="Lead", by="ai")  # not an identity field
    store.update_candidate(bo["id"], email="bo@x.com", by="app")  # the recruiter's own edit
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "ai_change"]
    assert len(items) == 1
    assert items[0]["title"] == "Your AI app changed Daniel Reyes's email and phone. Review or undo."
    assert items[0]["href"].startswith("/changes")
    page = local_client(store).get("/").text
    assert "AI change" in page and "changed Daniel Reyes&#39;s email and phone" in page


def test_today_ai_item_goes_after_undo_and_after_a_week(store):
    ana = store.add_candidate(name="Ana", email="ana@x.com")
    store.update_candidate(ana["id"], name="Ana Evil", by="ai")
    entry = [t for t in store.timeline(ana["id"]) if t["kind"] == "change"][0]
    later = datetime.now(timezone.utc) + timedelta(days=8)
    assert not [i for i in actions.action_items(store, now=later)["items"] if i["kind"] == "ai_change"]
    assert [i for i in actions.action_items(store)["items"] if i["kind"] == "ai_change"]
    store.undo_change(entry["id"])
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "ai_change"]


# ---------------------------------------------------------- 6. Typesense
def test_typesense_refuses_plain_http_off_this_computer(monkeypatch):
    monkeypatch.setenv("TYPESENSE_API_KEY", "k")
    for url, ok in (("http://search.example.com:8108", False), ("ftp://x", False), ("http://", False),
                    ("https://search.example.com", True), ("http://localhost:8108", True),
                    ("http://127.0.0.1:8108", True), ("http://[::1]:8108", True)):
        monkeypatch.setenv("TYPESENSE_URL", url)
        assert (TypesenseIndex.from_env() is not None) is ok, url
        assert env_status()["refused"] is (not ok), url


def test_typesense_url_is_shown_on_privacy_and_settings(store, monkeypatch):
    monkeypatch.setenv("TYPESENSE_URL", "https://user:secret@search.example.com/ts")
    monkeypatch.setenv("TYPESENSE_API_KEY", "k")
    c = local_client(store)
    for path in ("/privacy", "/settings"):
        page = c.get(path).text
        assert "https://search.example.com/ts" in page and "secret" not in page and "Typesense is on" in page
    monkeypatch.setenv("TYPESENSE_URL", "http://search.example.com")
    assert "ignores it" in c.get("/settings").text
    monkeypatch.delenv("TYPESENSE_URL")
    assert "Typesense" not in c.get("/privacy").text


def test_privacy_page_tells_the_truth_about_copies(store):
    page = local_client(store).get("/privacy").text
    assert "unencrypted" in page and "backups" in page and "locked with a key" in page


@posix_only
def test_existing_default_data_folder_is_tightened(tmp_path, monkeypatch):
    monkeypatch.delenv("TALENT_DATA_DIR")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(mode=0o755)
    os.chmod(tmp_path / "data", 0o755)
    assert mode(paths.data_dir()) == 0o700
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "notes.txt").write_text("someone else's file")
    os.chmod(mine, 0o755)
    monkeypatch.setenv("TALENT_DATA_DIR", str(mine))
    assert mode(paths.data_dir()) == 0o755  # a picked folder that holds other files keeps its permissions


# ------------------------------------------------------------ 7. reports
def test_report_shows_a_preview_before_the_link(store):
    r = local_client(store).post("/report", data={"summary": "Search broke", "details": "mail me at a@b.com",
                                                  "where": "/"})
    page = r.text
    assert "Search broke" in page and "a@b.com" not in page and "What happened" in page
    assert "Opening it shows GitHub your text. It is posted only when you press Submit on GitHub." in page
    assert page.index("What happened") < page.index("Open the report on GitHub")
    title, body = recipes.issue_text("Search broke", "x", "app")
    assert title == "Search broke" and "**Where**\napp" in body


# -------------------------------------------------------- 8. suggestions
def test_suggestion_links_are_plain_text(store):
    store.add_candidate(name="Ana")
    store.add_suggestion("Read https://evil.example/x before calling", source="AI app")
    page = local_client(store).get("/").text
    assert "https://evil.example/x" in page and 'href="https://evil.example' not in page


# -------------------------------------------------------------- 9. connect
SPEC = LaunchSpec("/opt/talent/talent-search", ["mcp"], {"TALENT_DATA_DIR": "/data"})


def test_connect_keeps_the_first_backup(tmp_path):
    p = tmp_path / "Claude" / "claude_desktop_config.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"theme": "dark"}))
    integrations.connect_claude(p, SPEC)
    integrations.connect_claude(p, SPEC)
    assert json.loads((p.parent / (p.name + ".bak")).read_text()) == {"theme": "dark"}
    toml = tmp_path / ".codex" / "config.toml"
    toml.parent.mkdir()
    toml.write_text('model = "x"\n')
    integrations.connect_codex(toml, SPEC)
    integrations.connect_codex(toml, SPEC)
    assert (toml.parent / "config.toml.bak").read_text() == 'model = "x"\n'


def test_connect_page_names_other_ai_apps(store):
    page = local_client(store).get("/connect").text
    assert "Other AI apps" in page and "ChatGPT and others" not in page
    assert "ChatGPT itself can't connect to apps on this computer yet." in page


def test_my_pages_have_no_dashes(store):
    ana = store.add_candidate(name="Ana", email="ana@x.com")
    store.update_candidate(ana["id"], email="x@evil.example", by="ai")
    c = local_client(store)
    pages = [c.get(p).text for p in ("/changes", "/changes?who=ai", "/privacy", "/connect", "/settings", "/")]
    pages += [c.post("/report", data={"summary": "Broke"}).text, local_client(store, unlocked=False).get("/").text]
    for text in pages:
        assert "—" not in text and " – " not in text


def test_head_never_spends_a_launch_code(store):
    code = paths.new_launch_code()
    c = local_client(store, unlocked=False)
    assert c.head(f"/launch?code={code}").status_code == 405
    assert c.get(f"/launch?code={code}", follow_redirects=False).status_code == 303
