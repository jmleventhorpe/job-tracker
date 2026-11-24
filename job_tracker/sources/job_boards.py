"""Studios' own job boards, read through their hiring systems' public feeds.

Supported sites (all free, no key needed):
  greenhouse       boards-api.greenhouse.io
  lever            api.lever.co
  ashby            api.ashbyhq.com
  smartrecruiters  api.smartrecruiters.com
  workable         apply.workable.com

Each board is the studio's slug on that site: the part of its job-page URL
after the site name, e.g. job-boards.greenhouse.io/sonypicturesimageworks.
One board that cannot be read is reported and skipped; the rest still run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import requests

from ..models import Fatal, Posting
from ..text import select_value

# ISO 3166 alpha-2 -> name, for feeds that only give a country code (Lever).
# Covers the countries in the default [locations] list; others pass through
# as the code and fall to the location filter.
COUNTRIES = {
    "GB": "United Kingdom", "UK": "United Kingdom", "IE": "Ireland",
    "US": "United States", "CA": "Canada", "MX": "Mexico",
    "FR": "France", "DE": "Germany", "ES": "Spain", "PT": "Portugal",
    "IT": "Italy", "NL": "Netherlands", "BE": "Belgium", "LU": "Luxembourg",
    "CH": "Switzerland", "AT": "Austria", "DK": "Denmark", "SE": "Sweden",
    "NO": "Norway", "FI": "Finland", "IS": "Iceland", "PL": "Poland",
    "CZ": "Czech Republic", "SK": "Slovakia", "HU": "Hungary", "RO": "Romania",
    "BG": "Bulgaria", "GR": "Greece", "HR": "Croatia", "SI": "Slovenia",
    "RS": "Serbia", "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania",
    "MT": "Malta", "CY": "Cyprus", "UA": "Ukraine",
    "AU": "Australia", "NZ": "New Zealand",
    "BR": "Brazil", "AR": "Argentina", "CL": "Chile", "CO": "Colombia",
    "PE": "Peru", "UY": "Uruguay",
    "JP": "Japan", "KR": "South Korea", "CN": "China", "HK": "Hong Kong",
    "TW": "Taiwan", "SG": "Singapore", "MY": "Malaysia", "TH": "Thailand",
    "VN": "Vietnam", "PH": "Philippines", "ID": "Indonesia", "IN": "India",
    "NP": "Nepal", "LK": "Sri Lanka",
}


def _country(code_or_name: str) -> str:
    value = (code_or_name or "").strip()
    return COUNTRIES.get(value.upper(), value) if len(value) == 2 else value


def _date(value: Any) -> str | None:
    """ISO string or epoch milliseconds -> yyyy-mm-dd."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return str(value)[:10] or None


def _place(city: str, country: str) -> str:
    """'Los Angeles, United States' without repeating a country already there."""
    city, country = (city or "").strip(), (country or "").strip()
    if country and country.lower() in city.lower():
        return city
    return ", ".join(p for p in (city, country) if p)


def _mode(remote: bool = False, hybrid: bool = False, text: str = "") -> str:
    text = (text or "").lower().replace("_", "-")
    if hybrid or "hybrid" in text:
        return "Hybrid"
    if remote or "remote" in text:
        return "Remote"
    if text in ("on-site", "onsite", "in-office"):
        return "On-site"
    return ""


