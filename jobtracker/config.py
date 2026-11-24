"""Load and check config.toml, so a typo fails with a clear message
instead of a KeyError traceback halfway through a run."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import Any

from .models import Fatal

# Notion column names the sync writes to. Override any of them under
# [notion.properties] if you rename a column in Notion.
DEFAULT_NOTION_PROPERTIES = {
    "role": "Role",
    "studio": "Studio",
    "source": "Source",
    "location": "Location",
    "url": "Posting URL",
    "date_issued": "Date Issued",
    "experience": "Experience",
    "work_mode": "Work Mode",
    # Written only when [scoring] is enabled.
    "fit_score": "Fit Score",
    "discipline": "Discipline",
    "fit_reason": "Fit Reason",
    # Read only by --prune.
    "interest": "Interest",
    "status": "Status",
}

# The Interest option that --prune records and moves to the trash.
DEFAULT_NOT_INTERESTED = "Not Interested"

DEFAULT_REMOTE_WORDS = ["remote", "worldwide", "anywhere", "global"]


@dataclass
class FilterConfig:
    title_keywords: list[str]
    title_exclusions: list[str] = field(default_factory=list)
    software_keywords: list[str] = field(default_factory=list)
    allowed_locations: list[str] = field(default_factory=list)
    keep_remote_without_country: bool = True
    remote_words: list[str] = field(default_factory=lambda: list(DEFAULT_REMOTE_WORDS))
    max_age_days: int = 0


EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]


@dataclass
class ScoringConfig:
    enabled: bool = False
    model: str = "claude-opus-5-5"
    effort: str = "low"
    min_score: int = 6
    max_workers: int = 4
    disciplines: list[str] = field(default_factory=lambda: ["Other"])
    profile: str = ""


@dataclass
class Config:
    sources: list[dict[str, Any]]
    filters: FilterConfig
    notion_properties: dict[str, str]
    db_path: str
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    not_interested: str = DEFAULT_NOT_INTERESTED


class _Section:
    """Typed reads from one TOML table, tracking which keys were used."""

    def __init__(self, data: Any, where: str):
        if not isinstance(data, dict):
            raise Fatal(f"config: [{where}] must be a table")
        self.data, self.where, self.used = data, where, set()

    def get(self, key: str, kind: type | tuple[type, ...], default: Any = ...) -> Any:
        self.used.add(key)
        if key not in self.data:
            if default is ...:
                raise Fatal(f"config: [{self.where}] is missing '{key}'")
            return default
        value = self.data[key]
        if kind is list:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise Fatal(f"config: [{self.where}] '{key}' must be a list of strings")
        elif not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise Fatal(f"config: [{self.where}] '{key}' has the wrong type")
        return value

    def reject_unknown(self) -> None:
        unknown = sorted(set(self.data) - self.used)
        if unknown:
            raise Fatal(
                f"config: [{self.where}] has unknown keys {unknown} (typo?)"
            )


def parse_config(raw: dict[str, Any]) -> Config:
    top = _Section(raw, "top level")

    state = _Section(top.get("state", dict, {}), "state")
    db_path = state.get("db_path", str, "data/seen.db")
    state.reject_unknown()

    top.used.add("sources")
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        raise Fatal("config: add at least one [[sources]] table")
    for i, src in enumerate(sources):
        if not isinstance(src, dict) or "type" not in src:
            raise Fatal(f"config: [[sources]] #{i + 1} needs a 'type'")

    f = _Section(top.get("filters", dict), "filters")
    loc = _Section(top.get("locations", dict, {}), "locations")
    filters = FilterConfig(
        title_keywords=f.get("title_keywords", list),
        title_exclusions=f.get("title_exclusions", list, []),
        software_keywords=f.get("software_keywords", list, []),
        keep_remote_without_country=f.get("keep_remote_without_country", bool, True),
        remote_words=f.get("remote_words", list, DEFAULT_REMOTE_WORDS),
        max_age_days=f.get("max_age_days", int, 0),
        allowed_locations=loc.get("allowed", list, []),
    )
    f.reject_unknown()
    loc.reject_unknown()
    if not filters.title_keywords:
        raise Fatal("config: [filters] title_keywords is empty, nothing would match")

    notion = _Section(top.get("notion", dict, {}), "notion")
    props_raw = _Section(notion.get("properties", dict, {}), "notion.properties")
    props = dict(DEFAULT_NOTION_PROPERTIES)
    for key in DEFAULT_NOTION_PROPERTIES:
        props[key] = props_raw.get(key, str, props[key])
    props_raw.reject_unknown()
    not_interested = notion.get("not_interested", str, DEFAULT_NOT_INTERESTED)
    notion.reject_unknown()

    scoring = _parse_scoring(top.get("scoring", dict, {}))

    top.reject_unknown()
    return Config(
        sources=sources,
        filters=filters,
        notion_properties=props,
        db_path=db_path,
        scoring=scoring,
        not_interested=not_interested,
    )


def _parse_scoring(raw: dict[str, Any]) -> ScoringConfig:
    s = _Section(raw, "scoring")
    d = ScoringConfig()
    cfg = ScoringConfig(
        enabled=s.get("enabled", bool, d.enabled),
        model=s.get("model", str, d.model),
        effort=s.get("effort", str, d.effort),
        min_score=s.get("min_score", int, d.min_score),
        max_workers=s.get("max_workers", int, d.max_workers),
        disciplines=s.get("disciplines", list, d.disciplines),
        profile=s.get("profile", str, d.profile),
    )
    s.reject_unknown()
    if cfg.effort not in EFFORT_LEVELS:
        raise Fatal(f"config: [scoring] effort must be one of {EFFORT_LEVELS}")
    if not 0 <= cfg.min_score <= 10:
        raise Fatal("config: [scoring] min_score must be between 0 and 10")
    if not 1 <= cfg.max_workers <= 16:
        raise Fatal("config: [scoring] max_workers must be between 1 and 16")
    if not cfg.disciplines:
        raise Fatal("config: [scoring] disciplines must not be empty")
    if cfg.enabled and not cfg.profile.strip():
        raise Fatal("config: [scoring] is enabled but profile is empty")
    return cfg


def load_config(path: str) -> Config:
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise Fatal(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise Fatal(f"config: {path} is not valid TOML: {exc}") from None
    return parse_config(raw)
