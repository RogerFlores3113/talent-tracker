"""c13 G2: the hiring manager view is safe to send, the share form says what the file holds, the compare page
follows focus and width."""
from __future__ import annotations

import json
import re
from datetime import datetime
from html import unescape
from pathlib import Path

from conftest import local_client
from talent import compare, xlsx_export

ROOT = Path(__file__).resolve().parent.parent
NAMES = ("Maya Ross", "Emily Chen", "Noah Park", "Rafael Diaz", "Ivy Stone", "Omar Haddad")


def people(store, n=3):
    return [store.add_candidate(name=NAMES[i], tags="python, sql", about=f"About {NAMES[i]}")["id"] for i in range(n)]


def q(ids):
    return ",".join(map(str, ids))


# ---------------------------------------------------------------- 1. hiring manager view
def test_hm_view_shows_active_stages_only(store):
    ids = people(store)
    job = store.add_job("Data Engineer", "Acme")
    lost = store.add_job("Designer", "Acme")
    store.add_to_pipeline(ids[0], job["id"], "Interview")
    store.add_to_pipeline(ids[0], lost["id"], "Rejected")
    store.add_to_pipeline(ids[1], lost["id"], "Withdrawn")
    html = local_client(store).get(f"/candidates/compare?ids={q(ids)}&view=hm").text
    assert "Data Engineer (Acme): Interview" in html
    for gone in ("Rejected", "Withdrawn", "Designer"):
        assert gone not in html, gone
    full = local_client(store).get(f"/candidates/compare?ids={q(ids)}").text
    assert "Rejected" in full and "Designer" in full  # the recruiter still sees everything


def test_hm_view_with_only_lost_stages_says_not_in_play(store):
    ids = people(store)
    lost = store.add_job("Designer", "Acme")
    store.add_to_pipeline(ids[0], lost["id"], "Rejected")
    html = local_client(store).get(f"/candidates/compare?ids={q(ids)}&view=hm").text
    # cycle 14 (F1): a person who is not in play is left out of the hiring manager view entirely, so with fewer than
    # two left there is nothing to compare; the page says so and never shows the job or stage
    assert "Not in any job" not in html and "1 person left out" in html and "Maya Ross is not in play" in html
    assert "About Maya Ross" not in html and "About Emily Chen" in html
    assert "Designer" not in html and "Rejected" not in html


def test_hm_view_words_headings_and_about_toggle(store):
    ids = people(store)
    c = local_client(store)
    html = c.get(f"/candidates/compare?ids={q(ids)}&view=hm").text
    assert "only what is fine to share" not in html
    assert "Hides your notes, contact dates, labels and links back to this app." in html
    assert "About text is shown as written: read it before you send this." in html
    assert '<h2 id="side-h"' in html and "Side by side" in html and "At a glance" in html
    assert "About Maya Ross" in html and 'id="hm-about"' in html and "Hide About" in html
    assert "view=hm&amp;about=0" in html
    off = c.get(f"/candidates/compare?ids={q(ids)}&view=hm&about=0").text
    assert "About Maya Ross" not in off and "Show About" in off and 'href="/candidates/compare?ids=' in off
    assert "about=0" not in off.split('id="hm-about"')[1].split("</a>")[0]  # the toggle goes back to showing it
    full = c.get(f"/candidates/compare?ids={q(ids)}&about=0").text
    assert "About Maya Ross" in full  # the toggle belongs to the hiring manager view only


# ---------------------------------------------------------------- 7. glance
def test_glance_talks_about_logged_contact(store):
    ids = people(store)
    job = store.add_job("Data Engineer", "Acme")
    store.add_to_pipeline(ids[0], job["id"], "Contacted")
    d = compare.build(store, ids)
    assert "No contact logged with anyone here yet." in d["glance"]
    assert not any("Nobody here has been contacted" in g for g in d["glance"])
    assert not any("contact" in g for g in d["glance_hm"])
    html = local_client(store).get(f"/candidates/compare?ids={q(ids)}").text
    assert "No contact logged with anyone here yet." in html


# ---------------------------------------------------------------- 5 and 6. compare focus and width
def test_remove_links_carry_a_focus_fragment(store):
    ids = people(store, 4)
    html = local_client(store).get(f"/candidates/compare?ids={q(ids)}").text
    first = re.search(r'id="rm-%d" href="([^"]+)"' % ids[0], html).group(1)
    assert unescape(first).endswith(f"#rm-{ids[1]}")  # the next remove button
    last = re.search(r'id="rm-%d" href="([^"]+)"' % ids[3], html).group(1)
    assert unescape(last).endswith(f"#rm-{ids[2]}")
    three = local_client(store).get(f"/candidates/compare?ids={q(ids[:3])}").text
    assert unescape(re.search(r'id="rm-%d" href="([^"]+)"' % ids[0], three).group(1)).endswith("#compare-region")
    assert 'id="compare-region"' in three
    assert "rm-" in (ROOT / "src/talent/static/app.js").read_text()


