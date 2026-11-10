"""Cycle 14 F2 in a real browser (the strict view host). Skipped without Playwright."""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pw = pytest.importorskip("playwright.sync_api")

SEED = r"""
from talent.store import Store
from talent.paths import db_path
s = Store(db_path())
s.add_candidate(name="Ana Ruiz", email="ana@x.com", current_title="Data Engineer II", current_company="Acme", tags="python, sql",
                about="Builds data pipelines for payments.\nLoves small teams.\nThird line that waits behind More.")
for i in range(30):
    s.add_candidate(name=f"Pat Person{i:02d}", email=f"p{i}@x.com" if i % 2 else None, current_title="Data Engineer" if i % 3 == 0 else "Designer",
                    current_company="Acme", tags="python" if i % 4 == 0 else "go")
for i in range(3):
    s.add_candidate(name="Dana Dup", email=f"d{i}@x.com", current_title="Engineer", current_company="Zed")
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    if os.environ.get("F2_VIEW_HOST_PORT"):  # a view host already running (Playwright in another Python)
        yield int(os.environ["F2_VIEW_HOST_PORT"])
        return
    port = _free_port()
    dd = tmp_path_factory.mktemp("f2data")
    env = {**os.environ, "TALENT_DATA_DIR": str(dd)}
    subprocess.run([sys.executable, "-c", SEED], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "view_host.py"), "--port", str(port), "--data", str(dd)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tools", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("the view host did not start")
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


def _run(browser, port, tool, args):
    page = browser.new_page(viewport={"width": 1400, "height": 1600})
    page.goto(f"http://127.0.0.1:{port}")
    page.wait_for_function("document.querySelector('#tool').options.length > 3")
    page.select_option("#tool", tool)
    page.fill("#args", json.dumps(args))
    page.click("#run")
    return page, page.frame_locator("iframe").last


def _no_rejects(page):
    assert page.evaluate("window.HOSTREJECTS") == []  # the strict host turned nothing away


def test_card_shows_the_summary_and_a_title_pick_button(browser, host):
    page, fr = _run(browser, host, "search_candidates", {"query": "Ana Ruiz", "limit": 5, "highlight": ["python"]})
    fr.locator(".pcard").wait_for()
    summary = fr.locator("p.summary")
    summary.wait_for()  # the card arrives a moment after the list
    assert summary.inner_text().startswith("Builds data pipelines")
    assert "clamp2" in (summary.get_attribute("class") or "")
    more = fr.get_by_role("button", name=re.compile("More about Ana"))
    more.click()
    assert "clamp2" not in (summary.get_attribute("class") or "")
    assert fr.get_by_role("button", name=re.compile("Less about Ana")).get_attribute("aria-expanded") == "true"
    title = fr.get_by_role("button", name="Highlight title: Data Engineer II")
    assert title.inner_text() == "Data Engineer II"  # the person's own case
    title.click()
    bar = fr.locator('.hlbar button[data-skill="title:data engineer"]')
    assert bar.get_attribute("aria-label") == "Remove highlight: title: Data Engineer (yellow, pick 2)"
    assert fr.locator('.skillchips button[data-skill="python"]').get_attribute("data-hl") == "red"
    assert "Top match: " in fr.locator(".tophint").inner_text()
    # a title pick never colors a skill chip
    assert fr.locator('.skillchips button[data-skill="sql"]').get_attribute("data-hl") is None
    assert "Of everyone found:" in fr.locator(".hlbar").inner_text()
    _no_rejects(page)
    page.close()


def test_a_column_sort_really_sorts_and_can_go_back(browser, host):
    page, fr = _run(browser, host, "search_candidates", {"query": "", "limit": 40, "highlight": ["python"]})
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    first_by_pick = fr.locator("tbody th[scope=row]").all_inner_texts()
    fr.get_by_role("button", name="Sort by Name").click()
    names = [n.strip() for n in fr.locator("tbody th[scope=row]").all_inner_texts()]
    assert names == sorted(names, key=str.lower)  # every loaded row, not only inside the highlight tiers
    assert "Highlight tiers are set aside while sorting by a column" in fr.locator("table.ptable caption").inner_text()
    fr.get_by_role("button", name="Back to highlight order").click()
    assert fr.locator("tbody th[scope=row]").all_inner_texts() == first_by_pick
    _no_rejects(page)
    page.close()


def test_default_columns_fit_a_chat_and_empty_ones_wait_under_columns(browser, host):
    page, fr = _run(browser, host, "search_candidates", {"query": "", "limit": 20})
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    heads = [h.strip() for h in fr.locator("table.ptable thead th:visible").all_inner_texts()]
    assert heads[1:] == ["Name", "Title", "Skills", "Email"]  # no Job, Stage, LinkedIn or Match: nobody has any
    assert fr.locator(".tablewrap").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    fr.get_by_role("button", name="Columns").click()
    assert fr.get_by_role("checkbox", name=re.compile("Job")).count() == 1  # still one tick away
    page.close()


def test_compare_is_disabled_outside_two_to_six(browser, host):
    page, fr = _run(browser, host, "search_candidates", {"query": "", "limit": 20})
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    boxes = fr.locator("tbody input.rowpick")
    boxes.nth(0).check()
    assert fr.get_by_role("button", name="Compare 1").is_disabled()
    for i in range(1, 7):
        boxes.nth(i).check()
    btn = fr.get_by_role("button", name="Compare 7")
    assert btn.is_disabled() and fr.get_by_text("Compare takes 2 to 6").count() >= 1
    page.close()


def test_refresh_keeps_table_mode_ticks_sort_and_picks(browser, host):
    page, fr = _run(browser, host, "search_candidates", {"query": "", "limit": 12, "highlight": ["python"]})
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    fr.get_by_role("button", name="Sort by Name").click()
    fr.locator("tbody input.rowpick").nth(2).check()
    fr.get_by_role("button", name="Refresh").click()
    page.wait_for_timeout(1200)
    assert fr.locator("table.ptable").count() == 1  # still the table
    assert fr.locator("tbody input.rowpick:checked").count() == 1
    assert fr.locator('th[aria-sort="ascending"]').count() == 1
    assert fr.locator('.hlbar button[data-skill="python"]').count() == 1
    _no_rejects(page)
    page.close()


def test_find_duplicates_shows_one_card_per_group(browser, host):
    page, fr = _run(browser, host, "find_duplicates", {})
    fr.locator("article.pair").first.wait_for()
    assert fr.get_by_role("heading", name="Dana Dup, 3 profiles").count() == 1
    assert fr.locator("article.pair").count() == 1  # one card, not three pairs
    assert fr.locator("article.pair .side").count() == 3
    assert "1 group" in fr.locator(".vhead").inner_text()
    _no_rejects(page)
    page.close()
