import argparse
import json
import sqlite3
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2 as httpx
import pytest

from jobtracker import __main__ as app
from jobtracker.config import ScoringConfig, parse_config
from jobtracker.models import Fatal, Posting
from jobtracker.notion import Notion
from jobtracker.scoring import Score, Scorer, describe
from jobtracker.store import SeenStore

CFG = ScoringConfig(enabled=True, profile="Rigger, UK.",
                    disciplines=["Rigging", "Lighting", "Other"])


def posting(role="Rigger", url="https://a.com/1", **kw) -> Posting:
    return Posting(role=role, studio="Acme", source="Test", url=url,
                   country="United Kingdom", locations=["London, United Kingdom"], **kw)


def reply(data, stop="end_turn"):
    return SimpleNamespace(
        stop_reason=stop,
        content=[SimpleNamespace(type="thinking"),
                 SimpleNamespace(type="text", text=json.dumps(data))],
        usage=SimpleNamespace(input_tokens=300, output_tokens=40),
    )


def fake_client(*responses):
    client = mock.Mock()
    client.beta.messages.create.side_effect = list(responses)
    return client


def api_error(cls, status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(status, request=req), body=None)


# -- scorer -----------------------------------------------------------------

def test_score_parses_and_clamps():
    client = fake_client(reply({"score": 14, "discipline": "Rigging",
                                "reason": " Character rigging in London. "}))
    s = Scorer(CFG, client).score(posting())
    assert s == Score(10, "Rigging", "Character rigging in London.")


def test_request_shape():
    client = fake_client(reply({"score": 7, "discipline": "Rigging", "reason": "ok"}))
    Scorer(CFG, client).score(posting(software="Maya"))
    kw = client.beta.messages.create.call_args.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["fallbacks"] == "default"
    assert kw["output_config"]["effort"] == "low"
    schema = kw["output_config"]["format"]["schema"]
    assert schema["properties"]["discipline"]["enum"] == CFG.disciplines
    assert "Rigger, UK." in kw["system"]
    assert "Software: Maya" in kw["messages"][0]["content"]


def test_no_fallbacks_for_haiku():
    cfg = ScoringConfig(**{**CFG.__dict__, "model": "claude-haiku-5-5"})
    client = fake_client(reply({"score": 7, "discipline": "Rigging", "reason": "ok"}))
    Scorer(cfg, client).score(posting())
    assert "fallbacks" not in client.beta.messages.create.call_args.kwargs


def test_transient_errors_and_refusals_return_none():
    client = fake_client(
        api_error(anthropic.RateLimitError, 429),
        api_error(anthropic.InternalServerError, 500),
        reply({}, stop="refusal"),
    )
    scorer = Scorer(CFG, client)
    assert [scorer.score(posting()) for _ in range(3)] == [None, None, None]


@pytest.mark.parametrize("cls,status", [
    (anthropic.AuthenticationError, 401),
    (anthropic.BadRequestError, 400),
])
def test_config_errors_are_fatal(cls, status):
    with pytest.raises(Fatal):
        Scorer(CFG, fake_client(api_error(cls, status))).score(posting())


def test_missing_credentials_is_fatal():
    client = fake_client(TypeError('"Could not resolve authentication method."'))
    with pytest.raises(Fatal, match="ANTHROPIC_API_KEY"):
        Scorer(CFG, client).score(posting())


def test_score_all_keys_by_posting_and_counts_tokens():
    a, b = posting("Rigger", "https://a.com/1"), posting("Lighter", "https://a.com/2")
    client = mock.Mock()
    client.beta.messages.create.side_effect = lambda **kw: reply(
        {"score": 8 if "Rigger" in kw["messages"][0]["content"] else 2,
         "discipline": "Other", "reason": "r"})
    scorer = Scorer(CFG, client)
    scores = scorer.score_all([a, b])
    assert scores[a.key].score == 8 and scores[b.key].score == 2
    assert scorer.input_tokens == 600


def test_describe_marks_missing_fields():
    text = describe(posting())
    assert "Software: not given" in text and "Location: London" in text


# -- config -----------------------------------------------------------------

