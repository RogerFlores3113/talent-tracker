"""Fixer Y: import email columns, the paste parser, forward-only re-imports, and the duplicate finder's
bulk actions."""
from __future__ import annotations

import re

from conftest import flash_of, local_client

from talent import duplicates, importers


def _read(text):
    return importers.read_csv(text)


def _run(store, text, **kw):
    header, rows = _read(text)
    mapping = kw.pop("mapping", None) or importers.guess_mapping(header, rows)
    kw.setdefault("backup", False)
    kw.setdefault("add_stages", True)
    return importers.import_rows(store, header, rows, mapping, **kw)


def _token(r):
    return re.search(r'name="token" value="(\w+)"', r.text).group(1)


# ------------------------------------------------------ 1. no email column is skipped silently
GREENHOUSE = ("Candidate ID,Candidate Name,Email,Email (Personal),Personal Email Address,Current Stage,Recruiter\n"
              "1001,Dana Whitfield,dana@work.example,dana.w@home.example,,Phone Screen,Marcus\n"
              "1002,Eli Park,,eli.park@home.example,eli@alt.example,Phone Screen,Marcus\n"
              "1003,Fay Okoro,fay@work.example,,,Application Review,Marcus\n")


def test_every_email_looking_column_is_mapped_or_called_out():
    header, rows = _read(GREENHOUSE)
    mapping = importers.guess_mapping(header, rows)
    for h, m in zip(header, mapping):
        if "mail" in h.lower():
            assert m, f"{h} was skipped"
    assert importers.skipped_email_columns(header, mapping) == []
    skipped = [""] * len(header)
    assert importers.skipped_email_columns(header, skipped) == ["Email", "Email (Personal)", "Personal Email Address"]
    assert importers.skipped_email_columns(["Name", "Email Status", "Email Opt In"], ["name", "", ""]) == []


def test_personal_email_becomes_the_email_when_there_is_none_else_a_note(store):
    _run(store, GREENHOUSE)
    eli = store.find_duplicate("eli.park@home.example")
    assert eli and eli["name"] == "Eli Park"
    notes = [t["body"] for t in store.timeline(eli["id"]) if t["kind"] == "note"]
    assert any("eli@alt.example" in n for n in notes)
    dana = store.find_duplicate("dana@work.example")
    notes = [t["body"] for t in store.timeline(dana["id"]) if t["kind"] == "note"]
    assert any(n.startswith("Email (Personal):") and "dana.w@home.example" in n for n in notes)


def test_a_skipped_email_column_is_named_on_the_map_screen_and_in_the_result(store):
    c = local_client(store)
    r = c.post("/import", files={"file": ("gh.csv", GREENHOUSE.encode())})
    assert 'id="email-skipped"' not in r.text  # all three are matched
    header, rows = _read(GREENHOUSE)
    mapping = importers.guess_mapping(header, rows)
    cols = {f"col{i}": m for i, m in enumerate(mapping)}
    cols["col3"] = ""
    r = c.post("/import/check", data={"token": _token(r), "filename": "gh.csv", **cols})
    assert "Email (Personal)" in r.text and "left out" in r.text
    r = c.post("/import/run", data={"token": _token(r), "filename": "gh.csv", **cols})
    assert 'id="email-skipped"' in r.text and "Email (Personal)" in r.text


# ------------------------------------------------------ 2. paste parser
REPLY = """Hi Lena,

Thanks for reaching out about the Staff Engineer role. I'd be glad to chat next week. Tuesday afternoon works well for me, and I can do Thursday too.

Best,
Dr. Amara Okafor-Smith, PhD
Director of Machine Learning | Northwind Labs
Portland, OR 97214
(503) 555-0142
amara.okafor@northwind.example
https://www.linkedin.com/in/amara-okafor-smith
"""


