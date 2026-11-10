"""Cycle 13 A: skill highlights in the in-chat People table, in a real browser. Skipped without Playwright."""
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


def _table(browser, host):
    page, fr = _run(browser, host, args={"query": "", "limit": 30})
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    return page, fr


def _first_skills(fr, n=2):
    """Two skills that at least one person has, and are different."""
    got = fr.locator("tbody button.skillbtn").evaluate_all("els => els.map(e => e.dataset.skill)")
    out = []
    for g in got:
        if g not in out:
            out.append(g)
    return out[:n]


def test_skill_chip_lights_up_everywhere_and_sorts(host, browser):
    page, fr = _table(browser, host)
    a, b = _first_skills(fr, 2)
    fr.locator(f'tbody button.skillbtn[data-skill="{a}"]').first.click()
    assert fr.locator('.hlbar button.removable[data-hl="red"]').count() == 1
    lit = fr.locator(f'tbody button.skillbtn[data-skill="{a}"]')
    assert lit.count() >= 1
    assert all(x == "red" for x in lit.evaluate_all("els => els.map(e => e.dataset.hl)"))
    fr.locator(f'tbody button.skillbtn[data-skill="{b}"]').first.click()
    assert fr.locator(f'.hlbar button[data-skill="{b}"]').get_attribute("data-hl") == "yellow"
    rows = fr.locator("tbody tr").evaluate_all(
        "trs => trs.map(t => [...t.querySelectorAll('button.skillbtn')].map(b => b.dataset.skill))")
    key = lambda sk: (-((a in sk) + (b in sk)), a not in sk, b not in sk)
    keys = [key(r) for r in rows]
    assert keys == sorted(keys)
    # removing the first pick leaves the second one yellow, and nobody is hidden
    n = fr.locator("tbody tr").count()
    fr.locator(f'.hlbar button[data-skill="{a}"]').click()
    assert fr.locator(f'.hlbar button[data-skill="{b}"]').get_attribute("data-hl") == "yellow"
    assert fr.locator("tbody tr").count() == n
    fr.get_by_role("button", name="Clear", exact=True).click()
    assert fr.locator(".hlbar").is_hidden() and fr.locator("tbody button.skillbtn.hl").count() == 0
    page.close()
