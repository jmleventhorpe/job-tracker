# JobTracker

Pulls job postings from configured sources (currently the community
VFX/animation job sheet) into the Notion Job Tracker.

Each run reads existing Posting URL + Role pairs back from Notion and skips
them, including Not Interested rows. A small local store (`data/seen.db`)
also remembers every posting already handled, so a row you rename or delete
in Notion is not added again.

## Layout

    jobtracker/
      __main__.py         entry point: fetch -> filter -> seen store -> Notion
      config.py           loads and checks config.toml
      filters.py          location / keyword / link / age filters (all sources)
      notion.py           Notion API client, with pacing and retries
      store.py            SQLite seen-postings store
      text.py             matching, URL and date helpers
      sources/
        __init__.py       Source interface + SOURCE_TYPES registry
        google_sheet.py   public Google Sheet source
    tests/

## Setup

1. Create an internal integration at notion.so/profile/integrations and copy
   the token.
2. In Notion, open the Job Tracker database, then `...` -> Connections ->
   add the integration. Without this the API returns nothing.
3. `cp .env.example .env` and fill in the token.

## Run

    docker compose build
    docker compose run --rm jobtracker

Dry run, no Notion or seen-store writes and no token needed:

    docker compose run --rm jobtracker --dry-run

Locally, without Docker:

    pip install -r requirements.txt -r requirements-dev.txt
    python -m jobtracker --dry-run
    python -m pytest

## Daily schedule (Pi crontab)

    0 7 * * * cd /home/<user>/jobtracker && /usr/bin/docker compose run --rm jobtracker >> /var/log/jobtracker.log 2>&1

## Configuration

Everything lives in `config.toml`: sources, title keywords and exclusions,
software keywords, allowed locations, remote handling, maximum age, sheet
column names and Notion column names. Docker mounts the file, so edits take
effect on the next run without rebuilding. A typo or unknown key stops the
run with a message naming the key.

Only a code change (`jobtracker/`, `requirements.txt`) needs
`docker compose build`.

## Adding a source

Write a class in `jobtracker/sources/` with a `name`, a
`from_config(cls, cfg)` classmethod and `fetch() -> list[Posting]`, register
it in `SOURCE_TYPES`, then add a `[[sources]]` table with that `type` to
`config.toml`. Filtering, de-duplication and Notion writes are shared.

## Failure modes

- A sheet source refuses to run if its header row no longer contains the
  expected column names, rather than mapping the wrong fields. Fix it with
  `[sources.columns]` in `config.toml`.
- If the CSV export stops being public, it exits with an error. The fallback
  is the Sheets API with an API key.
- Notion rate limits (429) and server errors are retried with backoff. If a
  run still fails partway, the next run picks up where it left off.
- Deleting `data/seen.db` is safe: the next run rebuilds it from Notion, but
  rows you deleted from Notion may come back once.
