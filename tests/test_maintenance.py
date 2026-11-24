import argparse
import sqlite3
from unittest import mock

import pytest

from job_tracker import __main__ as app
from job_tracker.config import ScoringConfig, parse_config
from job_tracker.models import Fatal, Posting
from job_tracker.scoring import Score, describe, fingerprint
from job_tracker.sources.job_boards import JobBoardsSource, html_to_text
from job_tracker.store import SeenStore

BASE = {"sources": [{"type": "google_sheet"}], "filters": {"title_keywords": ["x"]}}


def posting(role, url, source="Community Sheet", studio="Acme"):
    return Posting(role=role, studio=studio, source=source, url=url, country="Canada")


def page(pid, role, url, source="Community Sheet", interest="", status=""):
    return {"id": pid, "role": role, "studio": "Acme", "source": source,
            "interest": interest, "status": status, "experience": "", "work_mode": ""}


# -- closing rows whose posting has gone -----------------------------------

class Src:
    def __init__(self, name, complete=True):
        self.name, self.complete = name, complete


def run_close(existing, listed, sources, tmp_path):
    cfg = parse_config({**BASE, "notion": {"prune_closed": True}})
    notion, report = mock.Mock(), app.Report()
    store = SeenStore(str(tmp_path / "seen.db"))
    app.close_stale(cfg, notion, store, existing, listed, sources, report)
    return notion, report, store


def test_close_stale_trashes_only_untouched_delisted_rows(tmp_path):
    live = posting("Rigger", "https://a/1")
    existing = {
        live.key: page("p1", "Rigger", "https://a/1"),
        ("a/2", "lighter"): page("p2", "Lighter", "https://a/2"),            # gone
        ("a/3", "td"): page("p3", "TD", "https://a/3", interest="Interested"),
        ("a/4", "comp"): page("p4", "Comp", "https://a/4", status="Applied"),
        ("a/5", "manual"): page("p5", "Manual", "https://a/5", source="LinkedIn"),
    } | {(f"z/{i}", f"r{i}"): page(f"k{i}", f"Live {i}", f"https://z/{i}") for i in range(8)}
    listed = [live] + [posting(f"Live {i}", f"https://z/{i}") for i in range(8)]
    notion, report, store = run_close(existing, listed, [Src("Community Sheet")], tmp_path)
    notion.trash.assert_called_once_with("p2")
    assert report.closed == 1 and ("a/2", "lighter") not in existing
    status = sqlite3.connect(store.db.execute("PRAGMA database_list").fetchone()[2]) \
        .execute("SELECT status FROM seen").fetchone()[0]
    assert status == "closed"


def test_close_stale_skips_a_source_that_failed(tmp_path):
    existing = {("a/2", "lighter"): page("p2", "Lighter", "https://a/2", source="Studio Boards")}
    notion, report, _ = run_close(existing, [], [Src("Studio Boards", complete=False)], tmp_path)
    notion.trash.assert_not_called()


def test_close_stale_refuses_when_too_many_look_closed(tmp_path):
    existing = {(f"a/{i}", f"r{i}"): page(f"p{i}", f"R{i}", f"https://a/{i}") for i in range(10)}
    notion, report, _ = run_close(existing, [], [Src("Community Sheet")], tmp_path)
    notion.trash.assert_not_called()
    assert "too many to trust" in report.warnings[0]


def test_twin_listing_keeps_row_open(tmp_path):
    existing = {("linkedin/1", "lighting artist"): page("p1", "Lighting Artist", "https://linkedin/1")}
    listed = [posting("Freelance Senior Lighting Artist", "https://board/1", source="Studio Boards")]
    notion, *_ = run_close(existing, listed, [Src("Community Sheet"), Src("Studio Boards")], tmp_path)
    notion.trash.assert_not_called()


# -- rescoring ---------------------------------------------------------------

def test_store_rescore_updates_rejected_rows_only(tmp_path):
    store = SeenStore(str(tmp_path / "seen.db"))
    p = posting("Rigger", "https://a/1")
    store.add(p, status="rejected", score=Score(4, "Rigging", "too far"))
    assert store.has(p) and not store.has(p, skip_rejected=True)
    store.add(p, "page-1", status="added", score=Score(7, "Rigging", "now fits"))
    assert store.db.execute("SELECT status, score, notion_id FROM seen").fetchone() == \
        ("added", 7, "page-1")
    store.add(p, status="rejected", score=Score(1, "Other", "x"))  # not rejected now
    assert store.db.execute("SELECT status FROM seen").fetchone() == ("added",)


