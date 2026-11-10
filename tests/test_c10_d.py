"""Cycle 10 D: paste a resume or note reads every role, strips trailing punctuation, labels the source
honestly and says what was NOT found; the Connect page speaks to a recruiter first."""
import sys

import pytest

from talent import importers

PLAIN_EM = """Priya Sharma
Senior Data Engineer at Streamline
San Francisco, CA
priya.sharma@example.com | (415) 555-0142

Experience
Streamline — Senior Data Engineer 2021 - Present
Acme Corp — Data Engineer 2018 - 2021

Education
UC Davis, BS Computer Science
"""

MANY_FORMATS = """Tomas Berg
tomas.berg@example.com
https://tomasberg.dev.

WORK EXPERIENCE
Staff Engineer, Northwind Labs, Jan 2020 - Present
Software Engineer at Contoso (2019-2021)
Developer | Fabrikam | 03/2018 – 06/2020
Intern, Initech, June 2016 to Aug 2016

Skills: Go, Rust, Kubernetes
"""

STACKED = """Aisha Rahman
Product Manager
aisha@example.org

Professional Experience

Product Manager
Globex Corporation
Mar 2019 - Present

Associate Product Manager
Hooli
2016 – 2019

Education
Stanford University
"""

NOTE = ("Hi Dana, someone passed me this: Marcus Lee is a staff backend engineer at Vercel, based in Austin, TX. "
        "He's great with Go and Postgres. Email marcus.lee@example.com, phone 512-555-0188, and his github is "
        "https://github.com/marcuslee. Worth a call!")

NOTE_TWO = ("Met Jordan Ellis at the conference. Jordan is a Product Designer at Figma in New York. "
            "jordan.ellis@example.com.")

SIGNATURE = """Thanks,

Sam Ortiz
VP of Engineering | Northwind Labs
sam@northwind.example
+1 (206) 555-0111
"""

LINKEDIN = """Lee Wu
Backend Engineer | Go, Rust
Greater Boston Area
Contact info
500+ connections

Experience
Backend Engineer
Stripe · Full-time
Jan 2021 - Present · 3 yrs

Software Engineer
Wayfair · Full-time
Jun 2017 - Dec 2020 · 3 yrs 7 mos
"""

TITLE_FIRST = """Marta Nowak
marta.nowak@example.com

Employment History
Recruiting Manager — Globex, 2020 to Present
Recruiter - Initech (Jan 2017 – Dec 2019)
"""


def roles(guess):
    return [(h["title"], h["company"], h["start"], h["end"]) for h in guess.get("work_history", [])]


def test_company_dash_title_with_years_reads_every_role():
    g = importers.quick_parse(PLAIN_EM)
    assert roles(g) == [("Senior Data Engineer", "Streamline", "2021", "now"),
                        ("Data Engineer", "Acme Corp", "2018", "2021")]
    assert g["source"] == "Resume" and g["name"] == "Priya Sharma"


def test_many_date_and_line_formats():
    g = importers.quick_parse(MANY_FORMATS)
    assert roles(g) == [("Staff Engineer", "Northwind Labs", "Jan 2020", "now"),
                        ("Software Engineer", "Contoso", "2019", "2021"),
                        ("Developer", "Fabrikam", "03/2018", "06/2020"),
                        ("Intern", "Initech", "June 2016", "Aug 2016")]
    assert g["website"] == "https://tomasberg.dev"  # no trailing dot
    assert g["tags"] == ["Go", "Rust", "Kubernetes"]


def test_stacked_resume_title_company_dates_lines():
    g = importers.quick_parse(STACKED)
    assert roles(g) == [("Product Manager", "Globex Corporation", "Mar 2019", "now"),
                        ("Associate Product Manager", "Hooli", "2016", "2019")]


def test_title_dash_company_and_either_order():
    g = importers.quick_parse(TITLE_FIRST)
    assert roles(g) == [("Recruiting Manager", "Globex", "2020", "now"),
                        ("Recruiter", "Initech", "Jan 2017", "Dec 2019")]


def test_prose_note_gets_name_title_company_place_and_clean_link():
    g = importers.quick_parse(NOTE)
    assert (g["name"], g["current_title"], g["current_company"], g["location"]) == (
        "Marcus Lee", "Staff Backend Engineer", "Vercel", "Austin, TX")
    assert g["website"] == "https://github.com/marcuslee"
    assert g["email"] == "marcus.lee@example.com" and g["source"] == "Note"


def test_second_note_takes_the_full_name_from_earlier():
    g = importers.quick_parse(NOTE_TWO)
    assert (g["name"], g["current_title"], g["current_company"]) == ("Jordan Ellis", "Product Designer", "Figma")
    assert g["email"] == "jordan.ellis@example.com" and g["source"] == "Note"


def test_signature_and_linkedin_keep_their_labels():
    assert importers.quick_parse(SIGNATURE)["source"] == "Email signature"
    g = importers.quick_parse(LINKEDIN)
    assert g["source"] == "LinkedIn" and len(g["work_history"]) == 2


