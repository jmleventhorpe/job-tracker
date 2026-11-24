"""Sync job sources into the Notion Job Tracker.

    python -m job_tracker [--config config.toml] [--dry-run] [--no-score] [--limit N]

Pipeline: fetch sources -> hard filters -> skip postings seen before ->
AI fit score (optional) -> Notion.
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import Config, load_config
from .filters import apply_filters
from .models import Fatal, Posting
from .notion import Notion
from .sources import build_sources
from .store import SeenStore


def collect(cfg: Config) -> list[Posting]:
    postings: list[Posting] = []
    for source in build_sources(cfg.sources):
        print(f"Fetching {source.name}...")
        found = source.fetch()
        print(f"  {len(found)} rows")
        postings.extend(found)

    matched, stats = apply_filters(postings, cfg.filters)
    for reason, count in sorted(stats.items()):
        print(f"  dropped {count}: {reason}")
    print(f"  {len(matched)} postings match the filters")
    return matched


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
    the tracker stays tidy and the posting is never added again."""
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
        return 0

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
    return 0


def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if (args.prune or not args.dry_run) and not (token and database_id):
        raise Fatal("NOTION_TOKEN and NOTION_DATABASE_ID must be set")
    if args.prune:
        return prune(cfg, Notion(token, database_id, cfg.notion_properties),
                     args.dry_run)
    scorer = make_scorer(cfg, not args.no_score)

    postings = collect(cfg)
    # A dry run reads the store if there is one but never creates it.
    if args.dry_run and not os.path.exists(cfg.db_path):
        store = SeenStore(":memory:")
    else:
        store = SeenStore(cfg.db_path)
    try:
        fresh = [p for p in postings if not store.has(p)]
        print(f"  {len(postings) - len(fresh)} already seen in earlier runs "
              f"({len(store)} in {cfg.db_path})")

        if args.dry_run:
            if args.limit:
                fresh = fresh[:args.limit]
            return dry_run(cfg, fresh, scorer)

        notion = Notion(token, database_id, cfg.notion_properties)
        if scorer:
            notion.check_columns({"fit_score": "number", "discipline": "select",
                                  "fit_reason": "rich_text"})
        existing = notion.existing_pages()
        print(f"  {len(existing)} rows already in Notion")

        # Rows already in Notion: fill blanks and record them, so the store
        # is populated from Notion on the first run. They are not scored.
        updated = present = 0
        new: list[Posting] = []
        for posting in postings:
            page = existing.get(posting.key)
            if page:
                if notion.fill_blanks(page, posting):
                    updated += 1
                store.add(posting, page["id"], status="present")
                present += 1
            elif not store.has(posting):
                new.append(posting)
        if args.limit:
            new = new[:args.limit]

        scores = scorer.score_all(new) if scorer else {}
        added = rejected = unscored = 0
        for posting in new:
            score = scores.get(posting.key)
            if score and score.score < cfg.scoring.min_score:
                store.add(posting, status="rejected", score=score)
                rejected += 1
                continue
            if scorer and score is None:
                unscored += 1  # API trouble: fall back to the keyword match
            page_id = notion.create(posting, score)
            store.add(posting, page_id, status="added", score=score)
            added += 1
            print(line(posting, score).replace("  - ", "  + ", 1))

        summary = (f"Done. {added} added, {updated} existing rows filled in, "
                   f"{present} already in Notion")
        if scorer:
            summary += f", {rejected} scored below {cfg.scoring.min_score}"
            if unscored:
                summary += f", {unscored} added unscored after API errors"
        print(summary + ".")
        return 0
    finally:
        store.close()


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