def test_wide_compare_uses_full_width_and_hints_at_scrolling(store):
    ids = people(store, 6)
    html = local_client(store).get(f"/candidates/compare?ids={q(ids)}").text
    assert 'class="compare-full"' in html and "Scroll sideways to see all 6 people." in html
    assert 'aria-describedby="compare-hint"' in html
    css = (ROOT / "src/talent/static/style.css").read_text()
    assert "body.compare-full main { max-width: none; }" in css and ".compare-hint.hint-6" in css
    two = local_client(store).get(f"/candidates/compare?ids={q(ids[:2])}").text
    assert "Scroll sideways" not in two and "compare-full" not in two


# ---------------------------------------------------------------- 2. share form
def test_share_notes_warning_and_preview_data(store):
    ids = people(store, 3)
    job = store.add_job("Senior Backend Engineer", "Acme")
    store.add_to_pipeline(ids[0], job["id"], "Interview")
    store.add_to_pipeline(ids[1], job["id"], "Rejected")
    c = local_client(store)
    html = c.get(f"/candidates/share?ids={q(ids)}").text
    warning = "Notes go out word for word and may hold pay, other interviews or private remarks. Read them first."
    assert warning in html
    assert re.search(r'id="notes-warn" hidden', html)
    shown = c.get(f"/candidates/share?ids={q(ids)}&notes=latest").text
    assert re.search(r'id="notes-warn" >', shown) or 'id="notes-warn" hidden' not in shown
    pv = json.loads(unescape(re.search(r"data-preview='([^']*)'", html).group(1)))
    mine = pv["jobs"][str(job["id"])]
    assert mine["out"]["count"] == 1 and "Emily Chen" not in mine["out"]["names"] and mine["out"]["stage"] is True
    assert mine["in"]["count"] == 2 and "Emily Chen" in mine["in"]["names"]  # the one not in the job stays out
    assert pv["jobs"][""]["out"]["stage"] is False


def test_share_preview_matches_the_file(store):
    ids = people(store, 4)
    job = store.add_job("Senior Backend Engineer", "Acme")
    other = store.add_job("Designer", "Acme")
    store.add_to_pipeline(ids[0], job["id"], "Interview")
    store.add_to_pipeline(ids[1], job["id"], "Rejected")
    store.add_to_pipeline(ids[2], job["id"], "Withdrawn")
    pv = xlsx_export.share_preview(store, ids, hl=[])
    for jid, rej in ((job["id"], False), (job["id"], True), (other["id"], False)):
        t = xlsx_export.share_table(store, ids, job_id=jid, include_rejected=rej)
        v = pv["jobs"][str(jid)]["in" if rej else "out"]
        assert v["count"] == len(t["rows"]) and v["names"] == [r[0] for r in t["rows"]][:3]
        assert v["stage"] == ("Stage" in t["headers"])
    assert "Stage" not in xlsx_export.share_table(store, ids, job_id=other["id"])["headers"]  # nobody is in it


def test_share_page_with_a_job_shows_the_file_it_makes(store):
    ids = people(store, 3)
    job = store.add_job("Senior Backend Engineer", "Acme")
    store.add_to_pipeline(ids[0], job["id"], "Interview")
    store.add_to_pipeline(ids[1], job["id"], "Rejected")
    html = local_client(store).get(f"/candidates/share?ids={q(ids)}&job={job['id']}").text
    assert '<dd id="pv-count">1 of 3' in html and "left out: 1 not in this job, 1 rejected or withdrawn" in html
    assert 'placeholder="Shortlist for Senior Backend Engineer"' in html
    plain = local_client(store).get(f"/candidates/share?ids={q(ids)}").text
    assert 'placeholder="A line for the top of the file"' in plain and "Shortlist for" not in plain


# ---------------------------------------------------------------- 3. file name
def test_share_file_name_never_uses_the_note(store):
    now = datetime(2026, 11, 10)
    assert xlsx_export.share_filename(None, now, "Pay is 250k, do not forward") == "shortlist-2026-11-10.xlsx"
    assert xlsx_export.share_filename({"title": "Senior Backend Engineer"}, now, "secret") == \
        "shortlist-senior-backend-engineer-2026-11-10.xlsx"
    ids = people(store, 2)
    res = local_client(store).post("/candidates/share", data={"ids": q(ids), "note": "Top secret picks"})
    assert "secret" not in res.headers["content-disposition"] and "shortlist-" in res.headers["content-disposition"]
    js = (ROOT / "src/talent/static/share.js").read_text()
    assert "note.value" not in js
