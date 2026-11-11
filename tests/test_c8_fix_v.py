"""Fix round V: every email a person has counts (a table of other emails used by import matching, the duplicate
guard, the finder, merge, search, the page, the MCP tools and the CSV export), and pasted "Title, Company" lines."""
import csv
import io
import json
import sqlite3
import time

import anyio
import pytest

from talent import duplicates, importers
from talent.importers import quick_parse, read_csv
from talent.mcp_server import build_server
from talent.store import DuplicateValue, Store

from conftest import local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def run_import(store, text):
    header, rows = read_csv(text)
    return importers.import_rows(store, header, rows, importers.guess_mapping(header, rows), backup=False)


GREENHOUSE = ("Name,Email,Email (Personal),Phone\n"
              "Maya Okafor,maya.okafor@acme.io,maya.home@gmail.com,415-555-0101\n")
LINKEDIN = ("First Name,Last Name,Email Address,Company,Position\n"
            "Maya,Okafor,maya.home@gmail.com,Acme,Staff Engineer\n")


# ------------------------------------------------------------------ the table
def test_other_emails_are_kept_in_a_table_and_lowercased(store):
    a = store.add_candidate(name="Ana Diaz", email="ana@work.com")["id"]
    assert store.add_other_emails(a, ["Ana.Home@Gmail.com", "ana@work.com", "ana.home@gmail.com", ""]) == ["ana.home@gmail.com"]
    assert store.other_emails(a) == ["ana.home@gmail.com"]
    assert store.all_emails(a) == ["ana@work.com", "ana.home@gmail.com"]
    assert store.candidate_detail(a)["other_emails"] == ["ana.home@gmail.com"]


def test_an_address_someone_else_has_is_refused(store):
    a = store.add_candidate(name="Ana Diaz", email="ana@work.com")["id"]
    b = store.add_candidate(name="Bo Li", email="bo@work.com")["id"]
    store.add_other_emails(b, ["bo.home@gmail.com"])
    with pytest.raises(DuplicateValue):
        store.add_other_emails(a, ["BO@work.com"])
    with pytest.raises(DuplicateValue):
        store.add_other_emails(a, ["bo.home@gmail.com"])
    with pytest.raises(ValueError):
        store.add_other_emails(a, ["not an email"])
    assert store.other_emails(a) == []


def test_add_and_edit_refuse_an_email_that_is_someones_other_email(store):
    a = store.add_candidate(name="Ana Diaz", email="ana@work.com")["id"]
    store.add_other_emails(a, ["ana.home@gmail.com"])
    with pytest.raises(DuplicateValue):
        store.add_candidate(name="Someone New", email="Ana.Home@gmail.com")
    b = store.add_candidate(name="Bo Li", email="bo@work.com")["id"]
    with pytest.raises(DuplicateValue):
        store.update_candidate(b, email="ana.home@gmail.com")
    assert store.add_candidate(name="Fine Person", email="fine@x.com")  # an unrelated address still works


def test_making_an_other_email_the_main_one_removes_it_from_the_others(store):
    a = store.add_candidate(name="Ana Diaz", email="ana@work.com")["id"]
    store.add_other_emails(a, ["ana.home@gmail.com"])
    store.update_candidate(a, email="ana.home@gmail.com")
    assert store.other_emails(a) == []


def test_search_finds_a_person_by_an_other_email(store):
    a = store.add_candidate(name="Ana Diaz", email="ana@work.com")["id"]
    store.add_other_emails(a, ["ana.home@gmail.com"])
    res = store.search_candidates("ANA.HOME@gmail.com")
    assert res.exact and [h.candidate["id"] for h in res.hits] == [a]
    assert store.find_duplicate("ana.home@gmail.com")["id"] == a


