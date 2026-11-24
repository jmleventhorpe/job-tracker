from datetime import date
from unittest import mock

import pytest

from job_tracker.config import FilterConfig, load_config, parse_config
from job_tracker.filters import apply_filters, location_ok, title_ok
from job_tracker.models import Fatal, Posting
from job_tracker.notion import Notion
from job_tracker.sources import build_sources
from job_tracker.sources.google_sheet import GoogleSheetSource
from job_tracker.store import SeenStore
from job_tracker.text import keyword_hit, parse_date, tidy_url


def posting(**kw) -> Posting:
    base = dict(role="Lighting Artist", studio="Studio", source="Test",
                url="https://example.com/job", country="United Kingdom")
    base.update(kw)
    return Posting(**base)


FILTERS = FilterConfig(
    title_keywords=["lighting", "rigging", "c++"],
    title_exclusions=["fx td"],
    allowed_locations=["united kingdom", "uk", "canada"],
)


# -- text helpers -----------------------------------------------------------

@pytest.mark.parametrize("text,keywords,hit", [
    ("Senior Lighting Artist", ["lighting"], "lighting"),
    ("Highlighting Specialist", ["lighting"], None),
    ("Senior C++ Developer", ["c++"], "c++"),
    ("Lighting / FX TD", ["fx td"], "fx td"),
    ("Lighting   Artist", ["lighting artist"], "lighting artist"),
])
def test_keyword_hit(text, keywords, hit):
    assert keyword_hit(text, keywords) == hit


@pytest.mark.parametrize("raw,expected", [
    ("https://a.com/jobs", "https://a.com/jobs"),
    ("www.studio.com/careers", "https://www.studio.com/careers"),
    ("studio.co.uk", "https://studio.co.uk"),
    ("jobs@studio.com", "mailto:jobs@studio.com"),
    ("Email Jane", ""),
    ("LinkedIn", ""),
    ("", ""),
])
def test_tidy_url(raw, expected):
    assert tidy_url(raw) == expected


def test_parse_date_is_day_first_by_default():
    assert parse_date("03/04/2026") == "2026-04-03"
    assert parse_date("November 2, 2026") == "2026-11-02"
    assert parse_date("03/04/2026", ["%m/%d/%Y"]) == "2026-03-04"
    assert parse_date("soon") is None


# -- filters ----------------------------------------------------------------

@pytest.mark.parametrize("country,region,mode,ok", [
    ("United Kingdom", "", "On-site", True),
    ("United Kingdom (Remote)", "", "Remote", True),
    ("", "England", "Hybrid", False),       # 'england' not in this test list
    ("", "UK", "Hybrid", True),               # swapped columns
    ("", "", "Remote", True),
    ("Worldwide", "", "Remote", True),
    ("", "", "On-site", False),
    ("India", "", "Remote", False),           # remote but tied to another country
])
def test_location_ok(country, region, mode, ok):
    p = posting(country=country, region=region, work_mode=mode)
    assert location_ok(p, FILTERS) is ok


def test_title_ok_with_exclusion_and_software_fallback():
    assert title_ok(posting(role="Lighting TD"), FILTERS)
    assert not title_ok(posting(role="Lighting / FX TD"), FILTERS)
    assert not title_ok(posting(role="Compositor"), FILTERS)
    cfg = FilterConfig(title_keywords=["lighting"], software_keywords=["katana"])
    assert title_ok(posting(role="Compositor", software="Nuke, Katana"), cfg)


def test_apply_filters_merges_and_counts():
    cfg = FilterConfig(title_keywords=["lighting"], allowed_locations=["united kingdom",
                       "canada"], max_age_days=30)
    rows = [
        posting(locations=["London, United Kingdom"], date_issued="2026-11-20"),
        posting(country="Canada", locations=["Vancouver, Canada"]),
        posting(url="", role="Lighting Lead"),
        posting(role="Lighting Old", date_issued="2026-01-01"),
        posting(role="Producer"),
    ]
    kept, stats = apply_filters(rows, cfg, today=date(2026, 11, 24))
    assert len(kept) == 1
    assert kept[0].location == "London, United Kingdom, Vancouver, Canada"
    assert stats["title"] == 1
    assert stats["matched but no usable link"] == 1
    assert stats["matched but older than 30 days"] == 1


# -- google sheet source ----------------------------------------------------

