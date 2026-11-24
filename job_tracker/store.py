"""Local record of every posting already handled.

Notion is still checked each run, but this store means a posting is never
re-added after you rename or delete its row in Notion, and that each posting
is scored by the AI only once, including ones scored too low to add. Rejected
postings can be scored again (see --rescore-rejected and
[scoring] rescore_on_profile_change).
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
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the first version; added to older databases on open.
EXTRA_COLUMNS = {
    "status": "TEXT",       # added | present | rejected | dismissed | closed
    "score": "INTEGER",
    "discipline": "TEXT",
    "reason": "TEXT",
}


class SeenStore:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self._cache: dict[tuple[str, bool], set[tuple[str, str]]] = {}
        self.db.executescript(SCHEMA)
        have = {row[1] for row in self.db.execute("PRAGMA table_info(seen)")}
        for name, kind in EXTRA_COLUMNS.items():
            if name not in have:
                self.db.execute(f"ALTER TABLE seen ADD COLUMN {name} {kind}")
        self.db.commit()

    def __len__(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def has(self, posting: Posting, skip_rejected: bool = False) -> bool:
        """Seen before: same link and title, or the same studio and title
        from a different source (the same job listed in two places).
        With skip_rejected, postings that scored too low count as unseen."""
        row = self.db.execute(
            "SELECT status FROM seen WHERE url = ? AND role = ?", posting.key
        ).fetchone()
        if row and not (skip_rejected and row[0] == "rejected"):
            return True
        return posting.studio_role in self._other_sources(posting.source, skip_rejected)

    def _other_sources(self, source: str, skip_rejected: bool) -> set[tuple[str, str]]:
        """(studio, role core) of everything recorded from other sources.
        Cached; add() clears the cache."""
        key = (source, skip_rejected)
        if key not in self._cache:
            self._cache[key] = {
                (normalise_studio(s), role_core(t))
                for s, t, status in self.db.execute(
                    "SELECT studio, title, status FROM seen WHERE source != ?", (source,))
                if not (skip_rejected and status == "rejected")
            }
        return self._cache[key]

    def add(
        self,
        posting: Posting,
        notion_id: str | None = None,
        status: str = "added",
        score=None,
    ) -> None:
        """Record a posting. `score` is a scoring.Score or None.
        An existing row is only updated if it was rejected (a rescore)."""
        url, role = posting.key
        self.db.execute(
            "INSERT INTO seen (url, role, source, studio, title, "
            "first_seen, notion_id, status, score, discipline, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url, role) DO UPDATE SET status = excluded.status, "
            "score = excluded.score, discipline = excluded.discipline, "
            "reason = excluded.reason, "
            "notion_id = COALESCE(excluded.notion_id, seen.notion_id) "
            "WHERE seen.status = 'rejected'",
            (url, role, posting.source, posting.studio, posting.role,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), notion_id,
             status,
             score.score if score else None,
             score.discipline if score else None,
             score.reason if score else None),
        )
        self.db.commit()
        self._cache.clear()

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                        "SET value = excluded.value", (key, value))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