def test_the_migration_fills_other_emails_from_the_importers_notes(tmp_path):
    path = tmp_path / "old.db"
    s = Store(path)
    a = s.add_candidate(name="Ana Diaz", email="ana@work.com")["id"]
    b = s.add_candidate(name="Bo Li", email="bo@work.com")["id"]
    c = s.add_candidate(name="Cy Ng", email="cy@work.com")["id"]
    s.log(a, "note", "Other emails: ana.home@gmail.com, ana.old@yahoo.com")
    s.log(a, "note", "Email (Personal): ana.second@icloud.com")
    s.log(b, "note", "Other emails: ana.home@gmail.com, bo.home@gmail.com")  # first claim wins
    s.log(b, "note", "Spoke to bo@somewhere.com about it: he said call back")  # a person's own words: ignored
    s.log(c, "note", "Other emails: cy@work.com")  # their own main address: skipped
    s.close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE candidate_emails")
    migrations = __import__("talent.store", fromlist=["x"])._MIGRATIONS
    # the migration just before v9a's data step runs again, whatever later cycles appended after it
    version_before = next(i for i, m in enumerate(migrations) if isinstance(m, tuple)
                          and getattr(m[1], "__name__", "") == "_migrate_v9a_data")
    for col in ("pinned", "seen_max_id"):  # v10n, after v9a, is rewound too
        con.execute(f"ALTER TABLE saved_searches DROP COLUMN {col}")
    con.execute("ALTER TABLE candidates DROP COLUMN links")  # v11l, after v9a, is rewound too
    con.execute("DROP TABLE candidate_private_tags")  # v14, after this step, is rewound too
    con.execute("ALTER TABLE candidates DROP COLUMN private_tags")
    con.execute(f"PRAGMA user_version = {version_before}")
    con.commit()
    con.close()
    s = Store(path)
    assert s.other_emails(a) == ["ana.home@gmail.com", "ana.old@yahoo.com", "ana.second@icloud.com"]
    assert s.other_emails(b) == ["bo.home@gmail.com"]
    assert s.other_emails(c) == []
    assert s.timeline(a)  # the notes themselves are untouched
    s.close()


# ------------------------------------------------------------------ import
def test_a_linkedin_row_with_a_personal_email_updates_the_person_not_a_new_one(store):
    run_import(store, GREENHOUSE)
    assert store.count_candidates() == 1
    maya = store.find_by_name("Maya Okafor")[0]
    assert store.other_emails(maya["id"]) == ["maya.home@gmail.com"]
    result = run_import(store, LINKEDIN)
    assert store.count_candidates() == 1 and result["added"] == 0
    assert store.get_candidate(maya["id"])["current_company"] == "Acme"
    assert store.get_candidate(maya["id"])["email"] == "maya.okafor@acme.io"  # the main email is not replaced


def test_a_row_whose_emails_belong_to_two_people_is_left_alone(store):
    a = store.add_candidate(name="Maya Okafor", email="maya@a.com")["id"]
    b = store.add_candidate(name="Mia Chen", email="mia@b.com")["id"]
    text = "Name,Email,Other emails\nMaya Okafor,maya@a.com,mia@b.com\n"
    r = run_import(store, text)
    assert r["conflicts"] == 1 and r["added"] == 0 and store.count_candidates() == 2
    assert store.other_emails(a) == [] and store.other_emails(b) == []


def test_an_other_emails_column_is_read_and_exported_back(store, tmp_path):
    text = "Name,Email,Other emails\nMaya Okafor,maya@a.com,\"maya.home@gmail.com; maya.old@yahoo.com\"\n"
    header, rows = read_csv(text)
    assert importers.guess_mapping(header, rows)[2] == "other_emails"
    run_import(store, text)
    m = store.find_by_name("Maya Okafor")[0]
    assert store.other_emails(m["id"]) == ["maya.home@gmail.com", "maya.old@yahoo.com"]
    assert not [t for t in store.timeline(m["id"]) if "Other emails" in (t.get("body") or "")]  # no note for it
    out = list(csv.DictReader(io.StringIO(importers.export_csv(store))))
    assert out[0]["Other emails"] == "maya.home@gmail.com, maya.old@yahoo.com"
    fresh = Store(tmp_path / "fresh.db")
    run_import(fresh, importers.export_csv(store))
    again = fresh.find_by_name("Maya Okafor")[0]
    assert fresh.other_emails(again["id"]) == ["maya.home@gmail.com", "maya.old@yahoo.com"]
    fresh.close()


def test_two_rows_in_one_file_sharing_an_other_email_become_one_person(store):
    text = ("Name,Email,Other emails\n"
            "Maya Okafor,maya@a.com,maya.home@gmail.com\n"
            "Maya Okafor,maya.home@gmail.com,\n")
    run_import(store, text)
    assert store.count_candidates() == 1
    m = store.find_by_name("Maya Okafor")[0]
    assert m["email"] == "maya@a.com" and store.other_emails(m["id"]) == ["maya.home@gmail.com"]


