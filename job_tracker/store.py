"""Local record of every posting already handled.

Notion is still checked each run, but this store means a posting is never
re-added after you rename or delete its row in Notion, and that each posting
is scored by the AI only once, including ones scored too low to add.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from .models import Posting
from .text import normalise_studio, role_core

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    url         TEXT NOT NULL,
    role        TEXT NOT NULL,
    source      TEXT NOT NULL,
    studio      TEXT NOT NULL,
    title       TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    notion_id   TEXT,
    PRIMARY KEY (url, role)
);
"""

# Columns added after the first version; added to older databases on open.
EXTRA_COLUMNS = {
    "status": "TEXT",       # added | present | rejected
    "score": "INTEGER",
    "discipline": "TEXT",
    "reason": "TEXT",
}


class SeenStore:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self._cache: dict[str, set[tuple[str, str]]] = {}
        self.db.executescript(SCHEMA)
        have = {row[1] for row in self.db.execute("PRAGMA table_info(seen)")}
        for name, kind in EXTRA_COLUMNS.items():
            if name not in have:
                self.db.execute(f"ALTER TABLE seen ADD COLUMN {name} {kind}")
        self.db.commit()

    def __len__(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def has(self, posting: Posting) -> bool:
        """Seen before: same link and title, or the same studio and title
        from a different source (the same job listed in two places)."""
        if self.db.execute(
            "SELECT 1 FROM seen WHERE url = ? AND role = ?", posting.key
        ).fetchone():
            return True
        return posting.studio_role in self._other_sources(posting.source)

    def _other_sources(self, source: str) -> set[tuple[str, str]]:
        """(studio, role core) of everything recorded from other sources.
        Cached per source; add() clears the cache."""
        if source not in self._cache:
            self._cache[source] = {
                (normalise_studio(s), role_core(t))
                for s, t in self.db.execute(
                    "SELECT studio, title FROM seen WHERE source != ?", (source,))
            }
        return self._cache[source]

    def add(
        self,
        posting: Posting,
        notion_id: str | None = None,
        status: str = "added",
        score=None,
    ) -> None:
        """Record a posting. `score` is a scoring.Score or None."""
        url, role = posting.key
        self.db.execute(
            "INSERT OR IGNORE INTO seen (url, role, source, studio, title, "
            "first_seen, notion_id, status, score, discipline, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (url, role, posting.source, posting.studio, posting.role,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), notion_id,
             status,
             score.score if score else None,
             score.discipline if score else None,
             score.reason if score else None),
        )
        self.db.commit()
        self._cache.clear()

    def close(self) -> None:
        self.db.close()
