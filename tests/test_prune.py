import sqlite3
from unittest import mock

from job_tracker import __main__ as app
from job_tracker.config import DEFAULT_NOTION_PROPERTIES, parse_config
from job_tracker.models import Posting
from job_tracker.notion import Notion
from job_tracker.store import SeenStore

BASE = {"sources": [{"type": "google_sheet"}], "filters": {"title_keywords": ["x"]}}


def page(pid, role, url, status=None, interest="Not Interested"):
    sel = lambda v: {"select": {"name": v} if v else None}  # noqa: E731
    return {"id": pid, "properties": {
        "Role": {"title": [{"plain_text": role}]},
        "Studio": {"rich_text": [{"plain_text": "Acme"}]},
        "Posting URL": {"url": url},
        "Source": sel("Community Sheet"),
        "Status": sel(status),
        "Interest": sel(interest),
    }}


def test_pages_with_select_filters_and_paginates():
    n = Notion("t", "db", dict(DEFAULT_NOTION_PROPERTIES))
    responses = [
        {"results": [page("p1", "Rigger", "https://a.com/1")],
         "has_more": True, "next_cursor": "c2"},
        {"results": [page("p2", "Lighter", "https://a.com/2", status="Applied")],
         "has_more": False},
    ]
    with mock.patch.object(n, "_request", side_effect=responses) as req:
        found = n.pages_with_select("interest", "Not Interested")
    first = req.call_args_list[0].kwargs["json"]
    assert first["filter"] == {"property": "Interest",
                               "select": {"equals": "Not Interested"}}
    assert req.call_args_list[1].kwargs["json"]["start_cursor"] == "c2"
    assert [(f["id"], f["role"], f["status"]) for f in found] == [
        ("p1", "Rigger", ""), ("p2", "Lighter", "Applied")]


def test_status_type_property_is_read_too():
    n = Notion("t", "db", dict(DEFAULT_NOTION_PROPERTIES))
    pg = page("p1", "Rigger", "https://a.com/1")
    pg["properties"]["Status"] = {"status": {"name": "Interviewing"}}
    with mock.patch.object(n, "_request",
                           return_value={"results": [pg], "has_more": False}):
        assert n.pages_with_select("interest", "Not Interested")[0]["status"] \
            == "Interviewing"


def test_prune_records_then_trashes_and_keeps_applied(tmp_path):
    cfg = parse_config({**BASE, "state": {"db_path": str(tmp_path / "seen.db")}})
    notion = mock.Mock()
    notion.pages_with_select.return_value = [
        {"id": "p1", "role": "Rigger", "url": "https://a.com/1", "studio": "Acme",
         "source": "Community Sheet", "status": ""},
        {"id": "p2", "role": "Lighter", "url": "https://a.com/2", "studio": "Acme",
         "source": "Community Sheet", "status": "Applied"},
    ]
    assert app.prune(cfg, notion, dry_run=False) == 0
    notion.trash.assert_called_once_with("p1")
    rows = sqlite3.connect(cfg.db_path).execute(
        "SELECT title, status, notion_id FROM seen").fetchall()
    assert rows == [("Rigger", "dismissed", "p1")]
    # The sync now treats it as seen, so it is never re-added.
    assert SeenStore(cfg.db_path).has(
        Posting(role="rigger", studio="", source="", url="http://a.com/1/"))


def test_prune_dry_run_writes_nothing(tmp_path):
    cfg = parse_config({**BASE, "state": {"db_path": str(tmp_path / "seen.db")}})
    notion = mock.Mock()
    notion.pages_with_select.return_value = [
        {"id": "p1", "role": "Rigger", "url": "u", "studio": "A", "source": "",
         "status": ""}]
    assert app.prune(cfg, notion, dry_run=True) == 0
    notion.trash.assert_not_called()
    assert not (tmp_path / "seen.db").exists()


def test_not_interested_value_is_configurable():
    cfg = parse_config({**BASE, "notion": {"not_interested": "Pass"}})
    assert cfg.not_interested == "Pass"
