"""Local record of every posting already handled.

Notion is still checked each run, but this store means a posting is never
re-added after you rename or delete its row in Notion, and lets later steps
(such as AI scoring) run once per posting rather than every day.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from .models import Posting

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


class SeenStore:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def __len__(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def has(self, posting: Posting) -> bool:
        return self.db.execute(
            "SELECT 1 FROM seen WHERE url = ? AND role = ?", posting.key
        ).fetchone() is not None

    def add(self, posting: Posting, notion_id: str | None = None) -> None:
        url, role = posting.key
        self.db.execute(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, role, posting.source, posting.studio, posting.role,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), notion_id),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
