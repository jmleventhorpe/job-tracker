from unittest import mock

import pytest
import requests

from job_tracker.config import FilterConfig
from job_tracker.filters import apply_filters
from job_tracker.models import Fatal, Posting
from job_tracker.sources import build_sources
from job_tracker.sources.job_boards import JobBoardsSource, _place
from job_tracker.store import SeenStore

FEEDS = {
    "https://boards-api.greenhouse.io/v1/boards/spi/jobs": {"jobs": [
        {"title": "Pipeline TD", "absolute_url": "https://gh/spi/1",
         "company_name": "Sony Pictures Imageworks",
         "location": {"name": "Vancouver, British Columbia, Canada"},
         "first_published": "2022-01-01T00:00:00-04:00",
         "updated_at": "2026-11-20T10:00:00-04:00"}]},
    "https://api.lever.co/v0/postings/sky": [
        {"text": "Rigger", "hostedUrl": "https://lever/sky/1", "country": "ES",
         "workplaceType": "hybrid", "createdAt": 1776335646204,
         "categories": {"location": "Madrid"}}],
    "https://api.ashbyhq.com/posting-api/job-board/glitch": {"jobs": [
        {"title": "Lighting Artist", "jobUrl": "https://ashby/1", "isListed": True,
         "isRemote": True, "workplaceType": "Remote", "location": "Sydney",
         "publishedAt": "2026-11-17T04:49:04.147+00:00",
         "address": {"postalAddress": {"addressCountry": "Australia",
                                       "addressLocality": "Sydney"}}},
        {"title": "Hidden", "jobUrl": "https://ashby/2", "isListed": False}]},
    "https://api.smartrecruiters.com/v1/companies/rodeo/postings": {
        "totalFound": 1, "content": [
            {"id": "77", "name": "Lighting TD", "company": {"name": "Rodeo FX"},
             "location": {"city": "Montreal", "country": "ca", "hybrid": True},
             "experienceLevel": {"label": "Mid-Senior Level"},
             "releasedDate": "2026-11-23T17:28:38.277Z"}]},
    "https://apply.workable.com/api/v1/widget/accounts/oneofus": {
        "name": "One Of Us", "jobs": [
            {"title": "Senior Lighting Artist", "url": "https://wk/1", "city": "London",
             "country": "United Kingdom", "telecommuting": False, "experience": None,
             "published_on": "2026-10-20"}]},
}


def fake_get(self, url, **params):
    if url not in FEEDS:
        raise requests.HTTPError("404")
    return FEEDS[url]


@pytest.fixture
def source():
    boards = [{"site": "greenhouse", "slug": "spi"},
              {"site": "lever", "slug": "sky", "studio": "Skydance Animation"},
              {"site": "ashby", "slug": "glitch", "studio": "Glitch Productions"},
              {"site": "smartrecruiters", "slug": "rodeo"},
              {"site": "workable", "slug": "oneofus", "studio": "One of Us"},
              {"site": "workable", "slug": "gone"}]
    with mock.patch.object(JobBoardsSource, "_get", fake_get):
        yield JobBoardsSource("Studio Boards", boards)


def test_reads_every_site_and_skips_broken_board(source, capsys):
    by_role = {p.role: p for p in source.fetch()}
    assert "board workable:gone" in capsys.readouterr().out
    assert set(by_role) == {"Pipeline TD", "Rigger", "Lighting Artist", "Lighting TD",
                            "Senior Lighting Artist"}  # unlisted Ashby job skipped

    gh = by_role["Pipeline TD"]
    assert gh.studio == "Sony Pictures Imageworks" and gh.source == "Studio Boards"
    assert gh.date_issued == "2026-11-20"  # last update, not 2022
    assert gh.location == "Vancouver, British Columbia, Canada"

    lv = by_role["Rigger"]
    assert (lv.studio, lv.country, lv.work_mode) == ("Skydance Animation", "Spain", "Hybrid")
    assert by_role["Lighting Artist"].work_mode == "Remote"
    sr = by_role["Lighting TD"]
    assert (sr.studio, sr.country, sr.experience) == ("Rodeo FX", "Canada", "Mid-Senior Level")
    assert sr.url == "https://jobs.smartrecruiters.com/rodeo/77"
    assert by_role["Senior Lighting Artist"].experience == ""  # null in feed


