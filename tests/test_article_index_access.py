"""Tests for Phase 1: article index schema migration and HathiTrust backfill.

Verifies:
  - Schema migration adds access/hathi_id/subject/language columns idempotently.
  - _ingest_seed_json correctly populates the new columns from HathiTrust seeds.
  - The backfill UPDATE logic works correctly.
  - Migration is idempotent (calling open_index twice on the same DB is safe).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from adapters.article_index import (
    _ingest_seed_json,
    ingest_pull_output,
    open_index,
)


# ---------------------------------------------------------------------------
# Tests: schema migration
# ---------------------------------------------------------------------------

class TestSchemasMigration:
    def test_new_columns_present_after_open(self, tmp_path: Path) -> None:
        """open_index creates a DB with all Phase 1 columns present."""
        db_path = tmp_path / "idx.sqlite"
        conn = open_index(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
        for col in ("access", "hathi_id", "subject", "language"):
            assert col in cols, f"missing column: {col}"
        conn.close()

    def test_migration_idempotent(self, tmp_path: Path) -> None:
        """Calling open_index twice on the same DB doesn't crash (idempotent)."""
        db_path = tmp_path / "idx.sqlite"
        conn1 = open_index(db_path)
        conn1.close()
        conn2 = open_index(db_path)  # should not raise
        cols = [r[1] for r in conn2.execute("PRAGMA table_info(articles)").fetchall()]
        for col in ("access", "hathi_id", "subject", "language"):
            assert col in cols
        conn2.close()

    def test_partial_index_created(self, tmp_path: Path) -> None:
        """The partial index on access for hathitrust_fulltext is created."""
        db_path = tmp_path / "idx.sqlite"
        conn = open_index(db_path)
        # SQLite stores index metadata in sqlite_master
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_access'"
        ).fetchone()
        assert idx is not None, "idx_access index not found"
        conn.close()


# ---------------------------------------------------------------------------
# Tests: _ingest_seed_json populates access/hathi_id/subject/language
# ---------------------------------------------------------------------------

class TestIngestSeedJsonAccessFields:
    def _make_seed_dir(self, tmp_path: Path, records: list) -> Path:
        """Write seed JSON to a source directory and return its path."""
        src_dir = tmp_path / "run1" / "AUTO-01-G1" / "hathitrust_fulltext"
        src_dir.mkdir(parents=True)
        (src_dir / "seed.json").write_text(json.dumps(records), encoding="utf-8")
        return src_dir

    def test_access_field_ingested(self, tmp_path: Path) -> None:
        """access field is stored in the DB when present in seed JSON."""
        db_path = tmp_path / "idx.sqlite"
        conn = open_index(db_path)

        records = [{
            "title": "Sears Catalog 1908",
            "url": "https://babel.hathitrust.org/cgi/pt?id=mdp.1",
            "authors": "Sears, Roebuck",
            "pub_date": "1908",
            "access": "Full view",
            "hathi_id": "mdp.49015001020396",
            "subject": "Catalogs",
            "language": "English",
        }]
        src_dir = self._make_seed_dir(tmp_path, records)

        inserted = _ingest_seed_json(
            conn, src_dir,
            run_id="run1",
            gap_id="AUTO-01-G1",
            source_id="hathitrust_fulltext",
            research_question="How did Sears shape retail?",
            gap_topic="Retail History",
        )
        conn.commit()

        assert inserted == 1
        row = conn.execute("SELECT * FROM articles LIMIT 1").fetchone()
        assert row["access"] == "Full view"
        assert row["hathi_id"] == "mdp.49015001020396"
        assert row["subject"] == "Catalogs"
        assert row["language"] == "English"
        conn.close()

    def test_null_access_when_missing(self, tmp_path: Path) -> None:
        """When access fields are absent from seed, they are stored as NULL."""
        db_path = tmp_path / "idx.sqlite"
        conn = open_index(db_path)

        records = [{"title": "Mystery Book", "url": "https://example.com"}]
        src_dir = self._make_seed_dir(tmp_path, records)

        _ingest_seed_json(
            conn, src_dir,
            run_id="run1",
            gap_id="AUTO-01-G1",
            source_id="hathitrust_fulltext",
            research_question=None,
            gap_topic=None,
        )
        conn.commit()

        row = conn.execute("SELECT access, hathi_id, subject, language FROM articles LIMIT 1").fetchone()
        assert row["access"] is None
        assert row["hathi_id"] is None
        conn.close()

    def test_ia_access_field_ingested(self, tmp_path: Path) -> None:
        """Internet Archive seeds with access='Full view' are stored correctly."""
        db_path = tmp_path / "idx.sqlite"
        conn = open_index(db_path)

        records = [{
            "title": "IA Book About Retail",
            "url": "https://archive.org/details/ia_book_retail",
            "authors": "Author Name",
            "pub_date": "1950",
            "access": "Full view",
            "ia_identifier": "ia_book_retail",
        }]
        src_dir = tmp_path / "run1" / "CP31" / "internet_archive"
        src_dir.mkdir(parents=True)
        (src_dir / "seed.json").write_text(json.dumps(records), encoding="utf-8")

        _ingest_seed_json(
            conn, src_dir,
            run_id="run1",
            gap_id="CP31",
            source_id="internet_archive",
            research_question=None,
            gap_topic=None,
        )
        conn.commit()

        row = conn.execute("SELECT access FROM articles WHERE source_id='internet_archive' LIMIT 1").fetchone()
        assert row is not None
        assert row["access"] == "Full view"
        conn.close()


