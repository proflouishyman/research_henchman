#!/usr/bin/env python3
"""Query the article index for common analytical views.

Usage:
    python scripts/query_articles.py --sources
    python scripts/query_articles.py --gaps
    python scripts/query_articles.py --zero-pdf-gaps
    python scripts/query_articles.py --search "e-commerce India"
    python scripts/query_articles.py --gap AUTO-01-G1
    python scripts/query_articles.py --doi-duplicates

All subcommands read from data/article_index.sqlite by default.
Build the index first with: python scripts/index_articles.py --run-id <run_id>
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_SCRIPT_DIR   = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from adapters.article_index import open_index

_DEFAULT_DB = _PROJECT_ROOT / "data" / "article_index.sqlite"


def _open(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        print(
            f"ERROR: Database not found: {db_path}\n"
            "Build it first with: python scripts/index_articles.py --run-id <run_id>",
            file=sys.stderr,
        )
        sys.exit(1)
    return open_index(db_path)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_sources(conn: sqlite3.Connection, limit: int) -> None:
    """List source_ids with article and PDF counts."""
    rows = conn.execute(
        """
        SELECT source_id,
               COUNT(*)                                               AS total,
               SUM(CASE WHEN pdf_path IS NOT NULL THEN 1 ELSE 0 END) AS with_pdf,
               SUM(CASE WHEN pdf_path IS NULL     THEN 1 ELSE 0 END) AS metadata_only
        FROM articles
        GROUP BY source_id
        ORDER BY total DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        print("No articles indexed.")
        return

    print(f"{'Source':<22} {'Total':>7} {'With PDF':>9} {'Metadata-only':>14}")
    print("-" * 56)
    for r in rows:
        print(
            f"{r['source_id']:<22} {r['total']:>7} {r['with_pdf']:>9} {r['metadata_only']:>14}"
        )


