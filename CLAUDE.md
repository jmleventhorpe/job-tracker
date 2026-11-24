# job-tracker

Daily job-posting sync into James's Notion Job Tracker. See README.md for how
it works and the config reference.

## Rules

- A push to `main` is a live deploy: the Pi's `jobtracker` container downloads
  `main` at the start of every run (daily 07:00 Africa/Johannesburg, and on
  restart). Run `.venv/bin/python -m pytest` before pushing, and ask James
  before pushing.
- Never commit `.env` or `data/seen.db`. `.env.example` holds placeholders only.
- Live API calls cost money (Opus 5.5, about $0.01 per posting scored). Dry
  runs with scoring on still call the API; use `--no-score` or `--limit`.
- James edits `config.toml` himself; when a note says the file changed on
  disk, keep his version.
- The Pi side (container, schedule, volume, env file) lives in
  `../pi-docker/docker-compose.yml`; that repo has its own rules (diff and
  go-ahead before every push).

## Working with the live system

- Run now: arr-mcp `restart_container jobtracker`; then
  `container_logs jobtracker` (allow about 2 minutes).
- Notion: Job Tracker database `1906968c4aa442e8817c84e3ccddb732`
  (data source `collection://e13e1517-4b80-43b8-b214-72d99db653f2`); run log
  **Job Tracker Runs** `4227e6dea3f081f7b148ddaa4b94a6f7`. The Notion MCP
  works as James; the script works through its own integration, which must be
  connected to each database it uses.
- Local `.venv` (uv, Python 3.13) has the dependencies. `.env` locally has
  the Anthropic key but a placeholder Notion token, so real (non-dry) runs
  only work on the Pi.

## Design decisions

- Profile-based scoring, not keyword lists: keywords stay broad and only
  decide what is worth scoring; `title_exclusions` drop FX/CFX/crowd first.
- Location preference is in the profile as score adjustments (UK +1;
  Europe/Australia/NZ 0; Canada -1; US/Mexico/South America/Asia -2), not
  in the filter. Lookdev is a second choice (-2); technical artist and
  technical animator roles are not a fit.
- Seen store statuses: added, present, rejected, dismissed, closed. Only
  rejected rows are ever rescored (automatically when the scoring
  fingerprint changes, or with `--rescore-rejected`).
- Cross-source de-dup uses (normalised studio, title core). It is only
  applied between different sources: the same source can list two jobs with
  the same title.
- Board postings set `still_open` and skip the age filter.
- Closed-posting clean-up only touches rows with no Interest and no Status,
  from sources read in full that run, and refuses if over a quarter would go.

## Not covered yet

- ILM, Walt Disney Animation, Pixar (Disney careers site, Radancy, not
  Workday), DreamWorks (inside NBCUniversal's SmartRecruiters board, no studio
  filter), Blue Zoo, Brown Bag, Netflix Animation: all reach the sheet.
- No alert outside Notion when a run fails before it can write the run log
  (for example the container cannot download the code).
