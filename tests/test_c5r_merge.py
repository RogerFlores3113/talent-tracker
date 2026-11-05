"""Cycle 5 re-review merge: one reviewed state. Yes on Today's "Right?" (confirm_ai_action) also releases a
held-back AI hire from the client update text."""
from talent import reports

from test_c5r_h import _acme, _ai_move


def test_confirming_an_ai_hire_on_today_releases_it_into_the_client_update(store):
    be, ana, maya = _acme(store)
    aid = _ai_move(store, maya, be["id"], "Placed")
    assert [m["name"] for m in reports.report(store, "acme")["held"]] == ["Maya Chen"]
    store.confirm_ai_action(aid)
    rep = reports.report(store, "acme")
    assert rep["held"] == [] and "Maya Chen to Placed" in reports.report_text(rep)
    assert not reports.unreviewed_in(store, "acme", None)
