"""Schema v2: migration, stages, timeline, tags, work history and Today's items."""
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from talent import actions
from talent.sample_data import load_sample_data
from talent.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def test_migrates_a_v1_database(tmp_path):
    path = tmp_path / "old.db"
    shutil.copy(FIXTURES / "v1.db", path)
    s = Store(path)
    assert s.schema_version >= 2
    old = s.search_candidates("payments").hits[0].candidate
    assert old["name"] == "Old" and old["tags"] == ["Go", "rust"]  # deduped, case-insensitive
    job = s.list_jobs()[0]
    assert [st["name"] for st in s.job_stages(job["id"])][:3] == ["Sourced", "Contacted", "Screening"]
    assert s.pipeline(job["id"])[0]["stage"] == "Screening"
    assert any(t["kind"] == "stage" for t in s.timeline(old["id"]))  # old activity copied over
    assert s._row("PRAGMA integrity_check")["integrity_check"] == "ok"
    s.close()
    assert Store(path).search_candidates("payments").total == 1  # reopening is a no-op


def test_tags_are_structured_and_filterable(store):
    a = store.add_candidate(name="A", tags="Python, sql, python")
    store.add_candidate(name="B", tags=["Go"])
    assert a["tags"] == ["Python", "sql"]
    assert [h.candidate["name"] for h in store.search_candidates("", tag="PYTHON").hits] == ["A"]
    assert {t["tag"].lower() for t in store.list_tags()} == {"python", "sql", "go"}
    merged, created = store.upsert_candidate(name="A", email=None, tags="rust")
    assert created  # no email or LinkedIn, so no merge by name alone


def test_work_history_is_saved_and_searched(store):
    c = store.add_candidate(name="W", work_history=[{"title": "SRE", "company": "Cloudflare", "start": "2019", "end": "2023"}])
    assert store.work_history(c["id"])[0]["company"] == "Cloudflare"
    assert store.search_candidates("cloudflare").total == 1
    store.update_candidate(c["id"], work_history=[])
    assert store.search_candidates("cloudflare").total == 0


def test_last_contacted_is_separate_from_updated(store):
    c = store.add_candidate(name="T")
    assert c["last_contacted_at"] is None
    store.log(c["id"], "note", "Internal note")
    assert store.get_candidate(c["id"])["last_contacted_at"] is None  # a note isn't contact
    store.log(c["id"], "call", "Intro", at="2026-03-01T09:00:00+00:00")
    assert store.get_candidate(c["id"])["last_contacted_at"].startswith("2026-03-01")
    store.log(c["id"], "email", "Older one", at="2025-01-01T09:00:00+00:00")
    assert store.get_candidate(c["id"])["last_contacted_at"].startswith("2026-03-01")  # never goes backwards
    future = datetime.now(timezone.utc) + timedelta(days=3)
    store.log(c["id"], "interview", "Onsite", at=future)
    assert store.get_candidate(c["id"])["last_contacted_at"].startswith("2026-03-01")  # future doesn't count
    with pytest.raises(ValueError):
        store.log(c["id"], "telepathy", "x")


def test_custom_stages(store):
    job = store.add_job("Designer", stages=["Applied", "Portfolio review", "Offer", "Hired"])
    assert [s["name"] for s in store.job_stages(job["id"])] == ["Applied", "Portfolio review", "Offer", "Hired"]
    c = store.add_candidate(name="D")
    assert store.add_to_pipeline(c["id"], job["id"])["stage"] == "Applied"
    store.add_stage(job["id"], "Take-home", after="Portfolio review")
    store.rename_stage(job["id"], "Applied", "New")
    assert store.pipeline(job["id"])[0]["stage"] == "New"  # people move with the renamed stage
    store.move_stage(c["id"], job["id"], "take-home")  # case-insensitive
    assert store.pipeline(job["id"])[0]["stage"] == "Take-home"
    with pytest.raises(ValueError, match="Portfolio review"):
        store.move_stage(c["id"], job["id"], "Nope")
    with pytest.raises(ValueError):
        store.remove_empty_stage(job["id"], "Take-home")  # someone is in it
    store.remove_empty_stage(job["id"], "Portfolio review")
    store.reorder_stages(job["id"], ["Hired", "Offer", "Take-home", "New"])
    assert store.job_stages(job["id"])[0]["name"] == "Hired"


def test_action_items_and_toggles(store):
    load_sample_data(store)
    items = actions.action_items(store)
    kinds = {i["kind"] for i in items["items"]}
    assert kinds == {"interview", "suggestion", "stuck", "thin", "stale"}
    assert items["items"][0]["kind"] == "interview"  # most urgent first
    store.set_settings(flag_stale="0", flag_thin="0")
    kinds = {i["kind"] for i in actions.action_items(store)["items"]}
    assert "stale" not in kinds and "thin" not in kinds
    stuck = next(i for i in actions.action_items(store)["items"] if i["kind"] == "stuck")
    store.snooze(stuck["key"], 7)
    after = actions.action_items(store)
    assert stuck["key"] not in {i["key"] for i in after["items"]} and after["hidden"] == 1
    store.set_settings(stuck_days="30")
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "stuck"]


def test_sample_data_is_idempotent(store):
    assert load_sample_data(store) == 16
    assert load_sample_data(store) == 0
    assert len(store.list_jobs()) == 2
