# job-tracker

Pulls VFX/animation job postings from the community job sheet and studios'
own job boards into the Notion **Job Tracker**, scores each new one 0-10
against a plain-English profile with Claude, and keeps the tracker tidy.

Each daily run:

1. Trashes rows you marked **Not Interested** (recorded first, so they never
   come back).
2. Fetches every source in `config.toml` and applies the hard filters
   (title keywords and exclusions, allowed locations, age).
3. Skips postings already handled (`data/seen.db`, plus what is in Notion).
4. Trashes untouched rows whose posting is no longer listed anywhere.
5. Fetches job descriptions for the new postings, scores them, and adds the
   ones at or above `min_score` with Fit Score, Discipline and Fit Reason.
6. Writes a summary row to the **Job Tracker Runs** database.

## Where it runs

On the Orange Pi, as the `jobtracker` container defined in the
[pi-docker](../pi-docker) repo (`docker-compose.yml`, config
`jobtracker_runner_py`). It runs once when the container starts, then daily
at 07:00 Johannesburg time. Each run downloads this repo's `main` from
GitHub, so **a push to `main` is a deploy**: code and `config.toml` changes
apply from the next run, with no rebuild. The seen store lives on the
`jobtracker-data` volume. Secrets (`NOTION_TOKEN`, `NOTION_DATABASE_ID`,
`ANTHROPIC_API_KEY`) are in `~/docker/jobtracker/.env` on the Pi only.

- Run now: restart the container (arr-mcp `restart_container jobtracker`).
- Logs: arr-mcp `container_logs jobtracker`, or the Job Tracker Runs database.

## Layout

    job_tracker/
      __main__.py         the run: prune, fetch, filter, close, score, add, log
      config.py           loads and checks config.toml
      filters.py          keyword / location / link / age filters, cross-source de-dup
      scoring.py          Claude fit scoring against the [scoring] profile
      notion.py           Notion API client (pacing, retries, run log)
      store.py            SQLite seen store: every posting handled, with status and score
      text.py             matching, URL, title and studio normalising
      sources/
        __init__.py       Source interface + SOURCE_TYPES registry
        google_sheet.py   the community sheet
        job_boards.py     studio job boards (8 hiring systems)
    tests/

## Local use

    uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
    .venv/bin/python -m pytest
    .venv/bin/python -m job_tracker --dry-run --no-score

A dry run writes nothing (Notion, seen store, run log). With scoring on it
calls the API, so cap it while tuning the profile:

    set -a; . ./.env; set +a
    .venv/bin/python -m job_tracker --dry-run --limit 20

Other flags: `--no-score`, `--limit N`, `--rescore-rejected`, `--prune`
(only the Not Interested clean-up; add `--dry-run` to list).

## Configuration (`config.toml`)

A typo or unknown key stops the run with a message naming it.

- `[[sources]]`: the sheet, and the `job_boards` list (below).
- `[filters]`: `title_keywords` decide what gets scored; `title_exclusions`
  drop FX/CFX/crowd and similar first. Keep the keywords broad: the profile
  does the judging.
- `[locations] allowed`: countries and regions that pass at all.
- `[scoring]`: `profile` (the recruiter brief: discipline fit, seniority,
  second choices, location tiers), `min_score`, `model`, `effort`,
  `disciplines`. When any of these change, postings rejected before are
  scored again on the next run (`rescore_on_profile_change`).
- `[notion]`: `auto_prune` (Not Interested clean-up), `prune_closed`
  (closed-posting clean-up), `runs_database_id` (run log; empty to turn off),
  and column names under `[notion.properties]` if you rename one.

## Studio job boards

The `job_boards` source reads studios' boards through public feeds. To add a
studio, find a job link on its careers page and take the board name from it:

| Job link looks like | site | slug |
|---|---|---|
| `job-boards.greenhouse.io/sonypicturesimageworks/...` | greenhouse | sonypicturesimageworks |
| `jobs.lever.co/skydance/...` | lever | skydance |
| `jobs.ashbyhq.com/glitch/...` | ashby | glitch |
| `jobs.smartrecruiters.com/rodeofx/...` | smartrecruiters | rodeofx |
| `apply.workable.com/one-of-us/...` | workable | one-of-us |
| `framestore.recruitee.com/o/...` | recruitee | framestore |
| `cinesitelondon.bamboohr.com/careers/...` | bamboohr | cinesitelondon |
| `jobs.jobvite.com/double-negative-visual-effects/...` | jobvite | double-negative-visual-effects |

Add `{ site = "...", slug = "...", studio = "..." }` to `boards`. BambooHR
gives no country, so also set `country = "..."` there.

- Board postings skip the age filter: a board only lists open jobs, and
  studios keep evergreen roles open for years.
- A job on both the sheet and a board (same studio, same title ignoring
  senior/freelance/contract and anything in brackets) is added once.
- A board that fails to load is a warning in the run log; its rows are not
  closed that run.

## Run log

Each run adds a row to **Job Tracker Runs** in Notion: Result (OK, Warnings,
Failed), Added, Scored, Rejected, Pruned, Closed, Cost (estimated API spend)
and Notes (errors, warnings, best new postings). The Notion integration has
to be connected to that database (`...` -> Connections). A run that fails
still writes its row when Notion is reachable; if the container cannot even
download the code, only `container_logs` shows it.

## Checking what was filtered out

    sqlite3 data/seen.db "select score, title, studio, reason from seen where status='rejected' order by score desc"

Statuses: `added`, `present` (already in Notion), `rejected` (below
`min_score`), `dismissed` (Not Interested), `closed` (no longer listed).

## Failure modes

- The sheet stops being public or changes its header row: the run fails with
  a message (Failed in the run log). Fix headers with `[sources.columns]`.
- Notion rate limits and server errors are retried with backoff; a run that
  still fails part-way is picked up by the next one.
- If Claude is unavailable, postings that passed the keywords are added
  without a score (a warning in the run log).
- Closed-posting clean-up refuses to remove more than a quarter of the
  untouched rows at once, in case a source changed shape.
- The seen store is not in git and has no backup. Losing it is recoverable:
  the next run rebuilds it from Notion, but postings you dismissed more than
  30 days earlier (gone from the Notion trash) could come back once.
