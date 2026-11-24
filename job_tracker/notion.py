"""Notion Job Tracker database: read existing rows, create and patch pages."""

from __future__ import annotations

import time

import requests

from .models import Fatal, Posting
from .text import normalise_role, normalise_url

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

MAX_ATTEMPTS = 5
MIN_INTERVAL = 0.35  # stay under Notion's ~3 requests/second limit


def _select_name(prop: dict | None) -> str:
    """Option name of a select or status property, '' if unset."""
    prop = prop or {}
    sel = prop.get("select") or prop.get("status")
    return (sel or {}).get("name", "") if sel else ""


def _rich_text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value[:2000]}}]}


class Notion:
    def __init__(self, token: str, database_id: str, properties: dict[str, str]):
        self.database_id = database_id
        self.props = properties
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, what: str, **kwargs) -> dict:
        """Call Notion, pacing requests and retrying 429s, 5xx and network errors."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = self.session.request(
                    method, f"{NOTION_API}{path}", timeout=60, **kwargs
                )
            except requests.RequestException as exc:
                if attempt == MAX_ATTEMPTS:
                    raise Fatal(f"Notion {what} failed: {exc}") from None
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_ATTEMPTS:
                    raise Fatal(f"Notion {what} failed ({resp.status_code}) after "
                                f"{MAX_ATTEMPTS} attempts: {resp.text}")
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2 ** attempt
                print(f"  Notion {resp.status_code}, retrying in {delay:.0f}s")
                time.sleep(delay)
                continue
            if resp.status_code == 404:
                raise Fatal(
                    "Notion returned 404. The token can't see the Job Tracker "
                    "database, or NOTION_DATABASE_ID is wrong."
                )
            if resp.status_code >= 400:
                raise Fatal(f"Notion {what} failed ({resp.status_code}): {resp.text}")
            return resp.json()
        raise AssertionError("unreachable")

    def check_columns(self, needed: dict[str, str]) -> None:
        """Stop early, naming what to add, if the database lacks a column.

        `needed` maps a key in self.props to the Notion property type."""
        data = self._request("GET", f"/databases/{self.database_id}", "schema read")
        have = {name: prop.get("type") for name, prop in data["properties"].items()}
        problems = []
        for key, kind in needed.items():
            name = self.props[key]
            if name not in have:
                problems.append(f"add a '{name}' column of type {kind}")
            elif have[name] != kind:
                problems.append(f"'{name}' should be type {kind}, not {have[name]}")
        if problems:
            raise Fatal(
                "Notion Job Tracker database needs changes: " + "; ".join(problems)
                + ". Or rename them under [notion.properties] in config.toml."
            )

    def existing_pages(self) -> dict[tuple[str, str], dict]:
        """Map (url, role) -> {'id', 'experience', 'work_mode'} for every row,
        including Not Interested ones."""
        p = self.props
        pages: dict[tuple[str, str], dict] = {}
        payload: dict = {"page_size": 100}
        while True:
            data = self._request(
                "POST", f"/databases/{self.database_id}/query", "query", json=payload
            )
            for page in data["results"]:
                props = page["properties"]
                url = (props.get(p["url"]) or {}).get("url") or ""
                title = (props.get(p["role"]) or {}).get("title") or []
                role = "".join(t.get("plain_text", "") for t in title)
                pages[(normalise_url(url), normalise_role(role))] = {
                    "id": page["id"],
                    "experience": _select_name(props.get(p["experience"])),
                    "work_mode": _select_name(props.get(p["work_mode"])),
                }
            if not data.get("has_more"):
                return pages
            payload["start_cursor"] = data["next_cursor"]

    def pages_with_select(self, key: str, value: str) -> list[dict]:
        """Every page whose select property `key` equals `value`, as
        {'id', 'role', 'url', 'studio', 'source', 'status'}."""
        p = self.props
        found: list[dict] = []
        payload: dict = {
            "page_size": 100,
            "filter": {"property": p[key], "select": {"equals": value}},
        }
        while True:
            data = self._request(
                "POST", f"/databases/{self.database_id}/query", "query", json=payload
            )
            for page in data["results"]:
                props = page["properties"]
                title = (props.get(p["role"]) or {}).get("title") or []
                studio = (props.get(p["studio"]) or {}).get("rich_text") or []
                found.append({
                    "id": page["id"],
                    "role": "".join(t.get("plain_text", "") for t in title),
                    "url": (props.get(p["url"]) or {}).get("url") or "",
                    "studio": "".join(t.get("plain_text", "") for t in studio),
                    "source": _select_name(props.get(p["source"])),
                    "status": _select_name(props.get(p["status"])),
                })
            if not data.get("has_more"):
                return found
            payload["start_cursor"] = data["next_cursor"]

    def trash(self, page_id: str) -> None:
        """Move a page to Notion's trash (restorable from there for 30 days)."""
        self._request("PATCH", f"/pages/{page_id}", "trash", json={"archived": True})

    def fill_blanks(self, page: dict, posting: Posting) -> bool:
        """Set Experience / Work Mode only where the Notion row is empty.
        Never touches anything else, so manual edits survive."""
        p = self.props
        props: dict = {}
        if not page["experience"] and posting.experience:
            props[p["experience"]] = {"select": {"name": posting.experience}}
        if not page["work_mode"] and posting.work_mode:
            props[p["work_mode"]] = {"select": {"name": posting.work_mode}}
        if not props:
            return False
        self._request("PATCH", f"/pages/{page['id']}", "update",
                      json={"properties": props})
        return True

    def create(self, posting: Posting, score=None) -> str:
        """Add a page. `score` is a scoring.Score or None."""
        p = self.props
        props: dict = {
            p["role"]: {"title": [{"text": {"content": posting.role[:2000]}}]},
            p["studio"]: _rich_text(posting.studio),
            p["source"]: {"select": {"name": posting.source}},
        }
        if posting.location:
            props[p["location"]] = _rich_text(posting.location)
        if posting.url:
            props[p["url"]] = {"url": posting.url[:2000]}
        if posting.date_issued:
            props[p["date_issued"]] = {"date": {"start": posting.date_issued}}
        if posting.experience:
            props[p["experience"]] = {"select": {"name": posting.experience}}
        if posting.work_mode:
            props[p["work_mode"]] = {"select": {"name": posting.work_mode}}
        if score:
            props[p["fit_score"]] = {"number": score.score}
            props[p["discipline"]] = {"select": {"name": score.discipline}}
            props[p["fit_reason"]] = _rich_text(score.reason)

        data = self._request(
            "POST", "/pages", "create",
            json={"parent": {"database_id": self.database_id}, "properties": props},
        )
        return data["id"]