def cmd_gaps(conn: sqlite3.Connection, limit: int) -> None:
    """List gaps with article and PDF counts."""
    rows = conn.execute(
        """
        SELECT gap_id,
               COUNT(*)                                               AS total,
               SUM(CASE WHEN pdf_path IS NOT NULL THEN 1 ELSE 0 END) AS with_pdf
        FROM articles
        GROUP BY gap_id
        ORDER BY total DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        print("No articles indexed.")
        return

    print(f"{'Gap ID':<18} {'Total':>7} {'With PDF':>9}")
    print("-" * 38)
    for r in rows:
        print(f"{r['gap_id']:<18} {r['total']:>7} {r['with_pdf']:>9}")


def cmd_zero_pdf_gaps(conn: sqlite3.Connection) -> None:
    """List gaps that have 0 articles with PDFs."""
    rows = conn.execute(
        """
        SELECT gap_id,
               COUNT(*)                                               AS total,
               SUM(CASE WHEN pdf_path IS NOT NULL THEN 1 ELSE 0 END) AS with_pdf
        FROM articles
        GROUP BY gap_id
        HAVING SUM(CASE WHEN pdf_path IS NOT NULL THEN 1 ELSE 0 END) = 0
        ORDER BY total DESC
        """
    ).fetchall()
    if not rows:
        print("All gaps have at least one PDF.")
        return

    print(f"Gaps with 0 PDFs ({len(rows)} total):")
    print(f"{'Gap ID':<18} {'Articles (metadata-only)':>24}")
    print("-" * 44)
    for r in rows:
        print(f"{r['gap_id']:<18} {r['total']:>24}")


def cmd_search(conn: sqlite3.Connection, query: str, limit: int) -> None:
    """Full-text search on title, abstract, authors, gap_research_question."""
    # FTS5 requires the query to not be empty
    query = query.strip()
    if not query:
        print("ERROR: search query cannot be empty.", file=sys.stderr)
        return

    rows = conn.execute(
        """
        SELECT a.id, a.title, a.authors, a.gap_id, a.source_id, a.pdf_path,
               a.abstract, a.gap_research_question,
               bm25(articles_fts) AS rank
        FROM articles_fts
        JOIN articles a ON a.id = articles_fts.rowid
        WHERE articles_fts MATCH ?
          AND a.canonical_id IS NULL          -- exclude duplicates
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()

    if not rows:
        print(f"No results for: {query!r}")
        return

    print(f"Search results for: {query!r}  ({len(rows)} shown)\n")
    for i, r in enumerate(rows, 1):
        pdf_flag = "[PDF]" if r["pdf_path"] else "[meta]"
        print(f"{i:2d}. {pdf_flag} {r['title']}")
        if r["authors"]:
            print(f"    Authors: {r['authors']}")
        print(f"    Gap: {r['gap_id']}  Source: {r['source_id']}")
        if r["gap_research_question"]:
            q = r["gap_research_question"]
            print(f"    Research Q: {q[:100]}{'...' if len(q) > 100 else ''}")
        if r["abstract"]:
            a = r["abstract"]
            print(f"    Abstract:  {a[:120]}{'...' if len(a) > 120 else ''}")
        print()


def cmd_gap(conn: sqlite3.Connection, gap_id: str) -> None:
    """Show all articles for one gap."""
    rows = conn.execute(
        """
        SELECT title, authors, source_id, pdf_path, journal, abstract, canonical_id
        FROM articles
        WHERE gap_id = ?
        ORDER BY source_id, title
        """,
        (gap_id,),
    ).fetchall()

    if not rows:
        print(f"No articles found for gap: {gap_id}")
        return

    print(f"Articles for gap {gap_id} ({len(rows)} rows):\n")
    for r in rows:
        dup = " [DUPE]" if r["canonical_id"] else ""
        pdf = " [PDF]" if r["pdf_path"] else ""
        print(f"  [{r['source_id']}]{pdf}{dup} {r['title']}")
        if r["authors"]:
            print(f"    Authors: {r['authors']}")
        if r["journal"]:
            print(f"    Source:  {r['journal']}")


def cmd_doi_duplicates(conn: sqlite3.Connection) -> None:
    """List DOIs that have multiple rows in the index."""
    rows = conn.execute(
        """
        SELECT doi, COUNT(*) AS cnt,
               GROUP_CONCAT(source_id, ', ') AS sources
        FROM articles
        WHERE doi IS NOT NULL
        GROUP BY doi
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
        """
    ).fetchall()

    if not rows:
        print("No duplicate DOIs found.")
        return

    print(f"{'DOI':<45} {'Count':>6} {'Sources'}")
    print("-" * 80)
    for r in rows:
        print(f"{r['doi']:<45} {r['cnt']:>6}  {r['sources']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Query the article index database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db", metavar="PATH", default=None,
        help="Path to the SQLite database (default: data/article_index.sqlite).",
    )
    parser.add_argument(
        "--sources", action="store_true",
        help="List source_ids with article and PDF counts.",
    )
    parser.add_argument(
        "--gaps", action="store_true",
        help="List gap_ids with article and PDF counts (top 20 by count).",
    )
    parser.add_argument(
        "--zero-pdf-gaps", action="store_true",
        help="List gaps with 0 PDFs.",
    )
    parser.add_argument(
        "--search", metavar="QUERY",
        help="Full-text search on title / abstract / authors / research question.",
    )
    parser.add_argument(
        "--gap", metavar="GAP_ID",
        help="Show all articles for the named gap.",
    )
    parser.add_argument(
        "--doi-duplicates", action="store_true",
        help="List DOIs that appear in more than one row.",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Max rows to display for --gaps, --search (default: 20).",
    )
    args = parser.parse_args(argv)

    # At least one action must be specified
    if not any([
        args.sources, args.gaps, args.zero_pdf_gaps,
        args.search, args.gap, args.doi_duplicates,
    ]):
        parser.print_help()
        return 1

    db_path = Path(args.db) if args.db else _DEFAULT_DB
    conn = _open(db_path)

    if args.sources:
        cmd_sources(conn, limit=args.limit)
    if args.gaps:
        cmd_gaps(conn, limit=args.limit)
    if args.zero_pdf_gaps:
        cmd_zero_pdf_gaps(conn)
    if args.search:
        cmd_search(conn, args.search, limit=args.limit)
    if args.gap:
        cmd_gap(conn, args.gap)
    if args.doi_duplicates:
        cmd_doi_duplicates(conn)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
