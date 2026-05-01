#!/usr/bin/env python3
"""Build or update the article index SQLite database from a pull_output run.

Usage:
    python scripts/index_articles.py --run-id run_27f86e44394442
    python scripts/index_articles.py --run-id run_27f86e44394442 --dedupe
    python scripts/index_articles.py --run-id run_27f86e44394442 --rebuild
    python scripts/index_articles.py --run-id run_27f86e44394442 --gap-id AUTO-01-G1

The index is idempotent by default: re-running with the same --run-id skips
rows that are already indexed (via the UNIQUE constraint on run_id, gap_id,
source_id, title) and only inserts newly-fetched articles.

The database file (data/article_index.sqlite) is gitignored (data/ is in
.gitignore). It can be recreated at any time from the pull_output files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Bootstrap: add project root to sys.path so project modules are importable
# when the script is run directly (python scripts/index_articles.py).
_SCRIPT_DIR   = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from adapters.article_index import dedupe_by_doi, ingest_pull_output, open_index


def _default_db_path() -> Path:
    return _PROJECT_ROOT / "data" / "article_index.sqlite"


def _default_pull_root(run_id: str) -> Path:
    return _PROJECT_ROOT / "data" / "pull_outputs" / run_id


def _print_summary(conn, inserted: int, deduped: int) -> None:
    """Print a compact summary to stdout."""
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    with_pdf = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE pdf_path IS NOT NULL"
    ).fetchone()[0]
    canonical_dupes = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE canonical_id IS NOT NULL"
    ).fetchone()[0]

    print(f"\nIndex summary")
    print(f"  Rows inserted this run : {inserted}")
    if deduped:
        print(f"  Rows marked as dupes  : {deduped}")
    print(f"  Total rows in DB      : {total}")
    print(f"  Articles with PDF     : {with_pdf}")
    print(f"  Duplicate DOI rows    : {canonical_dupes}")

    print("\nTop 5 sources:")
    rows = conn.execute(
        """
        SELECT source_id,
               COUNT(*) AS total,
               SUM(CASE WHEN pdf_path IS NOT NULL THEN 1 ELSE 0 END) AS with_pdf
        FROM articles
        GROUP BY source_id
        ORDER BY total DESC
        LIMIT 5
        """
    ).fetchall()
    for r in rows:
        print(f"  {r['source_id']:20s}  {r['total']:5d} articles  {r['with_pdf']:4d} with PDF")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build / update the article index from a pull_output run."
    )
    parser.add_argument(
        "--run-id", required=True, metavar="RUN_ID",
        help="Which pull_output run to index (e.g. run_27f86e44394442).",
    )
    parser.add_argument(
        "--db", metavar="PATH", default=None,
        help="Path to the SQLite database (default: data/article_index.sqlite).",
    )
    parser.add_argument(
        "--pull-root", metavar="PATH", default=None,
        help="Override path to the run's pull_output directory.",
    )
    parser.add_argument(
        "--gap-id", metavar="GAP_ID", default=None,
        help="Only index a specific gap (for targeted / incremental updates).",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Drop all rows for this run before re-indexing (not a full drop).",
    )
    parser.add_argument(
        "--dedupe", action="store_true",
        help="Run DOI deduplication after ingestion.",
    )
    args = parser.parse_args(argv)

    db_path   = Path(args.db) if args.db else _default_db_path()
    pull_root = Path(args.pull_root) if args.pull_root else _default_pull_root(args.run_id)

    if not pull_root.exists():
        print(f"ERROR: pull_root does not exist: {pull_root}", file=sys.stderr)
        return 1

    conn = open_index(db_path)
    print(f"Database : {db_path}")
    print(f"Run ID   : {args.run_id}")
    print(f"Pull root: {pull_root}")

    if args.rebuild:
        # Delete rows for this run so we start fresh (not a full table drop)
        deleted = conn.execute(
            "DELETE FROM articles WHERE run_id = ?", (args.run_id,)
        ).rowcount
        conn.commit()
        print(f"  Deleted {deleted} existing rows for run {args.run_id} (--rebuild)")

    inserted = ingest_pull_output(
        conn,
        pull_root,
        args.run_id,
        gap_filter=args.gap_id,
    )

    deduped = 0
    if args.dedupe:
        deduped = dedupe_by_doi(conn, run_id=args.run_id)

    _print_summary(conn, inserted, deduped)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
