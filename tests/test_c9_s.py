"""Cycle 9, implementer S: Stricter site guards (a Settings switch that guides and limits the AI app on sites whose
terms forbid automated collection). Off by default; recruiter actions are never limited."""
from __future__ import annotations

import socket
from pathlib import Path

import anyio
import pytest
from mcp.types import CallToolResult

from talent import recipes, site_guard
from talent.mcp_server import INSTRUCTIONS, build_server

from conftest import local_client

DASHES = ("—", "–")
SITES = ("linkedin.com", "indeed.com", "glassdoor.com", "facebook.com", "instagram.com")


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def prompt(server, name, **args) -> str:
    async def go():
        return await server.get_prompt(name, args)
    res = anyio.run(go)
    return "\n".join(m.content.text for m in res.messages)


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def data(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1]


def on(store):
    store.set_settings(strict_site_guards="1")


def li(n: int) -> dict:
    return {"name": f"Person Number{chr(65 + n // 26)}{chr(65 + n % 26)}",
            "linkedin_url": f"https://www.linkedin.com/in/pn{n}", "source": "LinkedIn"}


def test_default_off_and_setting_saves_and_shows(store):
    assert store.setting("strict_site_guards") == "0"
    c = local_client(store)
    assert "Stricter site guards" in c.get("/settings").text
    checked = 'name="strict_site_guards" value="1" checked'
    assert checked not in c.get("/settings").text
    r = c.post("/settings", data={"flags_shown": "1", "strict_site_guards": "1"}, follow_redirects=False)
    assert r.status_code == 303 and store.setting("strict_site_guards") == "1"
    assert checked in c.get("/settings").text
    c.post("/settings", data={"flags_shown": "1"}, follow_redirects=False)  # the Settings page, unticked
    assert store.setting("strict_site_guards") == "0"
    c.post("/settings", data={"strict_site_guards": "1"}, follow_redirects=False)
    assert store.setting("strict_site_guards") == "1"
    c.post("/settings", data={"theme": "ocean"}, follow_redirects=False)  # a partial form that leaves it out
    assert store.setting("strict_site_guards") == "1"


def test_settings_connect_and_privacy_pages_say_it_plainly(store):
    c = local_client(store)
    for path in ("/settings", "/privacy"):
        html = c.get(path).text
        assert all(d in html for d in SITES)
        assert "can't control what your AI app does" in html
    for path in ("/connect", "/settings"):
        html = c.get(path).text
        assert "automated collection isn't allowed" in html and "Stricter site guards" in html


def test_instructions_only_mention_it_when_on(store):
    assert "Stricter site guards" not in INSTRUCTIONS
    assert "Stricter site guards" not in build_server(store).instructions
    on(store)
    ins = build_server(store).instructions
    assert "The recruiter turned on Stricter site guards" in ins and "linkedin.com" in ins


def test_recipes_and_prompts_only_when_on(store):
    job = {"id": 1, "title": "Engineer", "company": "Acme", "location": "", "description": ""}
    for fn in (lambda g: recipes.source_for_job(job, guard=g), lambda g: recipes.save_from_linkedin("x", guard=g),
               lambda g: recipes.refresh_stale(guard=g), lambda g: recipes.refresh_candidate({"id": 1}, guard=g)):
        assert "Stricter site guards" not in fn(False)
        assert "turned on Stricter site guards" in fn(True)
    server = build_server(store)
    c = local_client(store)
    assert "turned on Stricter site guards" not in prompt(server, "save_from_linkedin", url="u")
    assert "turned on Stricter site guards" not in c.get("/recipes").text
    on(store)
    assert "turned on Stricter site guards" in prompt(server, "save_from_linkedin", url="u")
    assert "turned on Stricter site guards" in prompt(server, "source_candidates")
    assert "turned on Stricter site guards" in c.get("/recipes").text


def test_results_mention_guard_only_when_on_and_listed(store):
    server = build_server(store)
    assert "Stricter site guards" not in text(call(server, "save_candidate", **li(0)))
    on(store)
    r = call(server, "save_candidate", **li(1))
    assert "Stricter site guards" in text(r) and "linkedin.com" in data(r)["site_guard_note"]
    other = call(server, "save_candidate", name="Casey Plain", email="casey@example.com", source="Referral")
    assert "Stricter site guards" not in text(other)
    assert "Stricter site guards" in text(call(server, "save_candidates", people=[li(2)]))
    plain = call(server, "save_candidates", people=[{"name": "Dana Plain", "email": "d@example.com", "source": "Referral"}])
    assert "Stricter site guards" not in text(plain)
    job = store.add_job(title="Engineer", company="Acme")
    p = call(server, "propose_candidates", job_id=job["id"],
             people=[{"name": "Eve Listed", "linkedin_url": "https://linkedin.com/in/eve"}])
    assert "Stricter site guards" in text(p)
    p2 = call(server, "propose_candidates", job_id=job["id"], people=[{"name": "Fay Plain", "source": "Conference"}])
    assert "Stricter site guards" not in text(p2)