def test_a_pasted_reply_reads_the_signature_not_the_greeting_or_body():
    g = importers.quick_parse(REPLY)
    assert g["name"] == "Amara Okafor-Smith"
    assert g["current_title"] == "Director of Machine Learning" and g["current_company"] == "Northwind Labs"
    assert g["email"] == "amara.okafor@northwind.example"
    assert g["phone"] == "(503) 555-0142"
    assert g["linkedin_url"].endswith("/in/amara-okafor-smith")
    assert g["location"] == "Portland, OR"
    assert "about" not in g or "Thanks for reaching out" not in g["about"]
    assert g["source"] != "Resume"


def test_credentials_and_honorifics_are_dropped_from_a_signature_name():
    sig = "Sam Lee, MBA\nVP of Partnerships at Brightwave\nsam.lee@brightwave.example\n+1 415 555 0188\n"
    g = importers.quick_parse(sig)
    assert g["name"] == "Sam Lee" and g["current_company"] == "Brightwave"
    g = importers.quick_parse("Best,\n" + sig)
    assert g["name"] == "Sam Lee"
    g = importers.quick_parse("Hello Sam\n\nSounds good.\n\nThanks,\nDr. Sam Lee, PhD, MBA\nCTO, Brightwave\nsam@brightwave.example\n")
    assert g["name"] == "Sam Lee"


LINKEDIN_PAGE = """Skip to main content
Home
My Network
Jobs
Messaging
Notifications
Me
For Business
Jamie Park
Senior Software Engineer
Stripe
San Francisco Bay Area · 500+ connections · Contact info
"""


def test_linkedin_page_chrome_is_not_a_name_and_a_company_line_fills_the_company():
    g = importers.quick_parse(LINKEDIN_PAGE)
    assert g["name"] == "Jamie Park"
    assert g["current_title"] == "Senior Software Engineer"
    assert g["current_company"] == "Stripe"
    assert g["location"] == "San Francisco Bay Area"
    assert g["source"] == "LinkedIn"


RESUME = """Priya Raman
Portland, OR 97214 | priya@example.com | (503) 555-0100

Experience
Staff Engineer
Datadog — Remote
Jan 2021 - Present
Built things.
"""


def test_a_location_suffix_is_stripped_from_the_company_and_a_zip_is_read():
    g = importers.quick_parse(RESUME)
    assert g["current_company"] == "Datadog"
    assert g["work_history"][0]["company"] == "Datadog"
    assert g["location"] == "Portland, OR"
    assert g["phone"] == "(503) 555-0100"
    assert importers._strip_place("Acme - Platform Team") == "Acme - Platform Team"
    assert importers._strip_place("Acme - Austin, TX") == "Acme"


def test_paste_with_an_existing_email_and_a_different_name_says_so(store):
    store.add_candidate(name="Olu Bankole", email="olu@x.example")
    c = local_client(store)
    r = c.post("/paste", data={"text": "Maria Santos\nProduct Designer at Figma\nolu@x.example\n"})
    assert 'id="paste-name-differs"' in r.text and "Olu Bankole" in r.text and "Maria Santos" in r.text
    assert "will add these details" not in r.text
    r = c.post("/paste", data={"text": "Olu Bankole\nProduct Designer at Figma\nolu@x.example\n"})
    assert 'id="paste-name-differs"' not in r.text and "saving will add these details" in r.text


# ------------------------------------------------------ 3. re-imports never move people backwards
WEEK1 = ("Name,Email,Job,Client,Status\n"
         "Ana Diaz,ana@x.com,iOS Engineer,Initech,Recruiter Screen\n"
         "Bo Li,bo@x.com,iOS Engineer,Initech,Submitted to Client\n"
         "Cy Ng,cy@x.com,iOS Engineer,Initech,Recruiter Screen\n")
WEEK2 = ("Name,Email,Job,Client,Status\n"
         "Ana Diaz,ana@x.com,iOS Engineer,Initech,Submitted to Client\n"
         "Bo Li,bo@x.com,iOS Engineer,Initech,Recruiter Screen\n"
         "Cy Ng,cy@x.com,iOS Engineer,Initech,Recruiter Screen\n")


