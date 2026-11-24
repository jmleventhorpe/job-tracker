"""Small text helpers with no dependencies on the rest of the package."""

from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache

DEFAULT_DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y"]

_BARE_DOMAIN = re.compile(r"^(www\.)?[\w-]+(\.[\w-]+)*\.[a-z]{2,}(/\S*)?$", re.I)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalise_role(role: str) -> str:
    return squash(role).lower()


_STUDIO_FILLER = {"the", "studio", "studios", "animation", "entertainment",
                  "productions", "production", "inc", "ltd", "llc", "limited",
                  "vfx", "co"}


_ROLE_FILLER = {"senior", "sr", "junior", "jr", "mid", "experienced", "freelance",
                "contract", "temp", "temporary", "month", "months", "fixed", "term",
                "level", "the", "and", "of", "a", "an"}


def role_core(role: str) -> str:
    """'Freelance Senior Lighting Artist (Contract)' -> 'artist lighting'.

    Drops seniority, contract words, numbers and anything in brackets, so the
    same job titled slightly differently by two sources still matches.
    Lead and supervisor are kept: those are different jobs.
    """
    text = re.sub(r"\([^)]*\)", " ", role.lower())
    words = [w for w in re.findall(r"[a-z]+", text) if w not in _ROLE_FILLER]
    return " ".join(sorted(set(words)))


def normalise_studio(studio: str) -> str:
    """'Outpost VFX' and 'Outpost' -> 'outpost', so sources can be compared."""
    words = re.findall(r"[a-z0-9]+", studio.lower())
    kept = [w for w in words if w not in _STUDIO_FILLER]
    return " ".join(kept or words)


def normalise_url(url: str) -> str:
    u = url.strip().lower().rstrip("/")
    for prefix in ("https://", "http://", "mailto:", "www."):
        if u.startswith(prefix):
            u = u[len(prefix):]
    return u


def tidy_url(raw: str) -> str:
    """Turn a source/contact cell into a usable link, or '' if it isn't one."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.lower().startswith(("http://", "https://")):
        return raw
    if " " in raw:
        return ""
    if "@" in raw:
        return f"mailto:{raw}"
    if _BARE_DOMAIN.match(raw):
        return f"https://{raw}"
    return ""


def select_value(raw: str) -> str:
    """Notion select options can't contain commas and cap at 100 chars."""
    return squash(raw).replace(",", " /")[:100]


@lru_cache(maxsize=512)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    # Lookarounds instead of \b, so keywords that start or end with a symbol
    # ("c++", ".net", "3d") still match as whole words.
    return re.compile(rf"(?<!\w){re.escape(squash(keyword).lower())}(?!\w)")


def keyword_hit(text: str, keywords: list[str]) -> str | None:
    """Return the first keyword found in text as a whole word, else None."""
    text = squash(text).lower()
    for k in keywords:
        if _keyword_pattern(k).search(text):
            return k
    return None


def parse_date(raw: str, formats: list[str] | None = None) -> str | None:
    raw = squash(raw)
    if not raw:
        return None
    for fmt in formats or DEFAULT_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def place_parts(raw: str) -> set[str]:
    """'United Kingdom (Remote)' -> {'united kingdom (remote)', 'united kingdom', 'remote'}.

    Splits a location cell on brackets and separators so values the sheet
    writes loosely still match the allowed list.
    """
    raw = squash(raw).lower()
    if not raw:
        return set()
    parts = {raw}
    for piece in re.split(r"[()\[\]/,;|&]+|\s+-\s+|\s+or\s+|\s+and\s+", raw):
        piece = piece.strip(" .")
        if piece:
            parts.add(piece)
    return parts
