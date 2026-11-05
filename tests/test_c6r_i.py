"""Cycle 6 re-review, fixer I: import-csv keeps the file's stages, the time zone banner, clearly fake sample
names, and prompts that say when no zone is set."""
from __future__ import annotations

import re
from pathlib import Path

from conftest import flash_of, local_client

from talent import cli, importers, paths, recipes, reports
from talent.sample_data import SAMPLES, load_sample_data, sample_ids
from talent.store import Store

ATS = ("Name,Email,Job,Client,Status\n"
       "Ana Ruiz,ana@x.com,Engineer,Acme,Submitted to Client\n"
       "Ben Cole,ben@x.com,Engineer,Acme,Submitted to Client\n"
       "Cy Dunn,cy@x.com,Engineer,Acme,HM Interview\n")


def _csv(tmp_path: Path, text: str = ATS) -> Path:
    f = tmp_path / "ats.csv"
    f.write_text(text, encoding="utf-8")
    return f


def _stages(s: Store) -> tuple[list[str], dict[str, str]]:
    job = next(j for j in s.list_jobs() if j["title"] == "Engineer")
    return [st["name"] for st in s.job_stages(job["id"])], {p["candidate_name"]: p["stage"] for p in s.pipeline(job["id"])}


# ------------------------------------------------------------- 1. import-csv keeps the stages
def test_import_csv_keeps_the_files_stages_like_sync(tmp_path, capsys):
    cli.main(["import-csv", str(_csv(tmp_path))])
    out = capsys.readouterr().out
    assert "Added 2 new stages: " in out and "Submitted to Client on Engineer (Acme)" in out
    s = Store(paths.db_path())
    try:
        names, where = _stages(s)
        assert "Submitted to Client" in names and "HM Interview" in names
        assert where == {"Ana Ruiz": "Submitted to Client", "Ben Cole": "Submitted to Client", "Cy Dunn": "HM Interview"}
        assert importers.sync_profile(s, ["Name", "Email", "Job", "Client", "Status"])["add_stages"] is True
    finally:
        s.close()


def test_import_csv_dry_run_says_which_stages_it_adds(tmp_path, capsys):
    cli.main(["import-csv", str(_csv(tmp_path)), "--dry-run"])
    out = capsys.readouterr().out
    assert "2 new stages: " in out and "Nothing was changed (dry run)." in out


def test_import_csv_fold_stages_prints_each_fold(tmp_path, capsys):
    cli.main(["import-csv", str(_csv(tmp_path)), "--fold-stages"])
    out = capsys.readouterr().out
    assert "Put 3 people in a different stage, since their job has no stage by that name" in out
    assert re.search(r"  Submitted to Client to \w+ on Engineer \(Acme\): 2", out)
    assert re.search(r"  HM Interview to \w+ on Engineer \(Acme\): 1", out)
    assert "Put in a different stage (" not in out  # printed once, with counts, not again as a panel
    assert "—" not in out
    s = Store(paths.db_path())
    try:
        names, _ = _stages(s)
        assert "Submitted to Client" not in names
    finally:
        s.close()


def test_stage_fold_lines_says_nothing_when_nothing_folded():
    assert importers.stage_fold_lines({"stage_moves": {}, "missing_stages": [], "stages_added": True}) == []
    lines = importers.stage_fold_lines({"stage_moves": {"Onsite to Interviewing on X": 4}, "dry_run": True})
    assert lines[0].startswith("Will put 4 people in a different stage") and lines[1] == "  Onsite to Interviewing on X: 4"


def test_web_import_without_a_preview_still_offers_to_keep_stages(store, monkeypatch):
    from talent import web

    monkeypatch.setattr(web, "PREVIEW_MAX_ROWS", 0)
    c = local_client(store)
    r = c.post("/import", files={"file": ("ats.csv", ATS.encode())})
    assert re.search(r'name="add_stages" value="1"\s+checked', r.text)
    assert "Keep the file's own stages" in r.text


# ------------------------------------------------------------- 2. time zone banner
def test_today_shows_the_time_zone_banner_until_a_zone_is_saved(store):
    c = local_client(store)
    home = c.get("/").text
    assert "Welcome to Talent Search" in home and "Set your time zone" in home  # the welcome page has it too
    assert "data-tz-prefill" in home and 'name="next" value="/"' in home and 'action="/settings/time-zone"' in home
    assert not re.search(r"<script(?![^>]*\bsrc=)", home) and " onclick=" not in home
    store.add_candidate(name="Ana Ruiz")
    assert "Set your time zone" in c.get("/").text
    r = c.post("/settings/time-zone", data={"time_zone": "America/Chicago", "next": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/" and flash_of(r) == "Times now show in America/Chicago."
    assert "Set your time zone" not in c.get("/").text
    r = c.post("/settings/time-zone", data={"time_zone": "Not/AZone", "next": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/" and flash_of(r, "flash_error")
    r = c.post("/settings/time-zone", data={"time_zone": "America/Denver"}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings")  # from Settings: back to Settings
    reports.save_user_zone(store, "")
    assert "Set your time zone" in c.get("/").text


def test_banner_is_not_on_a_search(store):
    store.add_candidate(name="Ana Ruiz", about="rust")
    assert "Set your time zone" not in local_client(store).get("/?q=rust").text


def test_app_js_prefills_the_browsers_zone():
    js = (Path(__file__).parents[1] / "src" / "talent" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data-tz-prefill" in js and "resolvedOptions().timeZone" in js


# ------------------------------------------------------------- 3. sample names
def test_sample_people_have_clearly_made_up_names(store):
    assert all(name.endswith(" Example") for name, *_ in SAMPLES)
    assert len({name for name, *_ in SAMPLES}) == len(SAMPLES)
    load_sample_data(store)
    people, _ = sample_ids(store)
    assert {store.get_candidate(i)["name"] for i in people} == {name for name, *_ in SAMPLES}
    # a real person who happens to share a first name is never matched to a sample one
    r = importers.import_rows(store, ["Name", "Company"], [["Maya Okafor", "Ledgerline"]], ["name", "current_company"],
                              backup=False)
    assert r["added"] == 1 and not r["possible_duplicates"]


# ------------------------------------------------------------- 4. prompts say when no zone is set
def test_prompts_say_when_no_zone_is_set(store):
    reports.save_user_zone(store, "")
    try:
        assert "haven't set my time zone in Talent Search" in recipes.plan_my_day()
        reports.save_user_zone(store, "America/Chicago")
        assert "haven't set my time zone" not in recipes.plan_my_day()
    finally:
        reports.save_user_zone(store, "")


def test_import_date_lookup_uses_the_person_index(store):
    """The dated-stage lookup reads one person's entries, not every stage entry (4,700 rows took 8 s)."""
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT max(id) FROM timeline WHERE candidate_id = ? AND job_id = ? AND +kind = 'stage'",
        (1, 1)).fetchall()
    assert any("timeline_candidate_idx" in str(tuple(row)) for row in plan)