def _stage(store, name):
    job = next(j for j in store.list_jobs() if j["title"] == "iOS Engineer")
    return next(a["stage"] for a in store.pipeline(job["id"]) if a["candidate_name"] == name)


def test_a_reimport_moves_forward_and_holds_back_moves_backwards(store):
    _run(store, WEEK1)
    p = importers.preview_import(store, *_read(WEEK2), importers.guess_mapping(*_read(WEEK2)), add_stages=True,
                                 move_stages=True)
    assert len(p["moved"]) == 1 and p["held_back"] == 1
    line = importers.preview_lines(p)[0]
    assert "move 1 person" in line and "hold back 1 move" in line
    r = _run(store, WEEK2, move_stages=True)
    assert r["held_back"] == 1 and _stage(store, "Bo Li") == "Submitted to Client"
    assert _stage(store, "Ana Diaz") == "Submitted to Client"
    assert any("further along" in x for x in r["already_in"])


def test_backwards_moves_happen_only_when_asked(store):
    _run(store, WEEK1)
    r = _run(store, WEEK2, move_stages=True, allow_backward=True)
    assert r["held_back"] == 0 and r["moved_backward"] == 1 and _stage(store, "Bo Li") == "Recruiter Screen"


LEVER = ("Name,Emails,Phones,Links,Origin,Stage\n"
         "Ana Diaz,ana@x.com,,,Applied,Recruiter Screen\n")
GH_WEEK = ("Candidate ID,Candidate Name,Email,Current Stage,Recruiter,Job,Client\n"
           "9,Ana Diaz,ana@x.com,Submitted to Client,Marcus,iOS Engineer,Initech\n")


def test_a_file_from_a_different_tool_does_not_move_stages_unless_ticked(store):
    header, rows = _read(LEVER)
    assert importers.detect_export(header) == "Lever"
    assert importers.other_tool(store, header) is None  # nothing imported before
    importers.save_sync_profile(store, header, importers.guess_mapping(header, rows), {"stage_order": []},
                                file_name="lever.csv")
    gh_header, _ = _read(GH_WEEK)
    assert importers.detect_export(gh_header) == "Greenhouse"
    assert importers.other_tool(store, gh_header) == "Lever"
    assert importers.other_tool(store, header) is None
    c = local_client(store)
    r = c.post("/import", files={"file": ("gh.csv", GH_WEEK.encode())})
    assert 'id="other-tool"' in r.text and "Lever" in r.text
    assert not re.search(r'name="move_stages" value="1"\s+checked', r.text)


def test_origin_column_becomes_a_tag(store):
    header, rows = _read(LEVER)
    mapping = importers.guess_mapping(header, rows)
    assert mapping[header.index("Origin")] == "tag_column"
    _run(store, LEVER)
    assert "Origin: Applied" in store.find_duplicate("ana@x.com")["tags"]


# ------------------------------------------------------ 4. duplicate finder
def test_same_name_and_company_is_dropped_when_linkedin_differs_and_demoted_when_emails_differ(store):
    co = "Initech"
    store.add_candidate(name="Sam Rivera", current_company=co, linkedin_url="https://linkedin.com/in/sam-rivera-1")
    store.add_candidate(name="Sam Rivera", current_company=co, linkedin_url="https://linkedin.com/in/sam-rivera-2")
    store.add_candidate(name="Kim Tran", current_company=co, email="kim@work.example")
    store.add_candidate(name="Kim Tran", current_company=co, email="kim.tran@home.example")
    store.add_candidate(name="Lee Moss", current_company=co, email="lee@a.example", phone="415 555 0101")
    store.add_candidate(name="Lee Moss", current_company=co, email="lee@b.example", phone="415 555 0101")
    pairs = duplicates.find_pairs(store)
    names = {store.get_candidate(p["keep"])["name"]: p["reasons"] for p in pairs}
    assert "Sam Rivera" not in names
    assert names["Kim Tran"] == ["same name and company, but different emails"]
    assert "same name and phone" in names["Lee Moss"]  # another signal agrees, so it stays as it was
    assert pairs[-1]["reasons"][0].endswith("different emails")