# ---------------------------------------------------------------------------
# Tests: backfill UPDATE logic
# ---------------------------------------------------------------------------

class TestBackfillLogic:
    """Validate the backfill UPDATE behavior used by the backfill script."""

    def test_update_sets_access_on_existing_rows(self, tmp_path: Path) -> None:
        """UPDATE sets access on a pre-existing row that has NULL access."""
        db_path = tmp_path / "idx.sqlite"
        conn = open_index(db_path)

        # Insert a row without access (simulates pre-Phase-1 row).
        conn.execute(
            """INSERT INTO articles
               (title, run_id, gap_id, source_id, gap_research_question)
               VALUES (?, ?, ?, ?, ?)""",
            ("Old HathiTrust Book", "run_old", "AUTO-01-G1", "hathitrust_fulltext", ""),
        )
        conn.commit()

        # Run the backfill UPDATE (same logic as the script).
        conn.execute(
            """UPDATE articles
                  SET access   = COALESCE(:access,   access),
                      hathi_id = COALESCE(:hathi_id, hathi_id)
                WHERE gap_id = :gap_id AND source_id = :source_id AND title = :title
                  AND (access IS NULL OR hathi_id IS NULL)""",
            {
                "access":    "Full view",
                "hathi_id":  "mdp.99999",
                "gap_id":    "AUTO-01-G1",
                "source_id": "hathitrust_fulltext",
                "title":     "Old HathiTrust Book",
            },
        )
        conn.commit()

        row = conn.execute(
            "SELECT access, hathi_id FROM articles WHERE title = 'Old HathiTrust Book'"
        ).fetchone()
        assert row["access"] == "Full view"
        assert row["hathi_id"] == "mdp.99999"
        conn.close()

    def test_backfill_does_not_overwrite_existing_access(self, tmp_path: Path) -> None:
        """COALESCE in backfill UPDATE preserves existing non-NULL access values."""
        db_path = tmp_path / "idx.sqlite"
        conn = open_index(db_path)

        conn.execute(
            """INSERT INTO articles
               (title, run_id, gap_id, source_id, access, gap_research_question)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("Book With Access", "run_old", "AUTO-01-G1", "hathitrust_fulltext",
             "Limited (search-only)", ""),
        )
        conn.commit()

        # Attempt to update — COALESCE should keep the existing value.
        conn.execute(
            """UPDATE articles
                  SET access = COALESCE(:access, access)
                WHERE title = :title AND (access IS NULL)""",
            {"access": "Full view", "title": "Book With Access"},
        )
        conn.commit()

        row = conn.execute(
            "SELECT access FROM articles WHERE title = 'Book With Access'"
        ).fetchone()
        # The WHERE clause `access IS NULL` means the row was NOT updated.
        assert row["access"] == "Limited (search-only)"
        conn.close()
