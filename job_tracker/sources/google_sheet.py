"""A public Google Sheet of job postings (the community VFX/animation sheet)."""

from __future__ import annotations

import csv
import io
import re
from typing import Any

import requests

from ..models import Fatal, Posting
from ..text import DEFAULT_DATE_FORMATS, parse_date, select_value, tidy_url

# Field -> header text as it appears in the sheet, matched case-insensitively
# by prefix after normalising. Override any of them under [sources.columns].
DEFAULT_COLUMNS = {
    "studio": "studio",
    "city": "city",
    "region": "province/state/region",
    "country": "country",
    "title": "job title",
    "experience": "experience level",
    "work_mode": "on-site/remote/hybrid",
    "date": "date",
    "url": "source/contact",
    "software": "software",
}
REQUIRED_FIELDS = ["studio", "title", "url"]
HEADER_SEARCH_ROWS = 15


def normalise_header(raw: str) -> str:
    """'Studio\\n(Featured listings in blue)' -> 'studio';
    ' On-Site/\\nRemote/Hybrid' -> 'on-site/remote/hybrid'."""
    text = re.sub(r"\([^)]*\)", " ", raw)
    text = re.sub(r"\s*/\s*", "/", text)
    return re.sub(r"\s+", " ", text).strip().lower()


class GoogleSheetSource:
    def __init__(
        self,
        name: str,
        spreadsheet_id: str,
        gid: int = 0,
        columns: dict[str, str] | None = None,
        date_formats: list[str] | None = None,
    ):
        self.name = name
        self.spreadsheet_id = spreadsheet_id
        self.gid = gid
        self.columns = {**DEFAULT_COLUMNS, **(columns or {})}
        self.date_formats = date_formats or DEFAULT_DATE_FORMATS

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> GoogleSheetSource:
        known = {"type", "name", "spreadsheet_id", "gid", "columns", "date_formats"}
        unknown = sorted(set(cfg) - known)
        if unknown:
            raise Fatal(f"config: google_sheet source has unknown keys {unknown}")
        if not isinstance(cfg.get("spreadsheet_id"), str):
            raise Fatal("config: google_sheet source needs a 'spreadsheet_id'")
        columns = cfg.get("columns", {})
        bad = sorted(set(columns) - set(DEFAULT_COLUMNS))
        if bad:
            raise Fatal(
                f"config: unknown sheet columns {bad}; "
                f"valid names are {sorted(DEFAULT_COLUMNS)}"
            )
        return cls(
            name=cfg.get("name", "Google Sheet"),
            spreadsheet_id=cfg["spreadsheet_id"],
            gid=int(cfg.get("gid", 0)),
            columns=columns,
            date_formats=cfg.get("date_formats"),
        )

    # -- fetching ----------------------------------------------------------

    def fetch_rows(self) -> list[list[str]]:
        # gviz endpoint: still works when the owner has disabled downloads for
        # viewers, which blocks /export with a 401. headers=0 stops Google
        # guessing header rows and merging them; find_header() finds the real one.
        url = (
            f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
            f"/gviz/tq?tqx=out:csv&gid={self.gid}&headers=0"
        )
        try:
            resp = requests.get(url, timeout=60, allow_redirects=True)
        except requests.RequestException as exc:
            raise Fatal(f"{self.name}: sheet download failed: {exc}") from None
        if resp.status_code != 200:
            raise Fatal(f"{self.name}: sheet download failed: HTTP {resp.status_code}")
        if "text/csv" not in resp.headers.get("content-type", ""):
            raise Fatal(
                f"{self.name}: sheet did not return CSV. It is probably no longer "
                "public; switch to the Sheets API with an API key."
            )
        return list(csv.reader(io.StringIO(resp.text)))

    def fetch(self) -> list[Posting]:
        return self.parse(self.fetch_rows())

    # -- parsing -----------------------------------------------------------

    def find_date_column(self, rows: list[list[str]], skip: set[int]) -> int | None:
        """The sheet's date column has no header, so pick it by content."""
        sample = rows[:40]
        best, best_hits = None, 0
        width = max((len(r) for r in sample), default=0)
        for i in range(width):
            if i in skip:
                continue
            hits = sum(
                1 for r in sample if i < len(r) and parse_date(r[i], self.date_formats)
            )
            if hits > best_hits:
                best, best_hits = i, hits
        return best if best_hits >= max(3, len(sample) // 2) else None

    def find_header(self, rows: list[list[str]]) -> tuple[int, dict[str, int]]:
        """Locate the header row and map field name -> column index."""
        cols = self.columns
        for idx, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
            headers = [normalise_header(c) for c in row]
            if not (any(h.startswith(cols["studio"]) for h in headers)
                    and any(h.startswith(cols["title"]) for h in headers)):
                continue
            mapping: dict[str, int] = {}
            for field, header in cols.items():
                for i, h in enumerate(headers):
                    if h and h.startswith(header) and i not in mapping.values():
                        mapping[field] = i
                        break
            missing = [cols[f] for f in REQUIRED_FIELDS if f not in mapping]
            if missing:
                raise Fatal(f"{self.name}: header row is missing columns {missing}")
            if "date" not in mapping:
                date_col = self.find_date_column(rows[idx + 1:], set(mapping.values()))
                if date_col is not None:
                    mapping["date"] = date_col
                else:
                    print(f"  warning: {self.name}: no date column found; "
                          "Date Issued left blank")
            return idx, mapping
        raise Fatal(
            f"{self.name}: no header row found in the first {HEADER_SEARCH_ROWS} "
            "rows. The sheet layout has changed; update [sources.columns] in "
            "config.toml."
        )

    def parse(self, rows: list[list[str]]) -> list[Posting]:
        header_idx, mapping = self.find_header(rows)

        def cell(row: list[str], field: str) -> str:
            i = mapping.get(field)
            return row[i].strip() if i is not None and i < len(row) else ""

        postings = []
        for row in rows[header_idx + 1:]:
            title, studio = cell(row, "title"), cell(row, "studio")
            if not title or not studio:
                continue
            city, region, country = cell(row, "city"), cell(row, "region"), cell(row, "country")
            loc = ", ".join(p for p in (city, country or region) if p)
            postings.append(Posting(
                role=title,
                studio=studio,
                source=self.name,
                url=tidy_url(cell(row, "url")),
                city=city,
                region=region,
                country=country,
                work_mode=select_value(cell(row, "work_mode")),
                experience=select_value(cell(row, "experience")),
                software=cell(row, "software"),
                date_issued=parse_date(cell(row, "date"), self.date_formats),
                locations=[loc] if loc else [],
            ))
        return postings
