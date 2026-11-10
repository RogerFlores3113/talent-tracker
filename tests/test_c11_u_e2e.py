"""c11 U in a real browser, inside the stand-in host (scripts/view_host.py). Skipped without Playwright."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pw = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def _post(port, name, args):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/call", method="POST",
                                 data=json.dumps({"name": name, "arguments": args}).encode(),
                                 headers={"content-type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    port = _free_port()
    dd = tmp_path_factory.mktemp("udata")
    env = {**os.environ, "TALENT_DATA_DIR": str(dd)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "view_host.py"), "--port", str(port),
                             "--data", str(dd), "--sample"], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tools", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("the view host did not start")
        past = lambda d: (datetime.now(timezone.utc) - timedelta(days=d)).replace(microsecond=0).isoformat()
        a = _post(port, "log_activity", {"candidate_id": 1, "kind": "interview", "summary": "Phone screen",
                                         "when": past(4), "job_id": 1, "interviewer": "Priya Shah"})
        _post(port, "add_interview_feedback", {"interview_id": a["structuredContent"]["logged"]["id"],
                                               "feedback": "Clear and kind.", "interviewer": "Priya Shah",
                                               "verdict": "strong yes"})
        _post(port, "log_activity", {"candidate_id": 6, "kind": "interview", "summary": "Screen", "when": past(3),
                                     "job_id": 1, "interviewer": "Priya Shah"})
        for i in range(3):
            _post(port, "save_candidate", {"name": "Sam Rivera", "current_company": f"Co{i}", "create_anyway": True})
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("Chromium is not available")
        yield b
        b.close()


def _run(browser, port, tool, args, dark=False, width=None):
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(f"http://127.0.0.1:{port}")
    page.wait_for_function("document.querySelector('#tool').options.length > 3")
    if dark:
        page.check("#dark")
    if width:
        page.evaluate(f"document.getElementById('chat').style.maxWidth='{width}px'")
    page.select_option("#tool", tool)
    page.fill("#args", json.dumps(args))
    page.click("#run")
    return page, page.frame_locator("iframe").last


def _sent(page):
    return page.evaluate("window.HOSTLOG.filter(m=>m.method==='ui/message').map(m=>m.params.content[0].text)")


def test_people_table_full_screen_shows_every_column_unclipped(host, browser):
    page, fr = _run(browser, host, "search_candidates", {"query": "", "limit": 10}, dark=True)
    fr.get_by_role("button", name="Show as table").click()  # c14 D2: the table no longer forces full screen
    fr.get_by_role("button", name="Full screen").click()  # it is its own button
    page.wait_for_function("document.querySelector('iframe').contentDocument === null || true")
    page.wait_for_timeout(800)
    assert fr.locator("html").get_attribute("data-mode") == "fullscreen"
    heads = [h.strip() for h in fr.locator("table.ptable thead th").all_inner_texts()]
    for want in ("Name", "Title", "Company", "Location", "Stage", "Match", "Last contacted", "Added", "Updated", "Color"):
        assert any(h.startswith(want) for h in heads), (want, heads)
    ok = fr.locator("body").evaluate("""() => {const w=document.querySelector('.tablewrap');const t=document.querySelector('table.ptable');
      const ths=[...t.querySelectorAll('thead th')].filter(x=>x.offsetParent);const r=w.getBoundingClientRect();
      return {w:[w.clientWidth,w.scrollWidth],ths:ths.map(x=>x.className+':'+Math.round(x.getBoundingClientRect().width)),scroll:w.scrollWidth<=w.clientWidth+1, inside:ths.every(x=>x.getBoundingClientRect().right<=r.right+1&&x.getBoundingClientRect().width>0)}}""")
    # c11 W: the table scrolls sideways inside its box when it must; the right edge shadow says so, and Name stays put
    edge = fr.locator(".tablebox .edge.r.on").count()
    assert (ok["scroll"] and ok["inside"]) or (not ok["scroll"] and edge == 1), (ok, edge)
    assert fr.locator("th.k-name").first.evaluate("e => getComputedStyle(e).position") == "sticky"
    page.close()


def test_people_table_stacks_at_360px(host, browser):
    page, fr = _run(browser, host, "search_candidates", {"query": "", "limit": 10}, width=360)
    fr.get_by_role("button", name="Show as table").click()
    page.wait_for_timeout(300)
    assert fr.locator("table.ptable thead").evaluate("e => getComputedStyle(e).position") == "absolute"
    assert fr.locator("body").evaluate("() => document.documentElement.scrollWidth <= 361")
    page.close()


def test_pipeline_card_has_chips_and_move_to_asks_claude(host, browser):
    page, fr = _run(browser, host, "job_pipeline", {"job_id": 1})
    fr.get_by_role("button", name="Open Maya Example summary").wait_for()
    assert fr.get_by_text("Strong yes 1").count() >= 1
    assert fr.get_by_text("Feedback owed (1)").count() == 1
    assert fr.locator("ul.stagecounts li").count() >= 3
    assert fr.get_by_role("button", name="Full screen").get_attribute("class").find("primary") >= 0
    fr.get_by_label("Move Maya Example to another stage").select_option("Offer")
    page.wait_for_timeout(300)
    assert not _sent(page)  # c11 W: picking a stage sends nothing; Move asks, Send sends
    fr.get_by_role("button", name="Move Maya Example").click()
    fr.get_by_role("button", name="Send").click()
    page.wait_for_timeout(300)
    msg = _sent(page)[-1]
    assert msg.startswith("Move Maya Example to Offer for Senior Backend Engineer (Acme Payments)")
    page.close()


def test_finalists_table_sorts_expands_and_asks_claude(host, browser):
    page, fr = _run(browser, host, "compare_finalists", {"job_id": 1})
    fr.get_by_role("button", name="Show feedback from Maya Example").click()
    assert fr.get_by_text("Clear and kind.").count() == 1
    fr.get_by_role("button", name="Sort by Feedback owed").click()
    rows = fr.locator("tbody tr:not(.qrow) th").all_inner_texts()
    assert rows[0].startswith("Maya") or rows[0].startswith("Kenji")
    fr.get_by_role("button", name="Advance Maya Example").click()
    fr.get_by_role("button", name="Yes, advance").click()
    page.wait_for_timeout(300)
    assert _sent(page)[-1].startswith("Advance Maya Example to ") and "for Senior Backend Engineer (Acme Payments)" in _sent(page)[-1]
    fr.get_by_role("button", name="Reject Maya Example").click()
    fr.get_by_role("button", name="Yes, reject Maya Example").click()
    page.wait_for_timeout(300)
    assert _sent(page)[-1].startswith("Reject Maya Example for Senior Backend Engineer (Acme Payments)") and "move them to Rejected" in _sent(page)[-1]
    page.close()


def test_today_person_row_opens_the_card_in_view(host, browser):
    page, fr = _run(browser, host, "list_action_items", {})
    fr.get_by_role("heading", name=re.compile("Interviews")).wait_for()
    fr.get_by_role("button", name=re.compile(r"Open Maya Example summary")).first.click()
    fr.get_by_role("button", name="Back", exact=True).wait_for()
    assert fr.get_by_role("article").count() == 1
    page.close()


def test_today_actions_ask_claude(host, browser):
    page, fr = _run(browser, host, "list_action_items", {})
    fr.get_by_role("button", name=re.compile(r"^Done: ")).first.click()
    page.wait_for_timeout(300)
    assert _sent(page)[-1].startswith("Mark my to-do as done")
    page.close()


def _propose(host):
    people = [{"name": "Rina Okafor", "linkedin_url": "https://www.linkedin.com/in/rina-okafor", "title": "Engineer",
               "company": "Paylane", "fit": "Settlement systems."},
              {"name": "Tomas Berg", "linkedin_url": "https://www.linkedin.com/in/tomas-berg", "title": "Principal",
               "company": "Northwind", "fit": "Go at scale."},
              {"name": "Maya Example", "linkedin_url": "https://www.linkedin.com/in/maya-x", "title": "Staff",
               "company": "Ledgerline", "fit": "Ledgers."}]
    return _post(host, "propose_candidates", {"job_id": 2, "people": people})


def test_shortlist_cards_show_saved_facts_and_named_linkedin_buttons(host, browser):
    _propose(host)
    page, fr = _run(browser, host, "list_prospects", {"job_id": 2})
    fr.get_by_role("button", name="LinkedIn profile of Rina Okafor").wait_for()
    box = fr.locator(".savedbox")
    assert box.count() >= 1 and "Ledgerline" in box.first.inner_text() and "Interviewing" in box.first.inner_text()
    assert fr.get_by_role("button", name="Open Maya Example summary").count() >= 1
    page.close()


def test_shortlist_table_mode_keeps_selected_with_the_existing_keys(host, browser):
    page, fr = _run(browser, host, "list_prospects", {"job_id": 2})
    fr.get_by_role("button", name="Show as a table").click()
    fr.get_by_role("columnheader", name=re.compile("Fit")).wait_for()
    fr.get_by_role("checkbox", name="Select Tomas Berg").check()
    fr.get_by_role("button", name="Keep 1 selected").click()
    fr.get_by_text("1 kept").first.wait_for(timeout=15000)
    assert "Kept" in fr.locator("tbody tr", has_text="Tomas Berg").inner_text()
    page.close()


def test_saved_view_lists_the_matching_people(host, browser):
    page, fr = _run(browser, host, "save_candidates", {"people": [{"name": "Sam Rivera", "title": "PM"},
                                                                  {"name": "Quiet Q", "company": "Bar"}], "job_id": 1})
    fr.get_by_text("The 3 saved people it could be").wait_for()
    assert fr.locator("ul.matches > li").count() == 3
    assert fr.get_by_text("No contact info").count() == 1
    fr.get_by_role("button", name=re.compile("^Same person: update Sam Rivera")).first.click()
    page.wait_for_timeout(300)
    assert "Update Sam Rivera (candidate id " in _sent(page)[-1]
    page.close()
