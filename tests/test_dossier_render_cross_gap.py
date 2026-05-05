"""Tests for Phase 2: find_cross_gap_candidates cross-linking function.

Tests that AUTO-* PDFs are surfaced in dossiers for new-style CP/IP/TODO gaps.
Uses tmp_path fixtures — no live DB reads.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from adapters.article_index import open_index
from layers.dossier_render import find_cross_gap_candidates, assemble_dossier


# ---------------------------------------------------------------------------
# Fixture: DB with AUTO-* rows (PDFs) + a CP gap for querying
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_with_cross_gap(tmp_path: Path) -> sqlite3.Connection:
    """Return an open connection to a DB with cross-gap candidate data.

    Adds relevance_score / relevance_why columns (normally added by
    scripts/score_relevance.py, not part of the base DDL).
    """
    db_path = tmp_path / "test.sqlite"
    conn = open_index(db_path)

    # Add scoring columns that score_relevance.py normally adds via migration.
    for stmt in [
        "ALTER TABLE articles ADD COLUMN relevance_score INTEGER",
        "ALTER TABLE articles ADD COLUMN relevance_why TEXT",
        "ALTER TABLE articles ADD COLUMN scored_at TEXT",
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()

    # Insert gap_tree rows for the CP gap and a few AUTO gaps.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gap_tree (
            gap_id TEXT PRIMARY KEY,
            parent_gap_id TEXT,
            depth INTEGER DEFAULT 0,
            tier INTEGER,
            gap_type TEXT,
            chapter TEXT,
            heading_path TEXT DEFAULT '',
            claim_text TEXT,
            research_question TEXT DEFAULT '',
            source_locator TEXT DEFAULT '',
            evidence_target INTEGER DEFAULT 5,
            detector_pass TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            rationale TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # CP gap — the one requesting cross-gap candidates.
    conn.execute(
        "INSERT INTO gap_tree (gap_id, gap_type, claim_text, tier) VALUES (?,?,?,?)",
        ("CP31", "company_profile", "Sears Roebuck dominated mail order retail", 1),
    )
    # AUTO gap with PDF and relevant content.
    conn.execute(
        "INSERT INTO gap_tree (gap_id, gap_type, claim_text, tier) VALUES (?,?,?,?)",
        ("AUTO-181-G1", "intro_promise", "Mail order catalog Sears rural America", 1),
    )
    conn.commit()

    # Insert AUTO-* articles with PDFs and relevance_score >= 1.
    for i, (title, score) in enumerate([
        ("Sears Roebuck Catalog 1908", 3),
        ("Mail Order and Rural America", 2),
        ("Department Store Rise", 1),
        ("Unrelated Topic Foobar XYZ", 0),  # score 0 — should not appear
    ]):
        conn.execute(
            """INSERT INTO articles
               (title, authors, journal, pub_date, abstract, url,
                pdf_path, source_id, run_id, gap_id,
                relevance_score, gap_research_question)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                title, "Smith, J.", "Test Journal", "1990",
                f"Abstract about {title}",
                f"https://example.com/{i}",
                f"data/pull_outputs/run1/AUTO-181-G1/ebsco_api/{i}.pdf",
                "ebsco_api", "run1", "AUTO-181-G1",
                score,
                "mail order catalog Sears rural consumption",
            ),
        )
    conn.commit()
    yield conn
    conn.close()


class TestFindCrossGapCandidates:
    """Tests for find_cross_gap_candidates."""

    def test_returns_auto_pdfs_for_cp_gap(self, db_with_cross_gap):
        """CP gap gets AUTO-* PDFs matching its claim text."""
        conn = db_with_cross_gap
        results = find_cross_gap_candidates(conn, "CP31", limit=20)
        # Should find the relevant Sears/mail-order rows (score >= 1 + has pdf)
        assert len(results) >= 1
        for r in results:
            assert r["from_gap_id"] == "AUTO-181-G1"
            assert r["pdf_path"]  # all results must have a PDF

    def test_does_not_return_score_0(self, db_with_cross_gap):
        """Score-0 rows are excluded even when they match the query."""
        conn = db_with_cross_gap
        results = find_cross_gap_candidates(conn, "CP31", limit=20)
        titles = [r["title"] for r in results]
        # "Unrelated Topic Foobar XYZ" has score 0 and shouldn't appear
        assert not any("Foobar" in t for t in titles)

    def test_empty_for_gap_with_no_claim(self, db_with_cross_gap):
        """Gap with no claim_text → empty result (no crash)."""
        conn = db_with_cross_gap
        # Insert a gap with no claim_text
        conn.execute(
            "INSERT INTO gap_tree (gap_id, gap_type, claim_text) VALUES (?,?,?)",
            ("CP_NOCLAIM", "company_profile", ""),
        )
        conn.commit()
        results = find_cross_gap_candidates(conn, "CP_NOCLAIM", limit=20)
        assert results == []

    def test_limit_respected(self, db_with_cross_gap):
        """The limit parameter caps the result size."""
        conn = db_with_cross_gap
        results = find_cross_gap_candidates(conn, "CP31", limit=1)
        assert len(results) <= 1

    def test_entries_have_from_gap_id(self, db_with_cross_gap):
        """Each result entry carries from_gap_id."""
        conn = db_with_cross_gap
        results = find_cross_gap_candidates(conn, "CP31", limit=5)
        for r in results:
            assert "from_gap_id" in r
            assert r["from_gap_id"]  # non-empty


class TestAssembleDossierRelatedBucket:
    """Tests that assemble_dossier includes the 'related' bucket for new-style gaps."""

    def test_related_bucket_present_for_cp_gap(self, db_with_cross_gap):
        """CP gap dossier includes a 'related' key in tiers."""
        conn = db_with_cross_gap
        dossier = assemble_dossier(conn, "CP31")
        assert "related" in dossier["tiers"]

    def test_related_bucket_empty_for_auto_gap(self, db_with_cross_gap):
        """AUTO-* gaps don't trigger the cross-gap query (they ARE the source)."""
        conn = db_with_cross_gap
        dossier = assemble_dossier(conn, "AUTO-181-G1")
        # AUTO-* gaps should have an empty related bucket (or no query run)
        related = dossier["tiers"].get("related", [])
        # AUTO gap should not self-reference — related is empty or absent
        assert all(e.get("from_gap_id") != "AUTO-181-G1" for e in related)