def test_fingerprint_follows_scoring_settings():
    a = ScoringConfig(enabled=True, profile="Rigger")
    assert fingerprint(a) == fingerprint(ScoringConfig(enabled=True, profile="Rigger "))
    assert fingerprint(a) != fingerprint(ScoringConfig(enabled=True, profile="Lighter"))
    assert fingerprint(a) != fingerprint(ScoringConfig(enabled=True, profile="Rigger", min_score=5))


def make_run(tmp_path, monkeypatch, postings, scores, **cfg_extra):
    cfg = parse_config({**BASE, "state": {"db_path": str(tmp_path / "seen.db")},
                        "scoring": {"enabled": True, "profile": "p"}, **cfg_extra})
    scorer = mock.Mock(cost=0.02)
    scorer.score_all.side_effect = lambda new: {p.key: scores[p.role] for p in new}
    notion = mock.Mock()
    notion.existing_pages.return_value = {}
    notion.create.side_effect = lambda p, s: f"page-{p.role}"
    monkeypatch.setenv("NOTION_TOKEN", "t")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db")
    monkeypatch.setattr(app, "load_config", lambda _: cfg)
    monkeypatch.setattr(app, "collect", lambda *_: (postings, postings, []))
    monkeypatch.setattr(app, "make_scorer", lambda *_: scorer)
    monkeypatch.setattr(app, "Notion", lambda *_: notion)
    args = argparse.Namespace(config="x", dry_run=False, no_score=False, limit=0,
                              prune=False, rescore_rejected=False)
    return cfg, scorer, notion, args


def test_rejected_are_rescored_when_profile_changes(tmp_path, monkeypatch):
    p = posting("Rigger", "https://a/1")
    cfg, scorer, notion, args = make_run(tmp_path, monkeypatch, [p],
                                         {"Rigger": Score(4, "Rigging", "meh")})
    app.run(args)                                  # rejected
    app.run(args)                                  # same settings: not scored again
    assert scorer.score_all.call_args_list[-1].args[0] == []

    cfg.scoring.profile = "p, now keener on rigging"
    scorer.score_all.side_effect = lambda new: {x.key: Score(8, "Rigging", "yes") for x in new}
    app.run(args)
    assert scorer.score_all.call_args_list[-1].args[0] == [p]
    notion.create.assert_called_once()

    args.rescore_rejected = True                   # nothing rejected left
    app.run(args)
    assert scorer.score_all.call_args_list[-1].args[0] == []


# -- run log -----------------------------------------------------------------

def test_run_log_written_with_counts(tmp_path, monkeypatch):
    p = posting("Rigger", "https://a/1")
    cfg, scorer, notion, args = make_run(
        tmp_path, monkeypatch, [p], {"Rigger": Score(9, "Rigging", "great")},
        notion={"runs_database_id": "runs"})
    assert app.run(args) == 0
    db, report = notion.log_run.call_args.args
    assert db == "runs" and report.result == "OK" and len(report.added) == 1
    assert report.best() == ["[9] Acme: Rigger"] and report.cost == 0.02


def test_run_log_written_on_failure(tmp_path, monkeypatch):
    cfg, scorer, notion, args = make_run(tmp_path, monkeypatch, [], {},
                                         notion={"runs_database_id": "runs"})
    monkeypatch.setattr(app, "collect", mock.Mock(side_effect=Fatal("sheet is private")))
    with pytest.raises(Fatal):
        app.run(args)
    report = notion.log_run.call_args.args[1]
    assert report.result == "Failed" and report.error == "sheet is private"


def test_notion_log_run_payload():
    from job_tracker.config import DEFAULT_NOTION_PROPERTIES
    from job_tracker.notion import Notion
    n = Notion("t", "db", dict(DEFAULT_NOTION_PROPERTIES))
    r = app.Report()
    r.added.append((8, posting("Rigger", "u")))
    r.warn("could not read board lever:x")
    with mock.patch.object(n, "_request") as req:
        n.log_run("runs", r)
    props = req.call_args.kwargs["json"]["properties"]
    assert props["Result"] == {"select": {"name": "Warnings"}}
    assert props["Added"] == {"number": 1} and "Cost" not in props
    notes = props["Notes"]["rich_text"][0]["text"]["content"]
    assert "lever:x" in notes and "[8] Acme: Rigger" in notes


