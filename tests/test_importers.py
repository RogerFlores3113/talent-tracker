import csv
import io
from urllib.parse import parse_qs, urlsplit

from talent import importers, recipes


def test_guess_mapping_handles_common_headers():
    header = ["First Name", "Last Name", "E-mail", "LinkedIn Profile URL", "Job Title", "Company", "City", "Skills", "Notes"]
    assert importers.guess_mapping(header) == [
        "first_name", "last_name", "email", "linkedin_url", "current_title", "current_company", "location", "tags", "about"]


def test_import_merges_and_reports_skips(store):
    text = "Name,Email,Skills\nAna Diaz,ana@x.com,go; rust\n,nobody@x.com,\nAna Diaz,ANA@x.com,python\n"
    header, rows = importers.read_csv(text)
    r = importers.import_rows(store, header, rows, importers.guess_mapping(header))
    assert r == {"added": 1, "updated": 1, "skipped": ["Row 3: no name"]}
    ana = store.search_candidates("ana").hits[0].candidate
    assert ana["tags"] == ["go", "rust", "python"] and ana["source"] == "CSV import"


def test_semicolon_csv_and_odd_encodings():
    header, rows = importers.read_csv(importers.decode("Name;Ville\nRené;Paris\n".encode("cp1252")))
    assert header == ["Name", "Ville"] and rows == [["René", "Paris"]]


def test_export_round_trips_and_blocks_formulas(store):
    store.add_candidate(name="=HYPERLINK(1)", tags="go", work_history=[{"title": "Dev", "company": "Acme", "start": "2020"}])
    out = list(csv.reader(io.StringIO(importers.export_csv(store))))
    assert out[0][0] == "Name" and out[0][-1] == "Work history"
    assert out[1][0] == "'=HYPERLINK(1)" and out[1][-1] == "Dev at Acme (2020-now)"


def test_quick_parse_pulls_out_the_basics():
    got = importers.quick_parse("JANE DOE\nStaff Engineer at Foo Corp\njane.doe@Example.com · +1 (415) 555-0100\n"
                                "https://www.linkedin.com/in/janedoe/\nhttps://github.com/jdoe\n2018 - 2021 Bar Inc")
    assert got["name"] == "Jane Doe" and got["current_title"] == "Staff Engineer" and got["current_company"] == "Foo Corp"
    assert got["email"] == "jane.doe@example.com" and got["phone"].startswith("+1")
    assert got["linkedin_url"].startswith("https://www.linkedin.com/in/janedoe") and got["website"] == "https://github.com/jdoe"
    assert importers.quick_parse("   ") == {}


def test_recipes_and_issue_links():
    for r in recipes.RECIPES:
        assert recipes.render(r.key).strip()
    p = recipes.source_for_job({"id": 3, "title": "SRE", "company": "Acme", "location": "Remote", "description": "k8s"})
    assert "SRE at Acme" in p and "job id 3" in p and "k8s" in p and "Ask me before saving" in p
    assert recipes.claude_link("hi there").startswith("claude://claude.ai/new?q=hi%20there")
    url = recipes.issue_url("Search is slow", "Takes 5s")
    q = parse_qs(urlsplit(url).query)
    assert q["title"] == ["Search is slow"] and "Takes 5s" in q["body"][0] and "Talent Search" in q["body"][0]
    # No em-dashes in anything the user reads.
    assert all("—" not in recipes.render(r.key) for r in recipes.RECIPES)
