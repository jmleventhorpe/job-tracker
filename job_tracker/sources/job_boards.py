"""Studios' own job boards, read through their hiring systems' public feeds.

Supported sites (all free, no key needed):
  greenhouse       boards-api.greenhouse.io
  lever            api.lever.co
  ashby            api.ashbyhq.com
  smartrecruiters  api.smartrecruiters.com
  workable         apply.workable.com
  recruitee        <slug>.recruitee.com/api
  bamboohr         <slug>.bamboohr.com/careers
  jobvite          jobs.jobvite.com/<slug> (the public careers page)

Each board is the studio's slug on that site: the part of its job-page URL
that names the studio, e.g. job-boards.greenhouse.io/sonypicturesimageworks.
One board that cannot be read is reported and skipped; the rest still run.

Job descriptions come with the listing where the feed includes them. For the
rest, fill_descriptions() fetches them, and is only called for postings that
passed the filters and have not been seen before.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from ..models import Fatal, Posting
from ..text import select_value

DESCRIPTION_CHARS = 4000  # enough for the scorer; keeps token use down

# ISO 3166 alpha-2 -> name, for feeds that only give a country code.
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
    """ISO string, 'yyyy-mm-dd hh:mm:ss UTC' or epoch ms -> yyyy-mm-dd."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, timezone.utc).date().isoformat()
        text = str(value).replace(" UTC", "+00:00").replace("Z", "+00:00")
        return datetime.fromisoformat(text).date().isoformat()
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


def html_to_text(raw: str, limit: int = DESCRIPTION_CHARS) -> str:
    """Job ad HTML (possibly entity-escaped, as Greenhouse sends it) -> plain text."""
    text = html.unescape(raw or "")
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|li|h\d|div)>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    lines = (line.strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)[:limit]