def test_exact_matches_list_and_bulk_merge_with_a_preview_and_checkboxes(store):
    a = store.add_candidate(name="Ana Diaz", email="ana.diaz@gmail.com", phone="415 555 0100")["id"]
    b = store.add_candidate(name="Ana Diaz", email="anadiaz+jobs@gmail.com")["id"]  # the same Gmail address
    store.add_candidate(name="Zed Kay", email="zed@x.example")
    store.add_candidate(name="Zed Kay", email="zed@y.example")  # same name only: not an exact match
    pairs = duplicates.exact_matches(store)
    assert len(pairs) == 1 and set(pairs[0]["reasons"]) & set(duplicates.STRONG)
    c = local_client(store)
    page = c.get("/candidates/duplicates?view=pairs")
    assert "Merge all exact email or LinkedIn matches (1)" in page.text and 'form="bulk-dismiss"' in page.text
    prev = c.get("/candidates/duplicates/merge-exact")
    assert prev.status_code == 200 and 'name="pair"' in prev.text and "checked" not in prev.text  # destructive: nothing starts ticked
    pair = f"{pairs[0]['keep']}-{pairs[0]['other']}"
    r = c.post("/candidates/duplicates/merge-exact", data={"pair": [pair, "999-1000"]}, follow_redirects=False)
    assert "Merged 1 pair" in flash_of(r)
    assert duplicates.exact_matches(store) == []
    left = [x for x in (a, b) if store._row("SELECT id FROM candidates WHERE id = ?", (x,))]
    assert len(left) == 1


def test_bulk_merge_refuses_pairs_that_are_not_exact_matches(store):
    a = store.add_candidate(name="Kim Tran", current_company="Initech", email="kim@w.example")["id"]
    b = store.add_candidate(name="Kim Tran", current_company="Initech", email="kim@h.example")["id"]
    c = local_client(store)
    r = c.post("/candidates/duplicates/merge-exact", data={"pair": [f"{a}-{b}"]}, follow_redirects=False)
    assert "No pairs were ticked" in flash_of(r, "flash_error")
    store.get_candidate(a)
    store.get_candidate(b)


def test_bulk_not_the_same_person(store):
    ids = []
    for n, who in enumerate(("Kim Tran", "Lee Moss", "Ana Ruiz")):
        x = store.add_candidate(name=who, current_company="Initech", email=f"kim{n}@w.example")["id"]
        y = store.add_candidate(name=who, current_company="Initech", email=f"kim{n}@h.example")["id"]
        ids.append((x, y))
    c = local_client(store)
    r = c.post("/candidates/duplicates/dismiss-many", data={"pair": [f"{x}-{y}" for x, y in ids[:2]]},
               follow_redirects=False)
    assert "2 pairs" in flash_of(r)
    assert len(duplicates.dismissed(store)) == 2
    assert len(duplicates.find_pairs(store)) == 1
    r = c.post("/candidates/duplicates/dismiss-many", data={}, follow_redirects=False)
    assert "No pairs were ticked" in flash_of(r, "flash_error")


def test_same_person_panel_lists_only_plausible_matches(store):
    omar = store.add_candidate(name="Omar Silva", phone="415 555 0199")["id"]
    chris = store.add_candidate(name="Chris Chen", phone="415 555 0199")["id"]
    omar2 = store.add_candidate(name="Omar Silva", email="omar@x.example")["id"]
    c = local_client(store)
    page = c.get(f"/candidates/{omar}").text
    assert "Same person?" in page and f"/candidates/{omar2}" in page and "Chris Chen" not in page
    duplicates.dismiss(store, omar, omar2)
    assert "Same person?" not in c.get(f"/candidates/{omar}").text
    assert chris