# -- descriptions and new sites --------------------------------------------

def test_html_to_text_handles_escaped_html():
    raw = "&lt;p&gt;Rig &amp; skin&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Maya&lt;/li&gt;&lt;/ul&gt;"
    assert html_to_text(raw) == "Rig & skin\nMaya"
    assert len(html_to_text("x" * 9000)) == 4000


def test_scorer_sees_description():
    p = posting("Rigger", "u")
    p.description = "Visa sponsorship available. 5+ years mGear."
    assert "<job_description>" in describe(p) and "mGear" in describe(p)
    assert "<job_description>" not in describe(posting("Rigger", "u"))


FEEDS = {
    "https://framestore.recruitee.com/api/offers/": {"offers": [
        {"title": "Rigging TD", "careers_url": "https://fs/1", "company_name": "Framestore",
         "city": "London", "country": "United Kingdom", "location": "London, United Kingdom",
         "hybrid": True, "published_at": "2026-11-11 13:43:29 UTC",
         "description": "<p>Rig creatures</p>", "requirements": "<p>Maya</p>"}]},
    "https://cinesitelondon.bamboohr.com/careers/list": {"result": [
        {"id": "46", "jobOpeningName": "Lighting TD", "location": {"city": "London",
         "state": "Greater London"}, "atsLocation": {"country": None}, "isRemote": None}]},
    "https://cinesitelondon.bamboohr.com/careers/46/detail": {"result": {"jobOpening": {
        "description": "<p>Light shots</p>"}}},
}
JOBVITE_LIST = """
<li><a href="/dneg/job/o1" class="jv-job-item flex-row-md">
  <div class="jv-job-list-name"><p class="m0">Senior Rigger</p></div>
  <div class="jv-job-list-location"><p class="m0">London, United Kingdom</p></div>
</a></li>
<li><a href="/dneg/job/o2" class="jv-job-item flex-row-md">
  <div class="jv-job-list-name"><p class="m0">Pipeline TD</p></div>
  <div class="jv-job-list-location"><p class="m0"><div class="jv-meta"> 2 Locations </div></p></div>
</a></li>"""
JOBVITE_DETAIL = """<p class="jv-job-detail-meta">
  ADG<span class='jv-inline-separator'></span> Mumbai, India
  <span class="jv-inline-separator"></span> Bengaluru, Karnataka </p>
<div class="jv-job-detail-description"><p>Build USD tools</p></div>
<div class="jv-job-detail-bottom-actions"></div>"""


@pytest.fixture
def boards():
    pages = {"https://jobs.jobvite.com/dneg/jobs": JOBVITE_LIST,
             "https://jobs.jobvite.com/dneg/job/o2": JOBVITE_DETAIL,
             "https://jobs.jobvite.com/dneg/job/o1": JOBVITE_DETAIL}
    with mock.patch.object(JobBoardsSource, "_get", lambda self, url, **_: FEEDS[url]), \
            mock.patch.object(JobBoardsSource, "_get_text", lambda self, url: pages[url]):
        yield JobBoardsSource("Studio Boards", [
            {"site": "recruitee", "slug": "framestore"},
            {"site": "bamboohr", "slug": "cinesitelondon", "studio": "Cinesite",
             "country": "United Kingdom"},
            {"site": "jobvite", "slug": "dneg", "studio": "DNEG"},
        ])


def test_recruitee_bamboohr_jobvite(boards):
    by_role = {p.role: p for p in boards.fetch()}
    fs = by_role["Rigging TD"]
    assert fs.studio == "Framestore" and fs.work_mode == "Hybrid"
    assert fs.description == "Rig creatures\nMaya" and fs.date_issued == "2026-11-11"

    cs = by_role["Lighting TD"]
    assert cs.country == "United Kingdom"            # board fallback
    assert cs.location == "London, United Kingdom" and cs.description == ""

    dn = by_role["Pipeline TD"]
    assert dn.studio == "DNEG" and dn.country == "Mumbai, India; Bengaluru, Karnataka"

    boards.fill_descriptions(list(by_role.values()))
    assert cs.description == "Light shots" and dn.description == "Build USD tools"
