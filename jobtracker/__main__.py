"""Sync job sources into the Notion Job Tracker.

    python -m jobtracker [--config config.toml] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import load_config
from .filters import apply_filters
from .models import Fatal, Posting
from .notion import Notion
from .sources import build_sources
from .store import SeenStore


def collect(cfg) -> list[Posting]:
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


def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not args.dry_run and not (token and database_id):
        raise Fatal("NOTION_TOKEN and NOTION_DATABASE_ID must be set")

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
            for p in sorted(fresh, key=lambda p: (p.studio.lower(), p.role.lower())):
                print(
                    f"  - {p.studio} | {p.role} | {p.location} | "
                    f"{p.experience or '-'} | {p.work_mode or '-'} | "
                    f"{p.date_issued or 'NO DATE'} | {p.url}"
                )
            print(f"Dry run: {len(fresh)} would be checked against Notion. "
                  "Nothing written.")
            return 0

        notion = Notion(token, database_id, cfg.notion_properties)
        existing = notion.existing_pages()
        print(f"  {len(existing)} rows already in Notion")

        added = updated = present = 0
        for posting in postings:
            page = existing.get(posting.key)
            if page:
                # Keep filling blanks on rows that are still listed, and record
                # them so the store is populated from Notion on the first run.
                if notion.fill_blanks(page, posting):
                    updated += 1
                store.add(posting, page["id"])
                present += 1
                continue
            if store.has(posting):
                continue  # handled before; renamed or deleted in Notion since
            page_id = notion.create(posting)
            store.add(posting, page_id)
            added += 1
            print(f"  + {posting.studio} | {posting.role}")

        print(f"Done. {added} added, {updated} existing rows filled in, "
              f"{present} already in Notion, "
              f"{len(postings) - added - present} skipped as seen before.")
        return 0
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="jobtracker")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show new matches without writing to Notion or the seen store",
    )
    try:
        return run(parser.parse_args())
    except Fatal as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