BASE = {"sources": [{"type": "google_sheet"}], "filters": {"title_keywords": ["x"]}}


def test_scoring_config_defaults_off():
    assert parse_config(BASE).scoring.enabled is False


@pytest.mark.parametrize("scoring,msg", [
    ({"enabled": True}, "profile is empty"),
    ({"effort": "huge"}, "effort"),
    ({"min_score": 11}, "min_score"),
    ({"modle": "x"}, "unknown keys"),
])
def test_scoring_config_errors(scoring, msg):
    with pytest.raises(Fatal, match=msg):
        parse_config({**BASE, "scoring": scoring})


# -- store ------------------------------------------------------------------

def test_store_records_scores_and_upgrades_old_db(tmp_path):
    path = str(tmp_path / "seen.db")
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE seen (url TEXT NOT NULL, role TEXT NOT NULL,
        source TEXT NOT NULL, studio TEXT NOT NULL, title TEXT NOT NULL,
        first_seen TEXT NOT NULL, notion_id TEXT, PRIMARY KEY (url, role));""")
    old.close()
    store = SeenStore(path)
    store.add(posting(), status="rejected", score=Score(3, "Other", "FX role"))
    row = store.db.execute("SELECT status, score, reason FROM seen").fetchone()
    assert row == ("rejected", 3, "FX role")


# -- notion -----------------------------------------------------------------

def test_check_columns_names_missing_and_wrong_types():
    n = Notion("t", "db", {"fit_score": "Fit Score", "discipline": "Discipline"})
    schema = {"properties": {"Fit Score": {"type": "rich_text"}}}
    with mock.patch.object(n, "_request", return_value=schema):
        with pytest.raises(Fatal) as exc:
            n.check_columns({"fit_score": "number", "discipline": "select"})
    assert "'Fit Score' should be type number" in str(exc.value)
    assert "add a 'Discipline' column of type select" in str(exc.value)


# -- full run ---------------------------------------------------------------

def test_run_adds_good_rejects_poor_and_falls_back(tmp_path, monkeypatch):
    good, poor, broken, known = (posting("Rigger", "https://a.com/1"),
                                 posting("FX TD", "https://a.com/2"),
                                 posting("Creature TD", "https://a.com/3"),
                                 posting("Lighter", "https://a.com/4"))
    cfg = parse_config({**BASE, "state": {"db_path": str(tmp_path / "seen.db")},
                        "scoring": {"enabled": True, "profile": "p"}})
    scorer = mock.Mock()
    scorer.score_all.return_value = {good.key: Score(8, "Other", "fits"),
                                     poor.key: Score(2, "Other", "FX"),
                                     broken.key: None}
    notion = mock.Mock()
    notion.existing_pages.return_value = {known.key: {"id": "old", "experience": "",
                                                      "work_mode": ""}}
    notion.fill_blanks.return_value = False
    notion.create.side_effect = ["p1", "p2"]

    monkeypatch.setenv("NOTION_TOKEN", "t")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db")
    monkeypatch.setattr(app, "load_config", lambda _: cfg)
    monkeypatch.setattr(app, "collect", lambda _: [good, poor, broken, known])
    monkeypatch.setattr(app, "make_scorer", lambda *_: scorer)
    monkeypatch.setattr(app, "Notion", lambda *_: notion)

    args = argparse.Namespace(config="x", dry_run=False, no_score=False, limit=0,
                              prune=False)
    assert app.run(args) == 0

    scorer.score_all.assert_called_once_with([good, poor, broken])  # not `known`
    created = [c.args[0] for c in notion.create.call_args_list]
    assert created == [good, broken]
    db = sqlite3.connect(cfg.db_path)
    rows = dict(db.execute("SELECT title, status FROM seen"))
    assert rows == {"Rigger": "added", "FX TD": "rejected",
                    "Creature TD": "added", "Lighter": "present"}

    # Second run: nothing new to score or add.
    notion.create.reset_mock()
    scorer.score_all.reset_mock(return_value=True)
    scorer.score_all.return_value = {}
    assert app.run(args) == 0
    scorer.score_all.assert_called_once_with([])
    notion.create.assert_not_called()
