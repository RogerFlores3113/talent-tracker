from fastapi.testclient import TestClient

from talent.web import create_app


def client(store):
    return TestClient(create_app(store))


def test_empty_state_then_sample_data(store):
    c = client(store)
    assert "No candidates yet" in c.get("/").text
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
    assert r.status_code == 201 and r.json()["candidate"]["tags"] == "go, rust"
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
    assert "Connect to Claude" in c.get("/connect").text
    r = c.post("/connect/claude")
    assert "Quit and reopen Claude" in r.text
    assert (tmp_path / "claude.json").exists()
    assert "Connected." in c.get("/connect").text
    assert "Quit and reopen Codex" in c.post("/connect/codex").text
    assert c.post("/connect/nope").status_code == 404
