from fastapi.testclient import TestClient

from talent.web import create_app


def client(store):
    return TestClient(create_app(store))


def test_empty_state_then_sample_data(store):
    c = client(store)
    assert "Welcome to Talent Search" in c.get("/").text
    r = c.post("/sample-data", follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/?q=rust").text
    assert "Maya Okafor" in page and "Daniel Reyes" not in page


def test_htmx_returns_fragment_only(store):
    store.add_candidate(name="Ana", about="react")
    r = client(store).get("/?q=react", headers={"HX-Request": "true", "HX-Target": "results"})
    assert "<html" not in r.text and "Ana" in r.text


def test_candidate_text_is_escaped(store):
    store.add_candidate(name="<script>alert(1)</script>", about="pwn <img src=x onerror=alert(1)> react")
    html = client(store).get("/?q=react").text
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html
    assert "<mark>react</mark>" in html


def test_create_edit_delete_via_forms(store):
    c = client(store)
    r = c.post("/candidates", data={"name": "Form Person", "about": "golang"}, follow_redirects=False)
    assert r.status_code == 303
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    assert "Form Person" in c.get(f"/candidates/{cid}").text
    c.post(f"/candidates/{cid}", data={"name": "Form Person", "about": "", "location": "Reno"})
    got = store.get_candidate(cid)
    assert got["about"] is None and got["location"] == "Reno"
    assert "needs a name" in c.post("/candidates", data={"name": " "}).text
    c.post(f"/candidates/{cid}/delete")
    assert c.get(f"/candidates/{cid}").status_code == 404


def test_json_api(store):
    c = client(store)
    r = c.post("/api/candidates", json={"name": "Api", "email": "api@x.com", "tags": ["go", "rust"]})
    assert r.status_code == 201 and r.json()["candidate"]["tags"] == ["go", "rust"]
    again = c.post("/api/candidates", json={"name": "Api", "email": "API@x.com", "location": "Oslo"})
    assert again.status_code == 200 and again.json()["created"] is False
    cid = r.json()["candidate"]["id"]
    assert c.patch(f"/api/candidates/{cid}", json={"current_title": "CTO"}).json()["current_title"] == "CTO"
    assert c.get("/api/candidates?q=cto").json()["total"] == 1
    assert c.delete(f"/api/candidates/{cid}").status_code == 204
    assert c.get(f"/api/candidates/{cid}").status_code == 404
    assert c.get("/api/health").json()["search"] == "built-in"


def test_blocks_foreign_host_and_cross_site_posts(store):
    c = client(store)
    assert c.get("/", headers={"host": "attacker.example"}).status_code == 403
    assert c.post("/sample-data", headers={"origin": "https://attacker.example"}).status_code == 403
    assert c.post("/connect/claude", headers={"origin": "https://attacker.example"}).status_code == 403
    assert c.post("/sample-data", headers={"origin": "http://127.0.0.1:8765"}, follow_redirects=False).status_code == 303


def test_connect_page_and_button(store, tmp_path, monkeypatch):
    monkeypatch.setattr("talent.integrations.claude_config_path", lambda: tmp_path / "claude.json")
    monkeypatch.setattr("talent.integrations.codex_config_path", lambda: tmp_path / "codex" / "config.toml")
    c = client(store)
    assert "Connect Claude" in c.get("/connect").text
    r = c.post("/connect/claude")
    assert "Quit and reopen Claude" in r.text
    assert (tmp_path / "claude.json").exists()
    assert "Connected." in c.get("/connect").text
    assert "Quit and reopen Codex" in c.post("/connect/codex").text
    assert c.post("/connect/nope").status_code == 404


def test_every_page_renders_with_sample_data(store):
    from talent.sample_data import load_sample_data
    load_sample_data(store)
    c = client(store)
    job = store.list_jobs()[0]["id"]
    cid = store.recent_candidates(1)[0]["id"]
    for path in ["/", "/?q=rust", "/candidates", "/candidates?tag=python&sort=name&dir=asc", "/candidates/new",
                 f"/candidates/{cid}", f"/candidates/{cid}?edit=1", "/paste", "/import", "/jobs", "/jobs/new",
                 f"/jobs/{job}", f"/jobs/{job}?view=table", f"/jobs/{job}/edit", "/settings", "/recipes",
                 "/privacy", "/report", "/connect"]:
        r = c.get(path)
        assert r.status_code == 200, path
        assert "—" not in r.text, f"em-dash on {path}"
    home = c.get("/").text
    assert "Interview with Maya Okafor" in home and "Stuck" in home and "uses none of your data" in home
    assert c.get("/nope").status_code == 404 and c.get("/candidates/9999").status_code == 404


def test_pipeline_board_move_and_view_choice(store):
    c = client(store)
    cand = store.add_candidate(name="Mover")
    job = store.add_job("Role")
    c.post(f"/jobs/{job['id']}/add", data={"candidate_id": cand["id"]})
    r = c.post(f"/jobs/{job['id']}/move", data={"candidate_id": cand["id"], "stage": "Offer", "view": "board"},
               headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<html" not in r.text and 'data-stage="Offer"' in r.text
    assert store.pipeline(job["id"])[0]["stage"] == "Offer"
    c.get(f"/jobs/{job['id']}?view=table")
    assert store.setting("pipeline_view") == "table"
    assert "group-head" in c.get(f"/jobs/{job['id']}").text  # remembered
    r = c.post(f"/jobs/{job['id']}/stages", data={"op": "rename", "stage": "Offer", "new_name": "Offer out"},
               follow_redirects=False)
    assert r.status_code == 303 and store.pipeline(job["id"])[0]["stage"] == "Offer out"


def test_csv_import_flow_and_export(store):
    import re
    c = client(store)
    r = c.post("/import", files={"file": ("people.csv", b"Full Name,Email,Tech\nZed Q,zed@x.com,go\n")})
    assert r.status_code == 200 and "Goes to" in r.text
    token = re.search(r'name="token" value="(\w+)"', r.text).group(1)
    r = c.post("/import/run", data={"token": token, "col0": "name", "col1": "email", "col2": "tags"})
    assert "Added 1" in r.text
    assert "Zed Q" in c.get("/export.csv").text
    assert "expired" in c.post("/import/run", data={"token": token}).text  # temp copy removed


def test_paste_fills_form_and_flags_duplicates(store):
    store.add_candidate(name="Jo", email="jo@x.com")
    r = client(store).post("/paste", data={"text": "Jo Smith\nPM at Acme\njo@x.com"})
    assert 'value="Jo Smith"' in r.text and "already saved" in r.text


def test_settings_toggle_today_items(store):
    from talent.sample_data import load_sample_data
    load_sample_data(store)
    c = client(store)
    c.post("/settings", data={"flag_interviews": "1", "stuck_days": "3", "pipeline_view": "board"})
    s = store.settings()
    assert s["flag_stale"] == "0" and s["flag_interviews"] == "1" and s["stuck_days"] == "3"
    assert "Stale profile" not in c.get("/").text


def test_report_builds_github_link(store):
    r = client(store).post("/report", data={"summary": "Board is blank", "details": "after rename"})
    assert "https://github.com/RogerFlores3113/talent-tracker/issues/new?" in r.text.replace("&amp;", "&")