def test_the_importers_existing_note_lines_are_still_written(store):
    run_import(store, GREENHOUSE)
    m = store.find_by_name("Maya Okafor")[0]
    bodies = [t["body"] for t in store.timeline(m["id"])]
    assert any("maya.home@gmail.com" in b and b.startswith("Email (Personal)") for b in bodies)


# ------------------------------------------------------------------ duplicates
def test_different_emails_label_only_when_the_two_share_no_email(store):
    a = raw(store, "Maya Okafor", "maya@acme.io", "Acme")
    b = raw(store, "Maya Okafor", "m.okafor@other.org", "Acme")
    raw_other(store, a, "maya.home@gmail.com")
    raw_other(store, b, "maya.home@gmail.com")  # (written around the store, as an older tool could have)
    pair = duplicates.find_pairs(store)[0]
    assert "same email" in pair["reasons"] and "same name and company, but different emails" not in pair["reasons"]
    exact = duplicates.exact_matches(store)
    assert len(exact) == 1 and {exact[0]["keep"], exact[0]["other"]} == {a, b}


def test_a_personal_email_that_is_someone_elses_main_email_makes_the_pair_exact(store):
    a = store.add_candidate(name="Maya Okafor", email="maya@acme.io")["id"]
    b = raw(store, "Maya Okafor", "maya.home@gmail.com", None)
    raw_other(store, a, "maya.home@gmail.com")
    exact = duplicates.exact_matches(store)
    assert len(exact) == 1 and {exact[0]["keep"], exact[0]["other"]} == {a, b}


def test_greenhouse_then_linkedin_overlap_can_be_bulk_merged(store):
    """The reported case: a person who exists twice because one copy has only the personal address."""
    a = store.add_candidate(name="Maya Okafor", email="maya.okafor@acme.io", current_company="Acme")["id"]
    store.add_other_emails(a, ["maya.home@gmail.com"])
    # an older import (before other emails were checked) left a second row on the personal address
    b = raw(store, "Maya Okafor", "maya.home@gmail.com", "Acme")
    c = local_client(store)
    assert "Maya Okafor" in c.get("/candidates/duplicates/merge-exact").text
    pairs = duplicates.exact_matches(store)
    assert len(pairs) == 1
    r = c.post("/candidates/duplicates/merge-exact", data={"pair": f"{pairs[0]['keep']}-{pairs[0]['other']}"},
               follow_redirects=False)
    assert r.status_code == 303 and store.count_candidates() == 1
    left = store.find_by_name("Maya Okafor")[0]["id"]
    assert set(store.all_emails(left)) == {"maya.okafor@acme.io", "maya.home@gmail.com"}


def raw(store, name, email, company):
    from test_c8_e import raw_add
    return raw_add(store, name, email=email, current_company=company)


def raw_other(store, cid, email):
    with store._lock, store._conn:
        store._conn.execute("DROP INDEX IF EXISTS candidate_emails_email_uq")  # as if two people had it before the rule
        store._conn.execute("INSERT INTO candidate_emails(candidate_id, email) VALUES (?, ?)", (cid, email))


# ------------------------------------------------------------------ merge
def test_merge_keeps_the_other_persons_emails_as_other_emails(store):
    a = store.add_candidate(name="Maya Okafor", email="maya@acme.io")["id"]
    b = store.add_candidate(name="Maya Okafor", email="maya.home@gmail.com")["id"]
    store.add_other_emails(b, ["maya.old@yahoo.com"])
    merged = store.merge_candidates(a, b)
    assert merged["email"] == "maya@acme.io"
    assert store.other_emails(a) == ["maya.home@gmail.com", "maya.old@yahoo.com"]
    assert store.find_duplicate("maya.old@yahoo.com")["id"] == a  # matchable now, not only text
    assert store.search_candidates("maya.old@yahoo.com").exact
    assert any("maya.home@gmail.com" in (t.get("body") or "") for t in store.timeline(a))


def test_merge_into_someone_with_no_email_does_not_double_list_the_address(store):
    a = store.add_candidate(name="Maya Okafor")["id"]
    b = store.add_candidate(name="Maya Okafor", email="maya@acme.io")["id"]
    store.add_other_emails(b, ["maya.home@gmail.com"])
    store.merge_candidates(a, b)
    assert store.get_candidate(a)["email"] == "maya@acme.io"
    assert store.other_emails(a) == ["maya.home@gmail.com"]


