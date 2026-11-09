"""The People view in a real browser, inside the stand-in host (scripts/view_host.py). Skipped without Playwright."""
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

pw = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    port = _free_port()
    data = tmp_path_factory.mktemp("viewdata")
    env = {**os.environ, "TALENT_DATA_DIR": str(data)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "view_host.py"), "--port", str(port),
                             "--data", str(data), "--sample"], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
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


def _run(browser, port, args=None, nolinks=False, tool="search_candidates"):
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(f"http://127.0.0.1:{port}")
    page.wait_for_function("document.querySelector('#tool').options.length > 3")
    if nolinks:
        page.check("#nolinks")
    page.select_option("#tool", tool)
    page.fill("#args", json.dumps(args if args is not None else {"query": "", "limit": 10}))
    page.click("#run")
    return page, page.frame_locator("iframe").last


def test_a_search_renders_a_card_and_next_moves_on(host, browser):
    page, fr = _run(browser, host)
    fr.get_by_role("region", name="Candidates").wait_for()
    assert "1 of 10" in fr.locator(".cnav .count").inner_text()
    fr.get_by_role("button", name="Next candidate").click()
    assert "2 of 10" in fr.locator(".cnav .count").inner_text()
    assert fr.get_by_role("group").first.get_attribute("aria-label").startswith("2 of 10: ")
    fr.get_by_role("region", name="Candidates").press("ArrowRight")
    page.wait_for_timeout(200)
    assert "3 of 10" in fr.locator(".cnav .count").inner_text()
    page.close()


def test_table_sorts_selects_and_tells_claude(host, browser):
    page, fr = _run(browser, host)
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    fr.get_by_role("button", name="Sort by Name").click()
    names = fr.locator("tbody th[scope=row]").all_inner_texts()
    assert names == sorted(names, key=str.lower)
    assert fr.locator("th[aria-sort=ascending]").count() == 1
    fr.get_by_role("checkbox", name="Select %s" % names[0]).click()
    fr.get_by_role("checkbox", name="Select %s" % names[1]).click()
    page.wait_for_timeout(300)
    sent = page.evaluate("window.HOSTLOG.filter(m => m.method === 'ui/update-model-context').length")
    assert sent >= 2
    assert "2 selected" in fr.locator(".sel").first.inner_text()
    page.close()


def test_open_link_falls_back_to_showing_the_address(host, browser):
    page, fr = _run(browser, host, nolinks=True)
    fr.get_by_role("button", name=re.compile(r"^Open .+ in Talent Search$")).first.click()
    fr.locator("#linknote .url").wait_for()
    assert "/candidates/" in fr.locator("#linknote .url").inner_text()
    assert fr.get_by_role("button", name="Copy link").count() == 1
    page.close()
