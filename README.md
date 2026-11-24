# job-tracker

Pulls job postings from configured sources (currently the community
VFX/animation job sheet) into the Notion Job Tracker.

Each run reads existing Posting URL + Role pairs back from Notion and skips
them, including Not Interested rows. A small local store (`data/seen.db`)
also remembers every posting already handled, so a row you rename or delete
in Notion is not added again.

New postings are then scored 0-10 by Claude against a plain-English profile
in `config.toml`. Good fits go into Notion with Fit Score, Discipline and Fit
Reason; poor fits are recorded locally and never scored again.

## Layout

    job_tracker/
      __main__.py         fetch -> filter -> seen store -> AI score -> Notion
      config.py           loads and checks config.toml
      filters.py          location / keyword / link / age filters (all sources)
      scoring.py          Claude fit scoring against the [scoring] profile
      notion.py           Notion API client, with pacing and retries
      store.py            SQLite seen-postings store (with scores)
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
4. For AI scoring: create an API key at platform.claude.com and add it to
   `.env` as `ANTHROPIC_API_KEY`. Then add three columns to the Notion
   database: **Fit Score** (Number), **Discipline** (Select) and
   **Fit Reason** (Text). The run stops with a message if any are missing.

## Run

    docker compose build
    docker compose run --rm job-tracker

Dry run, no Notion or seen-store writes and no Notion token needed:

    docker compose run --rm job-tracker --dry-run --no-score

A dry run with scoring shows each posting's score and reason, best first,
which is how to tune the profile. It does call the API, so cap it:

    docker compose run --rm job-tracker --dry-run --limit 20

`--limit N` also works on a real run, e.g. for a careful first run.

Locally, without Docker:

    pip install -r requirements.txt -r requirements-dev.txt
    python -m job_tracker --dry-run --no-score
    python -m pytest

## AI scoring

Configured under `[scoring]` in `config.toml`:

- `profile`: what you're looking for, written like a recruiter brief. This
  is the main thing to edit.
- `min_score`: postings below this (default 6) are not added to Notion.
- `model` / `effort`: `claude-opus-5-5` at `low` by default. Each posting is
  scored once, so the cost is mostly the first run (a couple of hundred
  postings) and then a handful a day. `claude-haiku-5-5` is much cheaper and
  a weaker judge.
- `disciplines`: the options for the Discipline column.

The title keywords still decide what gets scored, so they can be broader
than before: the profile decides what is kept. If the API is down or rate
limited, postings that passed the keywords are added without a score.
Rejected postings are in `data/seen.db` (`status = 'rejected'`) with their
score and reason, if you want to check what is being filtered out:

    sqlite3 data/seen.db "select score, title, studio, reason from seen where status='rejected' order by score desc"

## Daily schedule (Pi crontab)

    0 7 * * * cd /home/<user>/job-tracker && /usr/bin/docker compose run --rm job-tracker >> /var/log/job-tracker.log 2>&1

## Configuration

Everything lives in `config.toml`: sources, title keywords and exclusions,
software keywords, allowed locations, remote handling, maximum age, sheet
column names and Notion column names. Docker mounts the file, so edits take
effect on the next run without rebuilding. A typo or unknown key stops the
run with a message naming the key.

Only a code change (`job_tracker/`, `requirements.txt`) needs
`docker compose build`.

## Clearing out Not Interested rows

    docker compose run --rm job-tracker --prune --dry-run   # list them
    docker compose run --rm job-tracker --prune

Records every row whose Interest is Not Interested in `data/seen.db`, then
moves it to the Notion trash (restorable there for 30 days). Because the
posting is in the seen store, the sync never adds it again. Rows with a
Status set (Applied, Interviewing...) are always kept. Run it on the machine
that does the daily sync, so it writes to that machine's `data/seen.db`.

## Adding a source

Write a class in `job_tracker/sources/` with a `name`, a
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