class JobBoardsSource:
    SITES = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable",
             "recruitee", "bamboohr", "jobvite")
    BOARD_KEYS = {"site", "slug", "studio", "country"}

    def __init__(self, name: str, boards: list[dict[str, str]]):
        self.name = name
        self.boards = boards
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "job-tracker"
        self.failed_boards: list[str] = []

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
            extra = sorted(set(b) - cls.BOARD_KEYS)
            if extra:
                raise Fatal(f"config: job_boards board #{i} has unknown keys {extra}")
            if b["site"] not in cls.SITES:
                raise Fatal(f"config: job_boards board #{i} site '{b['site']}' must "
                            f"be one of {', '.join(cls.SITES)}")
        return cls(name=cfg.get("name", "Studio Boards"), boards=boards)

    @property
    def complete(self) -> bool:
        """True when every board was read this run (closing stale rows relies on it)."""
        return not self.failed_boards

    # -- fetching ----------------------------------------------------------

    def _get(self, url: str, **params) -> Any:
        resp = self.session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _get_text(self, url: str) -> str:
        resp = self.session.get(url, timeout=60)
        resp.raise_for_status()
        return resp.text

    def fetch(self) -> list[Posting]:
        self.failed_boards = []
        postings: list[Posting] = []
        for board in self.boards:
            label = f"{board['site']}:{board['slug']}"
            try:
                found = self.read_board(board)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                print(f"  warning: could not read board {label}: {exc}")
                self.failed_boards.append(label)
                continue
            postings.extend(found)
        return postings

    def read_board(self, board: dict[str, str]) -> list[Posting]:
        reader: Callable[[str], list[dict]] = getattr(self, f"_{board['site']}")
        studio_override = board.get("studio", "")
        country_fallback = board.get("country", "")
        postings = []
        for raw in reader(board["slug"]):
            # Feeds use null for missing fields; treat every one as "".
            r = {k: ("" if v is None else v) for k, v in raw.items()}
            if not r.get("role") or not r.get("url"):
                continue
            country = r.get("country", "") or country_fallback
            location = r.get("location", "")
            if country_fallback and not r.get("country"):
                location = _place(location, country_fallback)
            postings.append(Posting(
                role=str(r["role"]).strip(),
                studio=studio_override or r.get("studio") or board["slug"],
                source=self.name,
                url=r["url"],
                city=r.get("city", ""),
                region=r.get("region", ""),
                country=country,
                work_mode=select_value(r.get("work_mode", "")),
                experience=select_value(r.get("experience", "")),
                date_issued=r.get("date") or None,
                locations=[location] if location else [],
                still_open=True,  # boards only list open jobs
                description=html_to_text(r.get("description", "")),
                detail=r.get("detail", ""),
            ))
        return postings

    def fill_descriptions(self, postings: list[Posting]) -> None:
        """Fetch descriptions the listing didn't include (one request each)."""
        for p in postings:
            if p.source != self.name or p.description or not p.detail:
                continue
            try:
                p.description = self._describe(p.detail)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                print(f"  warning: no description for '{p.role}': {exc}")

    def _describe(self, detail: str) -> str:
        kind, _, target = detail.partition(":")
        if kind == "smartrecruiters":
            sections = (self._get(target).get("jobAd") or {}).get("sections") or {}
            parts = [(sections.get(k) or {}).get("text", "")
                     for k in ("jobDescription", "qualifications")]
            return html_to_text("\n".join(parts))
        if kind == "bamboohr":
            job = (self._get(target).get("result") or {}).get("jobOpening") or {}
            return html_to_text(job.get("description", ""))
        if kind == "jobvite":
            page = self._get_text(target)
            m = re.search(r'class="jv-job-detail-description"[^>]*>(.*?)'
                          r'<div class="jv-job-detail-bottom-actions', page, re.S)
            return html_to_text(m.group(1) if m else "")
        return ""

    # -- one reader per site, each returning plain dicts -------------------

    def _greenhouse(self, slug: str) -> list[dict]:
        data = self._get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                         content="true")
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
                # open for years.
                "date": _date(j.get("updated_at") or j.get("first_published")),
                "description": j.get("content", ""),
            })
        return out

    def _lever(self, slug: str) -> list[dict]:
        data = self._get(f"https://api.lever.co/v0/postings/{slug}", mode="json")
        out = []
        for j in data:
            cats = j.get("categories") or {}
            city = cats.get("location", "")
            country = _country(j.get("country", ""))
            lists = "\n".join(f"{x.get('text', '')}\n{x.get('content', '')}"
                              for x in j.get("lists") or [])
            out.append({
                "role": j["text"], "url": j["hostedUrl"],
                "city": city, "country": country, "location": _place(city, country),
                "work_mode": _mode(text=j.get("workplaceType", "")),
                "date": _date(j.get("createdAt")),
                "description": (j.get("descriptionPlain") or "") + "\n" + lists,
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
                "country": country, "location": _place(city, country),
                "work_mode": _mode(remote=bool(j.get("isRemote")),
                                   text=j.get("workplaceType", "")),
                "date": _date(j.get("publishedAt")),
                "description": j.get("descriptionPlain") or j.get("descriptionHtml", ""),
            })
        return out

    def _smartrecruiters(self, slug: str) -> list[dict]:
        out, offset = [], 0
        base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        while True:
            data = self._get(base, limit=100, offset=offset)
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
                    "detail": f"smartrecruiters:{base}/{j['id']}",
                })
            offset += len(data["content"])
            if not data["content"] or offset >= data.get("totalFound", 0) or offset >= 2000:
                return out

    def _workable(self, slug: str) -> list[dict]:
        data = self._get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
                         details="true")
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
                "description": j.get("description", ""),
            })
        return out

    def _recruitee(self, slug: str) -> list[dict]:
        data = self._get(f"https://{slug}.recruitee.com/api/offers/")
        out = []
        for j in data["offers"]:
            city, country = j.get("city", ""), j.get("country") or _country(j.get("country_code", ""))
            out.append({
                "role": j["title"], "url": j["careers_url"],
                "studio": j.get("company_name", ""),
                "city": city, "region": j.get("state_name", ""), "country": country,
                "location": _place(city, country),  # its own field can say "Remote job"
                "work_mode": _mode(remote=bool(j.get("remote")), hybrid=bool(j.get("hybrid"))),
                "date": _date(j.get("published_at") or j.get("created_at")),
                "description": f"{j.get('description', '')}\n{j.get('requirements', '')}",
            })
        return out

    def _bamboohr(self, slug: str) -> list[dict]:
        data = self._get(f"https://{slug}.bamboohr.com/careers/list")
        out = []
        for j in data["result"]:
            loc = j.get("location") or {}
            ats = j.get("atsLocation") or {}
            city = loc.get("city") or ats.get("city") or ""
            country = ats.get("country") or ""
            out.append({
                "role": j["jobOpeningName"],
                "url": f"https://{slug}.bamboohr.com/careers/{j['id']}",
                "city": city, "region": loc.get("state") or ats.get("state") or "",
                "country": country, "location": _place(city, country),
                "work_mode": _mode(remote=bool(j.get("isRemote"))),
                "detail": f"bamboohr:https://{slug}.bamboohr.com/careers/{j['id']}/detail",
            })
        return out

    def _jobvite(self, slug: str) -> list[dict]:
        base = "https://jobs.jobvite.com"
        page = self._get_text(f"{base}/{slug}/jobs")
        items = re.findall(
            r'<a href="(/' + re.escape(slug) + r'/job/[^"]+)" class="jv-job-item[^"]*">\s*'
            r'<div class="jv-job-list-name"><p[^>]*>(.*?)</p></div>\s*'
            r'<div class="jv-job-list-location">(.*?)</div>\s*</a>', page, re.S)
        if not items and "jv-job-list" not in page:
            raise ValueError("careers page layout not recognised")
        out = []
        for path, name, loc_html in items:
            url = base + path
            loc = re.sub(r"\s+", " ", html_to_text(loc_html))
            loc = re.sub(r"\s*,\s*", ", ", loc)
            work_mode = _mode(text=loc)
            if re.search(r"\d+ Locations", loc):  # "2 Locations", "Hybrid Remote, 2 Locations"
                loc = self._jobvite_locations(url)
            out.append({
                "role": html_to_text(name), "url": url,
                "country": loc, "location": loc, "work_mode": work_mode,
                "detail": f"jobvite:{url}",
            })
        return out

    def _jobvite_locations(self, url: str) -> str:
        """'2 Locations' on the list: read them from the job page's meta line,
        which is 'Department | City, Country | City, Region'."""
        page = self._get_text(url)
        m = re.search(r'<p class="jv-job-detail-meta">(.*?)</p>', page, re.S)
        if not m:
            return ""
        parts = [html_to_text(p) for p in re.split(r"<span[^>]*jv-inline-separator[^>]*>\s*</span>",
                                                   m.group(1))]
        places = [p for p in parts[1:] if p]  # first part is the department
        return "; ".join(re.sub(r"\s*,\s*", ", ", p) for p in places)