# ------------------------------------------------------------------ the page
def test_the_candidate_page_shows_other_emails_and_the_form_edits_them(store):
    a = store.add_candidate(name="Ana Diaz", email="ana@work.com")["id"]
    store.add_other_emails(a, ["ana.home@gmail.com"])
    c = local_client(store)
    page = c.get(f"/candidates/{a}")
    assert "Other emails" in page.text and "mailto:ana.home@gmail.com" in page.text
    form = c.get(f"/candidates/{a}?edit=1")
    assert 'name="other_emails"' in form.text and "ana.home@gmail.com" in form.text
    r = c.post(f"/candidates/{a}", data={"name": "Ana Diaz", "email": "ana@work.com",
                                         "other_emails": "ana.home@gmail.com\nAna.Old@yahoo.com"},
               follow_redirects=False)
    assert r.status_code == 303 and store.other_emails(a) == ["ana.home@gmail.com", "ana.old@yahoo.com"]
    c.post(f"/candidates/{a}", data={"name": "Ana Diaz", "email": "ana@work.com", "other_emails": "ana.old@yahoo.com"})
    assert store.other_emails(a) == ["ana.old@yahoo.com"]  # a deleted line is a removed address
    c.post(f"/candidates/{a}", data={"name": "Ana Diaz", "email": "ana@work.com", "other_emails": ""})
    assert store.other_emails(a) == []


def test_the_form_refuses_an_address_someone_else_has_and_escapes_it(store):
    a = store.add_candidate(name="Ana Diaz", email="ana@work.com")["id"]
    store.add_candidate(name="Bo Li", email="bo@work.com")
    r = local_client(store).post(f"/candidates/{a}", data={"name": "Ana Diaz", "other_emails": "bo@work.com"})
    assert r.status_code == 200 and "Bo Li" in r.text and store.other_emails(a) == []
    ok = local_client(store).post(f"/candidates/{a}", data={"name": "Ana Diaz", "other_emails": "<b>x</b>"})
    assert "<b>x</b>" not in ok.text


