"""c11 W: the views in a real browser inside the stand-in host. Skipped without Playwright."""
import json
import re

import pytest

pw = pytest.importorskip("playwright.sync_api")
from test_c11_v_e2e import _free_port, browser, host, _run  # noqa: E402,F401  (fixtures and the runner)


def sent(page, method):
    return page.evaluate("m => window.HOSTLOG.filter(x => x.method === m)", method)


def test_select_none_clears_selection_hidden_by_the_filter(host, browser):
    page, fr = _run(browser, host)
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    total = fr.locator("tbody tr").count()
    fr.get_by_role("button", name="Select all shown").click()
    fr.get_by_label("Filter these people").fill("Shoplane")
    shown = fr.locator("tbody tr").count()
    assert shown < total
    status = fr.locator(".sel").first.inner_text()
    assert re.search(r"%d selected, %d hidden by the filter" % (total, total - shown), status), status
    assert "Open %d in Talent Search" % total in fr.locator(".tbar").nth(2).inner_text() or fr.get_by_role("button", name=re.compile("Open %d in" % total)).count() == 1
    fr.get_by_role("button", name="Select none").click()
    assert fr.locator(".sel").first.inner_text().strip() == ""
    fr.get_by_label("Filter these people").fill("")
    assert fr.locator("input.rowpick:checked").count() == 0
    page.close()


def test_empty_ask_box_does_not_send(host, browser):
    page, fr = _run(browser, host)
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    fr.locator("input.rowpick").first.click()
    fr.get_by_role("button", name=re.compile(r"^Ask Claude about these")).click()
    before = len(sent(page, "ui/message"))
    fr.get_by_role("button", name="Send to Claude").click()
    assert "Type what you want Claude to do first." in fr.locator("body").inner_text()
    assert len(sent(page, "ui/message")) == before
    assert fr.locator("input[aria-label='What should Claude do with them?']").evaluate("e => e === document.activeElement")
    page.close()


def test_pipeline_move_confirms_before_sending(host, browser):
    page, fr = _run(browser, host, args={"job_id": 1}, tool="job_pipeline")
    fr.get_by_role("combobox").first.wait_for()
    sel = fr.get_by_role("combobox").first
    sel.select_option(index=1)
    page.wait_for_timeout(300)
    assert len(sent(page, "ui/message")) == 0  # picking only sets the value
    fr.get_by_role("button", name=re.compile(r"^Move ")).first.click()
    assert "Claude makes the move when you send this." in fr.locator("body").inner_text()
    assert len(sent(page, "ui/message")) == 0
    fr.get_by_role("button", name="Cancel").first.click()
    assert len(sent(page, "ui/message")) == 0
    fr.get_by_role("button", name=re.compile(r"^Move ")).first.click()
    fr.get_by_role("button", name="Send").click()
    page.wait_for_timeout(300)
    assert len(sent(page, "ui/message")) == 1
    page.close()


def test_back_to_table_keeps_sort_filter_and_selection(host, browser):
    page, fr = _run(browser, host)
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    fr.get_by_role("button", name="Sort by Name").click()
    fr.get_by_role("button", name=re.compile("Sort by Name")).click()  # descending
    fr.get_by_label("Filter these people").fill("Example")
    fr.locator("input.rowpick").nth(1).click()
    order = fr.locator("tbody th[scope=row]").all_inner_texts()
    fr.locator("tbody th[scope=row] button").first.click()
    fr.get_by_role("button", name="Back to table").wait_for()
    fr.get_by_role("button", name="Back to table").click()
    fr.locator("table.ptable").wait_for()
    assert fr.locator("tbody th[scope=row]").all_inner_texts() == order
    assert fr.get_by_label("Filter these people").input_value() == "Example"
    assert fr.locator("input.rowpick:checked").count() == 1
    page.close()


def test_one_load_asks_for_each_card_once(host, browser):
    page, fr = _run(browser, host)
    fr.get_by_role("region", name="Candidates").wait_for()
    page.wait_for_timeout(600)
    calls = [m["params"]["arguments"]["candidate_ids"] for m in sent(page, "tools/call") if m["params"]["name"] == "candidate_cards"]
    ids = [i for c in calls for i in c]
    assert len(ids) == len(set(ids)), calls
    page.close()


def test_a_cancelled_request_keeps_the_result(host, browser):
    page, fr = _run(browser, host)
    fr.get_by_role("region", name="Candidates").wait_for()
    page.evaluate("window.postMessage && document.querySelector('iframe').contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/tool-cancelled',params:{}},'*')")
    page.wait_for_timeout(300)
    assert fr.get_by_role("region", name="Candidates").count() == 1
    assert "What you see is from before." in fr.locator("#notice").inner_text()
    page.close()


def test_blocked_copy_says_why_and_selects_the_text(host, browser):
    page, fr = _run(browser, host)
    fr.get_by_role("button", name="Show as table").click()
    fr.locator("table.ptable").wait_for()
    fr.get_by_role("button", name="Select all shown").click()
    fr.get_by_role("button", name=re.compile(r"^Copy \d+ emails?")).click()
    box = fr.locator(".copynote")
    box.wait_for()
    assert "The chat app blocked copying." in box.inner_text()
    area = box.locator("textarea, input")
    assert area.evaluate("e => e === document.activeElement && e.selectionStart === 0 && e.selectionEnd === e.value.length")
    page.close()


def test_a_host_that_cannot_message_hides_the_controls(host, browser):
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(f"http://127.0.0.1:{host}")
    page.wait_for_function("document.querySelector('#tool').options.length > 3")
    page.check("#nomsg")
    page.select_option("#tool", "job_pipeline")
    page.fill("#args", json.dumps({"job_id": 1}))
    page.click("#run")
    fr = page.frame_locator("iframe").last
    fr.get_by_role("link", name=re.compile("Open in Talent Search")).or_(fr.get_by_role("button", name=re.compile("Open in Talent Search"))).first.wait_for()
    assert fr.get_by_role("button", name=re.compile(r"^Move ")).count() == 0
    page.close()
