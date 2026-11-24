"""Sync job sources into the Notion Job Tracker.

    python -m job_tracker [--config config.toml] [--dry-run] [--no-score]
                          [--limit N] [--rescore-rejected] [--prune]

Pipeline: trash Not Interested rows -> fetch sources -> hard filters ->
skip postings seen before -> trash rows whose posting has closed ->
fetch descriptions -> AI fit score -> Notion -> run summary in Notion.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Config, load_config
from .filters import apply_filters
from .models import Fatal, Posting
from .notion import Notion
from .sources import build_sources
from .store import SeenStore
from .text import normalise_studio, role_core


@dataclass
class Report:
    """What a sync did, for the console and the Notion run log."""
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pruned: int = 0
    closed: int = 0
    present: int = 0
    updated: int = 0
    scored: int = 0
    rejected: int = 0
    unscored: int = 0
    added: list[tuple[int | None, Posting]] = field(default_factory=list)
    cost: float | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def warn(self, message: str) -> None:
        print(f"  warning: {message}")
        self.warnings.append(message)

    @property
    def result(self) -> str:
        return "Failed" if self.error else ("Warnings" if self.warnings else "OK")

    def best(self, n: int = 5) -> list[str]:
        ranked = sorted(self.added, key=lambda sp: -(sp[0] if sp[0] is not None else -1))
        return [f"[{s if s is not None else '-'}] {p.studio}: {p.role}"
                for s, p in ranked[:n]]

    def done_line(self, cfg: Config) -> str:
        text = (f"Done. {len(self.added)} added, {self.updated} existing rows filled "
                f"in, {self.present} already in Notion")
        if self.scored or self.rejected:
            text += f", {self.rejected} scored below {cfg.scoring.min_score}"
        if self.closed:
            text += f", {self.closed} closed postings removed"
        if self.cost is not None:
            text += f", about ${self.cost:.2f} of API use"
        return text + "."


def collect(cfg: Config, report: Report) -> tuple[list[Posting], list[Posting], list]:
    """Fetch every source. Returns (postings that pass the filters, everything
    listed, the source objects)."""
    sources = build_sources(cfg.sources)
    listed: list[Posting] = []
    for source in sources:
        print(f"Fetching {source.name}...")
        found = source.fetch()
        print(f"  {len(found)} rows")
        for board in getattr(source, "failed_boards", []):
            report.warnings.append(f"could not read board {board}")
        listed.extend(found)

    matched, stats = apply_filters(listed, cfg.filters)
    for reason, count in sorted(stats.items()):
        print(f"  dropped {count}: {reason}")
    print(f"  {len(matched)} postings match the filters")
    return matched, listed, sources


def make_scorer(cfg: Config, enabled: bool):
    if not (enabled and cfg.scoring.enabled):
        return None
    from .scoring import Scorer  # only import the SDK when scoring is on
    return Scorer(cfg.scoring)


def line(p: Posting, score=None) -> str:
    head = f"[{score.score:>2}] " if score else ""
    tail = f"\n        {score.discipline}: {score.reason}" if score else ""
    return (f"  - {head}{p.studio} | {p.role} | {p.location} | "
            f"{p.experience or '-'} | {p.work_mode or '-'} | "
            f"{p.date_issued or 'NO DATE'} | {p.url}{tail}")


def dry_run(cfg: Config, fresh: list[Posting], scorer) -> int:
    scores = scorer.score_all(fresh) if scorer else {}

    def order(p: Posting):
        s = scores.get(p.key)
        return (-(s.score if s else 11), p.studio.lower(), p.role.lower())

    for p in sorted(fresh, key=order):
        print(line(p, scores.get(p.key)))
    if scorer:
        keep = sum(1 for s in scores.values() if s is None or s.score >= cfg.scoring.min_score)
        print(f"Dry run: {keep} of {len(fresh)} would be added "
              f"(min_score {cfg.scoring.min_score}). Nothing written.")
    else:
        print(f"Dry run: {len(fresh)} would be checked against Notion. Nothing written.")
    return 0


def prune(cfg: Config, notion: Notion, dry_run: bool) -> int:
    """Record every Not Interested row in the seen store, then trash it, so
    the tracker stays tidy and the posting is never added again.
    Returns how many rows were (or, in a dry run, would be) removed."""
    pages = notion.pages_with_select("interest", cfg.not_interested)
    keep = [pg for pg in pages if pg["status"]]
    pages = [pg for pg in pages if not pg["status"]]
    print(f"{len(pages)} rows marked '{cfg.not_interested}' to record and trash")
    if keep:
        print(f"  keeping {len(keep)} that have a Status set (you applied or "
              "were in touch), e.g. " + ", ".join(pg["role"] for pg in keep[:3]))
    if dry_run:
        for pg in pages:
            print(f"  - {pg['studio']} | {pg['role']} | {pg['url']}")
        print("Dry run: nothing recorded or trashed.")
        return len(pages)

    store = SeenStore(cfg.db_path)
    try:
        for i, pg in enumerate(pages, 1):
            posting = Posting(role=pg["role"], studio=pg["studio"],
                              source=pg["source"] or "Notion", url=pg["url"])
            # Record first, so a crash part-way never loses a dismissal.
            store.add(posting, pg["id"], status="dismissed")
            notion.trash(pg["id"])
            if i % 50 == 0:
                print(f"  {i} done")
    finally:
        store.close()
    print(f"Done. {len(pages)} recorded in {cfg.db_path} and moved to the "
          "Notion trash (restorable there for 30 days).")
    return len(pages)


def close_stale(cfg: Config, notion: Notion, store: SeenStore, existing: dict,
                listed: list[Posting], sources: list, report: Report) -> None:
    """Trash untouched rows whose posting is no longer listed anywhere.

    Only rows with no Interest and no Status, added by a source that was read
    in full this run, are considered: if a board failed to load, its jobs
    would all look closed. As a second guard, nothing is closed if more than
    a quarter of the candidate rows would be (a source that changed shape).
    """
    names = {s.name for s in sources}
    complete = {s.name for s in sources if getattr(s, "complete", True)}
    listed_keys = {p.key for p in listed}
    listed_twins = {p.studio_role for p in listed}
    candidates = [(key, pg) for key, pg in existing.items()
                  if not pg.get("interest") and not pg.get("status")
                  and pg.get("source") in names]
    stale = [(key, pg) for key, pg in candidates
             if pg.get("source") in complete and key not in listed_keys
             and (normalise_studio(pg.get("studio", "")), role_core(pg.get("role", "")))
             not in listed_twins]
    if not stale:
        return
    if len(stale) > max(5, len(candidates) // 4):
        report.warn(f"{len(stale)} of {len(candidates)} untouched rows look closed; "
                    "too many to trust, so none were removed")
        return
    for key, pg in stale:
        posting = Posting(role=pg.get("role", ""), studio=pg.get("studio", ""),
                          source=pg.get("source", ""), url=key[0])
        store.add(posting, pg["id"], status="closed")
        notion.trash(pg["id"])
        existing.pop(key, None)
        report.closed += 1
        print(f"  - closed: {pg.get('studio')} | {pg.get('role')}")


def sync(cfg: Config, args: argparse.Namespace, notion: Notion | None,
         scorer, report: Report) -> int:
    if cfg.auto_prune and notion and not args.dry_run:
        # Before the sync, so dismissed postings are already in the seen store.
        report.pruned = prune(cfg, notion, dry_run=False)

    postings, listed, sources = collect(cfg, report)
    fingerprint = None
    if scorer:
        from .scoring import fingerprint as scoring_fingerprint
        fingerprint = scoring_fingerprint(cfg.scoring)

    # A dry run reads the store if there is one but never creates it.
    if args.dry_run and not os.path.exists(cfg.db_path):
        store = SeenStore(":memory:")
    else:
        store = SeenStore(cfg.db_path)
    try:
        rescore = bool(scorer) and (
            args.rescore_rejected
            or (cfg.scoring.rescore_on_profile_change
                and store.get_meta("scoring_fingerprint") != fingerprint))
        if rescore:
            print("  scoring settings changed (or --rescore-rejected): postings "
                  "rejected before are scored again")
        fresh = [p for p in postings if not store.has(p, skip_rejected=rescore)]
        print(f"  {len(postings) - len(fresh)} already seen in earlier runs "
              f"({len(store)} in {cfg.db_path})")

        if args.dry_run:
            if args.limit:
                fresh = fresh[:args.limit]
            describe(sources, fresh)
            return dry_run(cfg, fresh, scorer)

        assert notion is not None
        if scorer:
            notion.check_columns({"fit_score": "number", "discipline": "select",
                                  "fit_reason": "rich_text"})
        existing = notion.existing_pages()
        print(f"  {len(existing)} rows already in Notion")
        if cfg.prune_closed:
            close_stale(cfg, notion, store, existing, listed, sources, report)
        by_studio_role = {
            (normalise_studio(pg.get("studio", "")), role_core(pg.get("role", ""))): pg
            for pg in existing.values() if pg.get("studio") and pg.get("role")
        }

        # Rows already in Notion: fill blanks and record them, so the store
        # is populated from Notion on the first run. They are not scored.
        new: list[Posting] = []
        for posting in postings:
            page = existing.get(posting.key)
            twin = by_studio_role.get(posting.studio_role)
            if not page and twin and twin.get("source") != posting.source:
                # Same job already added from another source under another link.
                store.add(posting, twin["id"], status="present")
                report.present += 1
                continue
            if page:
                if notion.fill_blanks(page, posting):
                    report.updated += 1
                store.add(posting, page["id"], status="present")
                report.present += 1
            elif not store.has(posting, skip_rejected=rescore):
                new.append(posting)
        if args.limit:
            new = new[:args.limit]

        describe(sources, new)
        scores = scorer.score_all(new) if scorer else {}
        report.scored = sum(1 for s in scores.values() if s is not None)
        for posting in new:
            score = scores.get(posting.key)
            if score and score.score < cfg.scoring.min_score:
                store.add(posting, status="rejected", score=score)
                report.rejected += 1
                continue
            if scorer and score is None:
                report.unscored += 1  # API trouble: fall back to the keyword match
            page_id = notion.create(posting, score)
            store.add(posting, page_id, status="added", score=score)
            report.added.append((score.score if score else None, posting))
            print(line(posting, score).replace("  - ", "  + ", 1))

        if scorer:
            report.cost = scorer.cost
            store.set_meta("scoring_fingerprint", fingerprint)
        if report.unscored:
            report.warn(f"{report.unscored} added without a score after API errors")
        print(report.done_line(cfg))
        return 0
    finally:
        store.close()


def describe(sources: list, postings: list[Posting]) -> None:
    """Let each source fetch descriptions its listing didn't include."""
    for source in sources:
        fill = getattr(source, "fill_descriptions", None)
        if fill:
            fill(postings)