SHEET = [
    ["Community job board", "", "", ""],
    ["", "", "", "", "", "", "", "", "", ""],
    ["Studio\n(Featured in blue)", "City", "Province/ State/Region", "Country",
     "Job Title", "Experience Level", "On-Site/\nRemote/Hybrid", "",
     "Source/Contact", "Software"],
    ["Acme", "London", "", "United Kingdom", "Lighting Artist", "Mid, Senior",
     "On-site", "November 2, 2026", "acme.com/jobs", "Katana"],
    ["Acme", "Leeds", "", "United Kingdom", "Lighting Artist", "", "On-site",
     "November 2, 2026", "https://acme.com/jobs", ""],
    ["Bolt", "", "", "", "Rigger", "Junior", "Remote", "November 1, 2026",
     "jobs@bolt.io", "Maya"],
    ["", "", "", "", "", "", "", "November 1, 2026", "", ""],
    ["Cog", "Paris", "", "France", "Rigger", "", "", "October 30, 2026", "", ""],
]


def test_sheet_parse_finds_header_and_date_column():
    src = GoogleSheetSource("Community Sheet", "id")
    postings = src.parse(SHEET)
    assert [p.studio for p in postings] == ["Acme", "Acme", "Bolt", "Cog"]
    first = postings[0]
    assert first.url == "https://acme.com/jobs"
    assert first.experience == "Mid / Senior"
    assert first.date_issued == "2026-11-02"
    assert first.locations == ["London, United Kingdom"]
    assert postings[2].url == "mailto:jobs@bolt.io"
    assert postings[0].key == postings[1].key  # merged later by filters


def test_sheet_header_override_and_missing_column():
    renamed = [row[:] for row in SHEET]
    renamed[2][4] = "Position"
    with pytest.raises(Fatal, match="no header row"):
        GoogleSheetSource("S", "id").parse(renamed)
    src = GoogleSheetSource("S", "id", columns={"title": "position"})
    assert len(src.parse(renamed)) == 4


def test_build_sources_rejects_unknown_type_and_keys():
    with pytest.raises(Fatal, match="unknown source type"):
        build_sources([{"type": "linkedin"}])
    with pytest.raises(Fatal, match="unknown keys"):
        build_sources([{"type": "google_sheet", "spreadsheet_id": "x", "gidd": 1}])
    with pytest.raises(Fatal, match="unknown sheet columns"):
        build_sources([{"type": "google_sheet", "spreadsheet_id": "x",
                        "columns": {"titel": "job"}}])


# -- config -----------------------------------------------------------------

def test_shipped_config_loads():
    cfg = load_config("config.toml")
    assert cfg.sources[0]["type"] == "google_sheet"
    assert "lighting" in cfg.filters.title_keywords
    assert cfg.notion_properties["url"] == "Posting URL"
    build_sources(cfg.sources)


def test_config_typos_are_reported():
    base = {"sources": [{"type": "google_sheet"}],
            "filters": {"title_keywords": ["x"]}}
    parse_config(base)
    with pytest.raises(Fatal, match="unknown keys.*title_keyword"):
        parse_config({**base, "filters": {"title_keywords": ["x"],
                                          "title_keyword": ["y"]}})
    with pytest.raises(Fatal, match="list of strings"):
        parse_config({**base, "filters": {"title_keywords": "rigging"}})
    with pytest.raises(Fatal, match="sources"):
        parse_config({"filters": {"title_keywords": ["x"]}})


# -- store ------------------------------------------------------------------

def test_seen_store_roundtrip(tmp_path):
    store = SeenStore(str(tmp_path / "sub" / "seen.db"))
    p = posting()
    assert not store.has(p)
    store.add(p, "page-1")
    store.add(p, "page-2")  # ignored, first record wins
    assert store.has(posting(role="  lighting   ARTIST ", url="http://example.com/job/"))
    assert len(store) == 1


# -- notion retries ---------------------------------------------------------

def _resp(status, body=None, headers=None):
    r = mock.Mock(status_code=status, headers=headers or {}, text="err")
    r.json.return_value = body or {}
    return r


def test_notion_retries_429_then_succeeds():
    n = Notion("t", "db", {})
    with mock.patch.object(n.session, "request", side_effect=[
        _resp(429, headers={"Retry-After": "0"}), _resp(502), _resp(200, {"id": "p1"}),
    ]) as req, mock.patch("job_tracker.notion.time.sleep"):
        assert n._request("POST", "/pages", "create") == {"id": "p1"}
    assert req.call_count == 3


def test_notion_does_not_retry_400():
    n = Notion("t", "db", {})
    with mock.patch.object(n.session, "request", return_value=_resp(400)) as req, \
            mock.patch("job_tracker.notion.time.sleep"):
        with pytest.raises(Fatal, match="400"):
            n._request("POST", "/pages", "create")
    assert req.call_count == 1