def test_a_note_with_no_name_is_a_note_not_a_signature():
    g = importers.quick_parse("Found a good one, her email is dev.ops@example.com.")
    assert g["source"] == "Note" and g["email"] == "dev.ops@example.com"


@pytest.mark.parametrize("text,url", [
    ("Ana Ruiz\nDesigner\nSee https://ana.design/work.", "https://ana.design/work"),
    ("Ana Ruiz\nDesigner\nPortfolio: www.anaruiz.com.", "https://www.anaruiz.com"),
    ("Ana Ruiz\nDesigner\n(https://anaruiz.dev/)", "https://anaruiz.dev/"),
])
def test_trailing_punctuation_is_stripped_from_links(text, url):
    assert importers.quick_parse(text)["website"] == url


def test_hints_say_what_was_not_found():
    g = importers.quick_parse(NOTE)
    hints = importers.paste_hints(NOTE, g)
    assert any(h.startswith("No work history found") for h in hints)
    full = importers.quick_parse(PLAIN_EM)
    assert importers.paste_hints(PLAIN_EM, full) == []


def test_hints_flag_a_partial_read_and_missing_dates():
    text = "Zed Ng\nzed@example.com\n\nExperience\nEngineer at Foo 2020 - 2022\nSome other job 2018 - 2020\n"
    g = importers.quick_parse(text)
    hints = importers.paste_hints(text, g)
    assert any("Only 1 of 2 roles were read" in h for h in hints)
    assert any("No name" in h for h in importers.paste_hints("x@example.com", importers.quick_parse("x@example.com")))
    assert importers.paste_hints("", {"name": "A", "current_title": "T", "work_history": [
        {"title": "T", "company": "C", "start": None, "end": "now"}]}) == [
        "1 role has no dates. Check the work history below."]


def test_paste_page_shows_hints(client):
    r = client.post("/paste", data={"text": NOTE})
    assert r.status_code == 200
    assert 'id="paste-hints"' in r.text and "No work history found" in r.text
    assert "Marcus Lee" in r.text
    assert 'value="https://github.com/marcuslee"' in r.text


# ----------------------------------------------------------------- Connect page
def _connect_env(tmp_path, monkeypatch, claude=True, codex=True, lmstudio=True):
    root = tmp_path / "apps"
    for name, on in (("claude", claude), ("codex", codex), ("lmstudio", lmstudio)):
        if on:
            (root / name).mkdir(parents=True)
    monkeypatch.setattr("talent.integrations.claude_config_path", lambda: root / "claude" / "claude.json")
    monkeypatch.setattr("talent.integrations.codex_config_path", lambda: root / "codex" / "config.toml")
    monkeypatch.setattr("talent.integrations.lmstudio_config_path", lambda: root / "lmstudio" / "mcp.json")
    return root


def test_connect_missing_apps_say_so_and_still_offer_the_others(store, tmp_path, monkeypatch):
    from conftest import local_client
    _connect_env(tmp_path, monkeypatch, claude=False)
    page = local_client(store).get("/connect").text
    assert "Claude Desktop isn&#39;t installed on this computer." in page or "Claude Desktop isn't installed" in page
    assert "Get Claude Desktop" in page and ">Connect Claude<" not in page
    assert ">Connect Codex<" in page and ">Connect LM Studio<" in page  # the other apps are still offered


def test_connect_installed_app_has_one_button(store, tmp_path, monkeypatch):
    from conftest import local_client
    _connect_env(tmp_path, monkeypatch)
    page = local_client(store).get("/connect").text
    assert ">Connect Claude<" in page and "isn't installed" not in page and "isn&#39;t installed" not in page


def test_connect_page_leads_with_the_plain_question_and_folds_the_jargon(store, tmp_path, monkeypatch):
    from conftest import local_client
    _connect_env(tmp_path, monkeypatch)
    page = local_client(store).get("/connect").text
    fold = page.index("Details for technical users")
    assert page.index("Step 1. Connect your AI app") < page.index('id="claude-panel"') < fold
    plain = page[:fold]
    for jargon in ("MCP", "mcpServers", "config.toml", "JSON", "Show full report"):
        assert jargon not in plain, jargon
    for jargon in ("MCP", "mcpServers", "config.toml", "Show full report"):
        assert jargon in page[fold:]
    for hook in ('id="claude-panel"', 'id="claude-code-panel"', 'id="codex-panel"', 'id="skills-panel"',
                 'id="by-hand"', 'id="claude-report"'):
        assert hook in page


def test_connect_buttons_still_work(store, tmp_path, monkeypatch):
    from conftest import local_client
    root = _connect_env(tmp_path, monkeypatch)
    c = local_client(store)
    monkeypatch.setattr(sys, "platform", "linux")
    assert "Open Claude and start a new chat" in c.post("/connect/claude").text
    assert (root / "claude" / "claude.json").exists()
    assert "Connected." in c.get("/connect").text and "Test connection" in c.get("/connect").text
    assert c.post("/connect/codex").status_code == 200 and (root / "codex" / "config.toml").exists()
    assert c.post("/connect/lmstudio").status_code == 200 and (root / "lmstudio" / "mcp.json").exists()
    assert c.post("/connect/claude/report").status_code == 200