def test_board_postings_pass_the_location_filter(source):
    cfg = FilterConfig(title_keywords=["lighting", "rigger", "pipeline"],
                       allowed_locations=["canada", "spain", "australia", "united kingdom"])
    kept, _ = apply_filters(source.fetch(), cfg)
    assert len(kept) == 5


def test_place_does_not_repeat_country():
    assert _place("Los Angeles, United States", "United States") == "Los Angeles, United States"
    assert _place("Madrid", "Spain") == "Madrid, Spain"


def test_config_validation():
    with pytest.raises(Fatal, match="site 'linkedin'"):
        build_sources([{"type": "job_boards", "boards": [{"site": "linkedin", "slug": "x"}]}])
    with pytest.raises(Fatal, match="needs site and slug"):
        build_sources([{"type": "job_boards", "boards": [{"site": "lever"}]}])
    with pytest.raises(Fatal, match="unknown keys"):
        build_sources([{"type": "job_boards", "boards": [{"site": "lever", "slug": "x",
                                                         "studoi": "y"}]}])


def p(source, studio, role, url):
    return Posting(role=role, studio=studio, source=source, url=url, country="Canada",
                   locations=[f"{studio} office"])


def test_same_job_from_two_sources_is_merged_but_not_within_one():
    cfg = FilterConfig(title_keywords=["lighting"], allowed_locations=["canada"])
    rows = [
        p("Community Sheet", "Outpost VFX", "Lighting Artist", "https://linkedin/1"),
        p("Studio Boards", "Outpost", "Lighting  Artist", "https://sr/1"),
        p("Community Sheet", "Stim", "Lighting Artist", "https://stim/a"),
        p("Community Sheet", "Stim", "Lighting Artist", "https://stim/b"),
    ]
    kept, stats = apply_filters(rows, cfg)
    assert [(k.studio, k.url) for k in kept] == [
        ("Outpost VFX", "https://linkedin/1"),
        ("Stim", "https://stim/a"), ("Stim", "https://stim/b")]
    assert stats["listed by Community Sheet too"] == 1


def test_store_recognises_same_job_from_another_source(tmp_path):
    store = SeenStore(str(tmp_path / "seen.db"))
    store.add(p("Community Sheet", "Rodeo FX", "Lighting TD", "https://linkedin/9"))
    assert store.has(p("Studio Boards", "Rodeo FX", "Lighting TD", "https://sr/9"))
    # Same source, different link: a different job.
    assert not store.has(p("Community Sheet", "Rodeo FX", "Lighting TD", "https://x/2"))


def test_role_core_ignores_seniority_and_contract_words():
    from job_tracker.text import role_core
    assert role_core("Freelance Senior Lighting Artist (Contract)") == role_core("Lighting Artist")
    assert role_core("Senior Pipeline TD, Lighting/Rendering") == \
        role_core("Pipeline TD, Lighting/Rendering")
    assert role_core("Lead Lighting Artist") != role_core("Lighting Artist")
    assert role_core("Lighting Supervisor") != role_core("Lighting Artist")


def test_reworded_job_from_two_sources_is_merged():
    cfg = FilterConfig(title_keywords=["lighting"], allowed_locations=["canada"])
    rows = [p("Community Sheet", "Outpost VFX", "Lighting Artist", "https://linkedin/1"),
            p("Studio Boards", "Outpost VFX", "Freelance Senior Lighting Artist", "https://sr/1"),
            p("Studio Boards", "Outpost VFX", "Lead Lighting Artist", "https://sr/2")]
    kept, _ = apply_filters(rows, cfg)
    assert [k.role for k in kept] == ["Lighting Artist", "Lead Lighting Artist"]


def test_age_filter_skips_postings_from_open_boards():
    cfg = FilterConfig(title_keywords=["rigger"], allowed_locations=["canada"],
                       max_age_days=90)
    old = dict(country="Canada", date_issued="2022-01-01")
    rows = [Posting(role="Rigger", studio="A", source="Sheet", url="u1", **old),
            Posting(role="Rigger", studio="B", source="Boards", url="u2", still_open=True,
                    **old)]
    kept, _ = apply_filters(rows, cfg)
    assert [k.studio for k in kept] == ["B"]
