"""Data shared by every source, filter and sink."""

from __future__ import annotations

from dataclasses import dataclass, field

from .text import normalise_role, normalise_studio, normalise_url, role_core


class Fatal(Exception):
    """An error that should stop the run with a clear message."""


@dataclass
class Posting:
    """One job advert, in a source-independent shape.

    Sources fill in what they know; anything left blank is simply skipped
    when writing to Notion.
    """

    role: str
    studio: str
    source: str
    url: str = ""
    city: str = ""
    region: str = ""
    country: str = ""
    work_mode: str = ""
    experience: str = ""
    software: str = ""
    date_issued: str | None = None  # ISO yyyy-mm-dd
    locations: list[str] = field(default_factory=list)
    # True when the source only lists currently open jobs (a studio's own
    # board), so the posting's age says nothing about whether it is stale.
    still_open: bool = False

    @property
    def key(self) -> tuple[str, str]:
        """Identity used for de-duplication, both in Notion and the seen store."""
        return (normalise_url(self.url), normalise_role(self.role))

    @property
    def studio_role(self) -> tuple[str, str]:
        """Looser identity for spotting the same job listed by two sources
        under different links. Only compared across different sources."""
        return (normalise_studio(self.studio), role_core(self.role))

    @property
    def location(self) -> str:
        seen, out = set(), []
        for loc in self.locations:
            if loc and loc.lower() not in seen:
                seen.add(loc.lower())
                out.append(loc)
        return ", ".join(out)

    def merge(self, other: Posting) -> None:
        """Fold a duplicate row (same URL + role, another city) into this one."""
        self.locations.extend(other.locations)
        self.date_issued = self.date_issued or other.date_issued
        self.experience = self.experience or other.experience
        self.work_mode = self.work_mode or other.work_mode