class JobBoardsSource:
    SITES = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable")

    def __init__(self, name: str, boards: list[dict[str, str]]):
        self.name = name
        self.boards = boards
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "job-tracker"

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> JobBoardsSource:
        unknown = sorted(set(cfg) - {"type", "name", "boards"})
        if unknown:
            raise Fatal(f"config: job_boards source has unknown keys {unknown}")
        boards = cfg.get("boards")
        if not isinstance(boards, list) or not boards:
            raise Fatal("config: job_boards source needs a 'boards' list")
        for i, b in enumerate(boards, 1):
            if not isinstance(b, dict) or not {"site", "slug"} <= set(b):
                raise Fatal(f"config: job_boards board #{i} needs site and slug")
            extra = sorted(set(b) - {"site", "slug", "studio"})
            if extra:
                raise Fatal(f"config: job_boards board #{i} has unknown keys {extra}")
            if b["site"] not in cls.SITES:
                raise Fatal(f"config: job_boards board #{i} site '{b['site']}' must "
                            f"be one of {', '.join(cls.SITES)}")
        return cls(name=cfg.get("name", "Studio Boards"), boards=boards)

    # -- fetching ----------------------------------------------------------

    def _get(self, url: str, **params) -> Any:
        resp = self.session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def fetch(self) -> list[Posting]:
        postings: list[Posting] = []
        for board in self.boards:
            label = f"{board['site']}:{board['slug']}"
            try:
                found = self.read_board(board)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                print(f"  warning: could not read board {label}: {exc}")
                continue
            postings.extend(found)
        return postings

    def read_board(self, board: dict[str, str]) -> list[Posting]:
        reader: Callable[[str], list[dict]] = getattr(self, f"_{board['site']}")
        studio_override = board.get("studio", "")
        postings = []
        for raw in reader(board["slug"]):
            # Feeds use null for missing fields; treat every one as "".
            r = {k: ("" if v is None else v) for k, v in raw.items()}
            if not r.get("role") or not r.get("url"):
                continue
            postings.append(Posting(
                role=str(r["role"]).strip(),
                studio=studio_override or r.get("studio") or board["slug"],
                source=self.name,
                url=r["url"],
                city=r.get("city", ""),
                region=r.get("region", ""),
                country=r.get("country", ""),
                work_mode=select_value(r.get("work_mode", "")),
                experience=select_value(r.get("experience", "")),
                date_issued=r.get("date") or None,
                locations=[r["location"]] if r.get("location") else [],
                still_open=True,  # boards only list open jobs
            ))
        return postings

    # -- one reader per site, each returning plain dicts -------------------

    def _greenhouse(self, slug: str) -> list[dict]:
        data = self._get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        out = []
        for j in data["jobs"]:
            loc = (j.get("location") or {}).get("name", "")
            out.append({
                "role": j["title"], "url": j["absolute_url"],
                "studio": j.get("company_name", ""),
                # "Montréal, Quebec, Canada": the location filter splits on commas.
                "country": loc, "location": loc,
                "work_mode": _mode(text=loc),
                # Last update, not first_published: studios keep evergreen roles
                # open for years, and the age filter would drop them.
                "date": _date(j.get("updated_at") or j.get("first_published")),
            })
        return out

    def _lever(self, slug: str) -> list[dict]:
        data = self._get(f"https://api.lever.co/v0/postings/{slug}", mode="json")
        out = []
        for j in data:
            cats = j.get("categories") or {}
            city = cats.get("location", "")
            country = _country(j.get("country", ""))
            out.append({
                "role": j["text"], "url": j["hostedUrl"],
                "city": city, "country": country,
                "location": _place(city, country),
                "work_mode": _mode(text=j.get("workplaceType", "")),
                "date": _date(j.get("createdAt")),
            })
        return out

    def _ashby(self, slug: str) -> list[dict]:
        data = self._get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        out = []
        for j in data["jobs"]:
            if j.get("isListed") is False:
                continue
            addr = ((j.get("address") or {}).get("postalAddress") or {})
            city = addr.get("addressLocality") or j.get("location", "")
            country = addr.get("addressCountry", "")
            out.append({
                "role": j["title"], "url": j["jobUrl"],
                "city": city, "region": addr.get("addressRegion", ""),
                "country": country,
                "location": _place(city, country),
                "work_mode": _mode(remote=bool(j.get("isRemote")),
                                   text=j.get("workplaceType", "")),
                "date": _date(j.get("publishedAt")),
            })
        return out

    def _smartrecruiters(self, slug: str) -> list[dict]:
        out, offset = [], 0
        while True:
            data = self._get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                             limit=100, offset=offset)
            for j in data["content"]:
                loc = j.get("location") or {}
                country = _country(loc.get("country", ""))
                city = loc.get("city", "")
                out.append({
                    "role": j["name"],
                    "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                    "studio": (j.get("company") or {}).get("name", ""),
                    "city": city, "region": loc.get("region", ""), "country": country,
                    "location": _place(city, country),
                    "work_mode": _mode(remote=bool(loc.get("remote")),
                                       hybrid=bool(loc.get("hybrid"))),
                    "experience": (j.get("experienceLevel") or {}).get("label", ""),
                    "date": _date(j.get("releasedDate")),
                })
            offset += len(data["content"])
            if not data["content"] or offset >= data.get("totalFound", 0) or offset >= 2000:
                return out

    def _workable(self, slug: str) -> list[dict]:
        data = self._get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
        out = []
        for j in data["jobs"]:
            city, country = j.get("city", ""), _country(j.get("country", ""))
            out.append({
                "role": j["title"], "url": j["url"],
                "studio": data.get("name", ""),
                "city": city, "region": j.get("state", ""), "country": country,
                "location": _place(city, country),
                "work_mode": _mode(remote=bool(j.get("telecommuting"))),
                "experience": j.get("experience", ""),
                "date": _date(j.get("published_on") or j.get("created_at")),
            })
        return out