def test_new_person_form_matches_on_an_other_email(store):
    a = store.add_candidate(name="Ana Diaz", email="ana@work.com")["id"]
    store.add_other_emails(a, ["ana.home@gmail.com"])
    r = local_client(store).post("/candidates", data={"name": "Ana Diaz", "email": "ana.home@gmail.com"},
                                 follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/candidates/{a}")
    assert store.count_candidates() == 1


# ------------------------------------------------------------------ MCP
def test_get_candidate_shows_other_emails_and_the_ai_adds_them_only_to_someone_new(store):
    server = build_server(store)
    res = call(server, "save_candidate", name="Ana Diaz", email="ana@work.com",
               other_emails=["Ana.Home@gmail.com", "nonsense"])
    a = res.structuredContent["id"]
    assert store.other_emails(a) == ["ana.home@gmail.com"]
    got = call(server, "get_candidate", candidate_id=a)
    assert got.structuredContent["other_emails"] == ["ana.home@gmail.com"]
    assert "Other emails: ana.home@gmail.com" in got.content[0].text
    # An address says who someone is: on a person already saved, the AI's new one is reported, not added.
    again = call(server, "save_candidate", name="Ana Diaz", email="ana@work.com", other_emails=["evil@evil.example"])
    assert store.other_emails(a) == ["ana.home@gmail.com"]
    assert again.structuredContent["other_emails_not_added"] == ["evil@evil.example"]
    assert "Not added: evil@evil.example" in again.content[0].text
    # saving with only the other address finds the same person
    r2 = call(server, "save_candidate", name="Ana Diaz", email="ana.home@gmail.com")
    assert r2.structuredContent["id"] == a and store.count_candidates() == 1
    # the recruiter can add one in the app
    store.add_other_emails(a, ["ana.old@yahoo.com"])
    assert store.other_emails(a) == ["ana.home@gmail.com", "ana.old@yahoo.com"]
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert not [n for n in tools if "email" in n and ("remove" in n or "delete" in n)]


# ------------------------------------------------------------------ speed
def test_5000_row_import_stays_quick(tmp_path):
    s = Store(tmp_path / "big.db")
    lines = ["Name,Email,Email (Personal),Company"]
    for i in range(5000):
        lines.append(f"Person{i} Surname{i},p{i}@work{i % 50}.com,p{i}.home@gmail.com,Co{i % 200}")
    text = "\n".join(lines) + "\n"
    t0 = time.perf_counter()
    r = run_import(s, text)
    first = time.perf_counter() - t0
    assert r["added"] == 5000 and sum(len(s.other_emails(i)) for i in (1, 2500, 5000)) == 3
    again = "\n".join(["First Name,Last Name,Email Address,Company"] + [
        f"Person{i},Surname{i},p{i}.home@gmail.com,Co{i % 200}" for i in range(5000)]) + "\n"
    t0 = time.perf_counter()
    r2 = run_import(s, again)
    second = time.perf_counter() - t0
    assert s.count_candidates() == 5000 and r2["added"] == 0
    assert first < 60 and second < 60
    s.close()


# ------------------------------------------------------------------ paste: title and company lines
def test_signature_title_comma_company():
    out = quick_parse("""Thanks for the update, happy to chat Thursday.

Marcus Lee
Senior Engineer, Stripe
marcus.lee@stripe.com
(415) 555-0199
""")
    assert out["name"] == "Marcus Lee" and out["current_title"] == "Senior Engineer"
    assert out["current_company"] == "Stripe" and out["source"] == "Email signature"


def test_signature_recruiter_title_comma_company_and_at_form():
    out = quick_parse("Best,\nJo Park\nTechnical Recruiter, Ramp\njo@ramp.com\n")
    assert (out["current_title"], out["current_company"]) == ("Technical Recruiter", "Ramp")
    out = quick_parse("Best,\nPriya Raman\nStaff Product Designer at Figma\npriya@figma.com\n")
    assert (out["current_title"], out["current_company"]) == ("Staff Product Designer", "Figma")


RESUME_ONE_LINE = """Dana Whitfield
Senior Software Engineer, Datadog — Remote
dana.whitfield@gmail.com | (415) 555-0142 | linkedin.com/in/danawhitfield

Experience
Senior Software Engineer, Datadog — Remote
2021 - Present
Built the metrics pipeline.
Software Engineer, Gusto
2018 - 2021

Skills
Go, Kubernetes
"""


def test_resume_header_line_with_a_remote_tail_gives_title_company_and_history():
    out = quick_parse(RESUME_ONE_LINE)
    assert out["current_title"] == "Senior Software Engineer" and out["current_company"] == "Datadog"
    assert out["work_history"][0] == {"title": "Senior Software Engineer", "company": "Datadog",
                                      "start": "2021", "end": "now"}
    assert out["work_history"][1]["company"] == "Gusto"


def test_resume_header_line_alone_makes_a_work_history_entry():
    out = quick_parse("Dana Whitfield\nSenior Software Engineer, Datadog — Remote\n"
                      "dana.whitfield@gmail.com | (415) 555-0142\n")
    assert (out["current_title"], out["current_company"]) == ("Senior Software Engineer", "Datadog")
    assert [(h["title"], h["company"]) for h in out["work_history"]] == [("Senior Software Engineer", "Datadog")]


def test_resume_with_the_company_line_above_the_title():
    out = quick_parse("""Dana Whitfield
Datadog — Remote
Senior Software Engineer
dana.whitfield@gmail.com | (415) 555-0142

Experience
Datadog - Remote
Senior Software Engineer
2021 - Present
Gusto
Software Engineer
2018 - 2021
""")
    assert (out["current_title"], out["current_company"]) == ("Senior Software Engineer", "Datadog")
    assert [(h["title"], h["company"]) for h in out["work_history"]] == [
        ("Senior Software Engineer", "Datadog"), ("Software Engineer", "Gusto")]


def test_a_title_comma_team_or_a_place_is_not_split():
    out = quick_parse("Best,\nSam Ortiz\nSoftware Engineer, Backend\nsam@x.com\n")
    assert out["current_title"] == "Software Engineer, Backend" and not out.get("current_company")
    out = quick_parse("Best,\nSam Ortiz\nProduct Manager, Austin, TX\nsam@x.com\n")
    assert not out.get("current_company") or out["current_company"] != "TX"
