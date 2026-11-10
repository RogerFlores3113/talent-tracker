"""Cycle 14 D2 in a real browser (the view host). Skipped without Playwright."""
import json
import re
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# ---------------------------------------------------------------- views (real browser)
pw = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    if os.environ.get("D2_VIEW_HOST_PORT"):  # a view host already running (when Playwright lives in another Python)
        yield int(os.environ["D2_VIEW_HOST_PORT"])
        return
    port = _free_port()
    dd = tmp_path_factory.mktemp("d2data")
    env = {**os.environ, "TALENT_DATA_DIR": str(dd)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "view_host.py"), "--port", str(port),
                             "--data", str(dd), "--sample"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


def test_e2e_opens_tiered_table_with_names_and_no_fullscreen(browser, host):
    page, fr = _run(browser, host, "search_candidates", {"query": "", "limit": 20, "highlight": ["python", "title:Data Engineer"]})
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    assert page.locator("#chat.full").count() == 0  # Show as table does not force full screen
    assert fr.locator('.hlbar button[aria-label="Remove highlight: python (red, pick 1)"]').count() == 1
    assert fr.locator('.hlbar button[aria-label="Remove highlight: title: Data Engineer (yellow, pick 2)"]').count() == 1
    chip = fr.locator('tbody button.skillbtn[data-skill="python"]').first
    assert chip.get_attribute("aria-label") == "python, highlighted red, pick 1 (remove)"
    heads = [h.strip() for h in fr.locator("table.ptable thead th:visible").all_inner_texts()]
    assert heads[1:5] == ["Name", "Title", "Skills", "Job"]
    assert "Company" not in heads and fr.get_by_role("button", name="Columns").count() == 1
    assert fr.locator("tbody tr").first.locator(".tophint").inner_text().startswith("Top match: ")
    # Columns turns Company on and it is remembered across a redraw
    fr.get_by_role("button", name="Columns").click()
    fr.get_by_role("checkbox", name="Company").check()
    assert "Company" in [h.strip() for h in fr.locator("table.ptable thead th:visible").all_inner_texts()]
    # a pick made in the view is kept when the view calls the tool again
    fr.locator('.hlbar button[data-skill="python"]').click()
    fr.get_by_role("button", name="Refresh").click()
    page.wait_for_timeout(800)
    assert fr.locator('.hlbar button[data-skill="title:data engineer"]').count() == 1
    assert fr.locator('.hlbar button[data-skill="python"]').count() == 0
    page.close()


def test_e2e_compare_from_table_follows_screen_order_and_sticky_tint(browser, host):
    page, fr = _run(browser, host, "search_candidates", {"query": "", "limit": 12})
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    fr.get_by_role("button", name="Sort by Name").click()
    fr.get_by_role("button", name="Sort by Name").click()  # descending
    screen = fr.locator("tbody th[scope=row]").all_inner_texts()
    boxes = fr.locator("tbody input.rowpick")
    for i in (3, 1, 2):  # ticked in a different order than the screen shows
        boxes.nth(i).check()
    assert fr.locator("tbody tr.sel th[scope=row]").first.evaluate("e => getComputedStyle(e).backgroundColor") == \
        fr.locator("tbody tr.sel td.pickcell").first.evaluate("e => getComputedStyle(e).backgroundColor")
    fr.get_by_role("button", name="Compare 3").click()
    fr.locator("table.ctable").wait_for()
    heads = [h.strip() for h in fr.locator("table.ctable thead th.cmp-person .cmp-nm").all_inner_texts()]
    assert heads == [screen[1], screen[2], screen[3]]
    assert fr.get_by_role("button", name="Hiring manager view").count() == 1
    # three people fit at 720px without sideways scroll
    fits = fr.locator(".cmp-wrap").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    assert fits
    page.close()


def test_e2e_export_status_does_not_move_the_table(browser, host):
    page, fr = _run(browser, host, "search_candidates", {"query": "", "limit": 6})
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    top = fr.locator("table.ptable").evaluate("e => e.getBoundingClientRect().top")
    assert fr.get_by_role("button", name="Export for me").count() == 1
    fr.get_by_role("button", name=re.compile(r"Export .*for hiring manager")).click()
    page.wait_for_timeout(1500)
    assert fr.locator(".xlstatus code.path").count() == 1  # the full path is shown
    assert abs(fr.locator("table.ptable").evaluate("e => e.getBoundingClientRect().top") - top) < 3
    page.close()
