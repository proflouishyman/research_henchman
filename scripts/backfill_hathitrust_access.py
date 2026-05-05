#!/usr/bin/env python3
"""Phase 1 backfill: populate access / hathi_id / subject / language on existing
HathiTrust rows in the article index.

Walks every data/pull_outputs/*/<gap_id>/hathitrust_fulltext/*.json file and
issues UPDATE statements keyed on (gap_id, source_id, title).  This preserves
all existing relevance_score values — UPDATE never touches unmentioned columns.

Usage:
    python3 scripts/backfill_hathitrust_access.py
    python3 scripts/backfill_hathitrust_access.py --db data/article_index.sqlite
    python3 scripts/backfill_hathitrust_access.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.article_index import open_index  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "article_index.sqlite"
PULL_OUTPUTS = PROJECT_ROOT / "data" / "pull_outputs"
SOURCE_ID = "hathitrust_fulltext"


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill HathiTrust access/hathi_id/subject/language.")
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite DB path.")
    p.add_argument("--dry-run", action="store_true", help="Print what would be updated; don't write.")
    args = p.parse_args()

    db_path = Path(args.db)
    conn = open_index(db_path)  # runs Phase 1 migration DDL if needed

    # Walk all hathitrust_fulltext seed JSON files in pull_outputs
    json_files = list(PULL_OUTPUTS.rglob(f"*/{SOURCE_ID}/*.json"))
    print(f"Found {len(json_files)} HathiTrust seed JSON files", flush=True)

    updated = 0
    skipped = 0
    missing = 0

    for jf in json_files:
        try:
            records = json.loads(jf.read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            print(f"[warn] {jf}: {exc}", flush=True)
            continue

        if not isinstance(records, list):
            records = [records]

        # Infer gap_id from path: pull_outputs/<run_id>/<gap_id>/hathitrust_fulltext/<file>.json
        gap_id = jf.parent.parent.name

        for rec in records:
            if not isinstance(rec, dict):
                continue
            title = (rec.get("title") or "").strip()
            if not title:
                continue

            access   = (rec.get("access") or "").strip() or None
            hathi_id = (rec.get("hathi_id") or "").strip() or None
            subject  = (rec.get("subject") or "").strip() or None
            language = (rec.get("language") or "").strip() or None

            # Skip records where all four fields are empty — nothing to update.
            if not any([access, hathi_id, subject, language]):
                skipped += 1
                continue

            if args.dry_run:
                print(f"  WOULD UPDATE gap={gap_id} title={title[:60]!r} "
                      f"access={access!r}", flush=True)
                updated += 1
                continue

            # UPDATE keyed on (gap_id, source_id, title) — all three are the
            # UNIQUE constraint components relevant to HathiTrust rows.
            result = conn.execute(
                """
                UPDATE articles
                   SET access   = COALESCE(:access,   access),
                       hathi_id = COALESCE(:hathi_id, hathi_id),
                       subject  = COALESCE(:subject,  subject),
                       language = COALESCE(:language, language)
                 WHERE gap_id    = :gap_id
                   AND source_id = :source_id
                   AND title     = :title
                   AND (access IS NULL OR hathi_id IS NULL OR subject IS NULL OR language IS NULL)
                """,
                {
                    "access":    access,
                    "hathi_id":  hathi_id,
                    "subject":   subject,
                    "language":  language,
                    "gap_id":    gap_id,
                    "source_id": SOURCE_ID,
                    "title":     title,
                },
            )
            if result.rowcount > 0:
                updated += result.rowcount
            else:
                missing += 1

    if not args.dry_run:
        conn.commit()

    conn.close()

    print(f"\n=== backfill summary ===", flush=True)
    print(f"  rows updated: {updated}", flush=True)
    print(f"  already-full (skipped update): {missing}", flush=True)
    print(f"  no-op (all fields blank in JSON): {skipped}", flush=True)

    # Verify: count rows now having access set
    if not args.dry_run:
        verify_conn = sqlite3.connect(str(db_path))
        n_with_access = verify_conn.execute(
            "SELECT COUNT(*) FROM articles WHERE source_id='hathitrust_fulltext' AND access IS NOT NULL"
        ).fetchone()[0]
        verify_conn.close()
        print(f"  hathitrust rows with access IS NOT NULL: {n_with_access}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