def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if (args.prune or not args.dry_run) and not (token and database_id):
        raise Fatal("NOTION_TOKEN and NOTION_DATABASE_ID must be set")
    notion = Notion(token, database_id, cfg.notion_properties) if token and database_id else None
    if args.prune:
        prune(cfg, notion, args.dry_run)
        return 0

    report = Report()
    try:
        scorer = make_scorer(cfg, not args.no_score)
        return sync(cfg, args, notion, scorer, report)
    except Exception as exc:
        report.error = str(exc) if isinstance(exc, Fatal) else f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if notion and cfg.runs_database_id and not args.dry_run:
            try:
                notion.log_run(cfg.runs_database_id, report)
            except Exception as exc:  # never hide the run's own result
                print(f"  warning: could not write the run summary to Notion: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="job-tracker")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show new matches without writing to Notion or the seen store "
             "(still scores them if scoring is on, which costs API calls)",
    )
    parser.add_argument("--no-score", action="store_true",
                        help="skip AI scoring for this run")
    parser.add_argument("--limit", type=int, default=0,
                        help="only process the first N new postings")
    parser.add_argument(
        "--rescore-rejected", action="store_true",
        help="score again postings that scored below min_score before (this "
             "happens automatically when the [scoring] settings change)",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help="instead of syncing: record every Not Interested row in the seen "
             "store so it is never re-added, then move it to the Notion trash "
             "(with --dry-run, only list them)",
    )
    try:
        return run(parser.parse_args())
    except Fatal as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
