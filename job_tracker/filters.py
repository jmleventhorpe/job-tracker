"""Source-independent filtering: location, keywords, link and age."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from .config import FilterConfig
from .models import Posting
from .text import keyword_hit, place_parts, squash


def _pieces(raw: str) -> set[str]:
    """The separate place names in a cell, without the whole-cell string
    when the cell was split ('UK (Remote)' -> {'uk', 'remote'})."""
    parts = place_parts(raw)
    whole = squash(raw).lower()
    return (parts - {whole}) or parts


def location_ok(p: Posting, cfg: FilterConfig) -> bool:
    allowed = {a.lower() for a in cfg.allowed_locations}
    if (place_parts(p.country) | place_parts(p.region)) & allowed:
        return True
    if not cfg.keep_remote_without_country:
        return False
    # Keep remote roles whose location is blank or only says "remote",
    # "worldwide" etc. A remote role tied to a non-allowed country is dropped.
    remote = {w.lower() for w in cfg.remote_words}
    pieces = _pieces(p.country) | _pieces(p.region)
    if any(piece not in remote for piece in pieces):
        return False
    return keyword_hit(f"{p.work_mode} {p.country} {p.region}", cfg.remote_words) is not None


def title_ok(p: Posting, cfg: FilterConfig) -> bool:
    if cfg.title_exclusions and keyword_hit(p.role, cfg.title_exclusions):
        return False
    if keyword_hit(p.role, cfg.title_keywords):
        return True
    return bool(cfg.software_keywords and keyword_hit(p.software, cfg.software_keywords))


def apply_filters(
    postings: list[Posting], cfg: FilterConfig, today: date | None = None
) -> tuple[list[Posting], Counter[str]]:
    """Return matching postings (duplicates merged) and counts of why rows were dropped."""
    today = today or date.today()
    # ISO date strings compare correctly as strings.
    cutoff = (
        (today - timedelta(days=cfg.max_age_days)).isoformat()
        if cfg.max_age_days else None
    )
    stats: Counter[str] = Counter()
    merged: dict[tuple[str, str], Posting] = {}

    for p in postings:
        if not location_ok(p, cfg):
            stats["location"] += 1
            continue
        if not title_ok(p, cfg):
            stats["title"] += 1
            continue
        # Counted separately: these matched, so worth knowing they were lost.
        if not p.url:
            stats["matched but no usable link"] += 1
            continue
        if cutoff and p.date_issued and p.date_issued < cutoff:
            stats[f"matched but older than {cfg.max_age_days} days"] += 1
            continue
        existing = merged.get(p.key)
        if existing:
            existing.merge(p)
        else:
            merged[p.key] = p

    return list(merged.values()), stats