def test_cap_applies_only_to_ai_saves_from_listed_sites_when_on(store):
    server = build_server(store)
    for n in range(12):  # off: no guard cap
        call(server, "save_candidate", **li(n))
    on(store)
    for n in range(100, 110):
        call(server, "save_candidate", **li(n))
    with pytest.raises(Exception) as err:
        call(server, "save_candidate", **li(120))
    assert "The recruiter turned on Stricter site guards" in str(err.value) and "10" in str(err.value)
    # a person from another source still saves, and so does an update to someone already saved
    assert data(call(server, "save_candidate", name="Gus Plain", email="gus@example.com", source="Referral"))["created"]
    assert data(call(server, "save_candidate", **{**li(100), "location": "Austin"}))["created"] is False
    res = data(call(server, "save_candidates", people=[li(130)]))
    assert res["counts"]["not_saved"] == 1 and "Stricter site guards" in res["not_saved"][0]["reason"]
    job = store.add_job(title="Engineer", company="Acme")
    with pytest.raises(Exception) as err2:
        call(server, "propose_candidates", job_id=job["id"],
             people=[{"name": "Hal Listed", "linkedin_url": "https://www.indeed.com/p/hal"}])
    assert "Stricter site guards" in str(err2.value)
    assert call(server, "propose_candidates", job_id=job["id"], people=[{"name": "Ivy Plain", "source": "Referral"}])


def test_propose_counts_toward_the_same_cap(store):
    on(store)
    server = build_server(store)
    job = store.add_job(title="Engineer", company="Acme")
    people = [{"name": f"Prospect Number{chr(65 + n)}", "linkedin_url": f"https://linkedin.com/in/p{n}"}
              for n in range(10)]
    call(server, "propose_candidates", job_id=job["id"], people=people)
    assert site_guard.left(store) == 0
    with pytest.raises(Exception):
        call(server, "save_candidate", **li(200))


def test_recruiter_actions_are_never_limited(store):
    on(store)
    c = local_client(store)
    for n in range(15):
        r = c.post("/candidates", data={"name": f"Hand Added{chr(65 + n)}",
                                         "linkedin_url": f"https://www.linkedin.com/in/h{n}", "source": "LinkedIn",
                                         "create_anyway": "1"}, follow_redirects=False)
        assert r.status_code in (200, 303)
    assert site_guard.left(store) == site_guard.CAP  # none of it counted
    r = c.post("/paste", data={"text": "Jo Smith\nhttps://www.linkedin.com/in/josmith\nEngineer"})
    assert r.status_code == 200
    server = build_server(store)
    assert data(call(server, "save_candidate", **li(300)))["created"]  # the AI still has its full allowance


def test_matching_sites_and_names():
    assert site_guard.listed_site("https://uk.linkedin.com/in/x")
    assert site_guard.listed_site(source="Indeed resume search") == "indeed.com"
    assert site_guard.listed_site(source="found on glassdoor.com") == "glassdoor.com"
    assert site_guard.listed_site("https://notlinkedin.com/x", source="Referral") is None
    assert site_guard.listed_site(source="mail a@linkedin.com") is None
    assert site_guard.listed_site("https://github.com/x", source="GitHub") is None


def test_no_dashes_in_new_copy_and_no_network(store, monkeypatch):
    real_connect = socket.socket.connect

    def refuse(self, address, *a, **k):
        # Windows' asyncio builds its self-pipe from a loopback socket pair; only
        # an address off this machine counts as a network call.
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address, *a, **k)
        raise AssertionError(f"network call to {address!r}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    on(store)
    server = build_server(store)
    copy = [site_guard.instruction_line(), site_guard.result_note("linkedin.com"), site_guard.cap_text(3),
            server.instructions, prompt(server, "save_from_linkedin", url="u")]
    for s in copy:
        assert not any(d in s for d in DASHES)
    note = (Path(site_guard.__file__).parent / "templates" / "_site_note.html").read_text()
    assert not any(d in note for d in DASHES)
    call(server, "save_candidate", **li(1))
    assert local_client(store).get("/settings").status_code == 200
