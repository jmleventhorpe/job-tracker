"""Job sources. Each [[sources]] table in config.toml becomes one Source.

To add a new kind of source:
  1. Write a class with a `name` attribute, a `from_config(cls, cfg: dict)`
     classmethod and a `fetch() -> list[Posting]` method.
  2. Register it in SOURCE_TYPES below under the `type` used in config.toml.
Filtering, de-duplication and Notion writes are shared, so a source only
has to turn its listings into Posting objects.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..models import Fatal, Posting
from .google_sheet import GoogleSheetSource


class Source(Protocol):
    name: str

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Source": ...

    def fetch(self) -> list[Posting]: ...


SOURCE_TYPES: dict[str, type] = {
    "google_sheet": GoogleSheetSource,
}


def build_sources(configs: list[dict[str, Any]]) -> list[Source]:
    sources = []
    for cfg in configs:
        kind = cfg["type"]
        if kind not in SOURCE_TYPES:
            raise Fatal(
                f"config: unknown source type '{kind}'. "
                f"Known types: {', '.join(sorted(SOURCE_TYPES))}"
            )
        sources.append(SOURCE_TYPES[kind].from_config(cfg))
    names = [s.name for s in sources]
    if len(set(names)) != len(names):
        raise Fatal(f"config: source names must be unique, got {names}")
    return sources
