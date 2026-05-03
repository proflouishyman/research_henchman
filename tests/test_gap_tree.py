"""Tests for adapters/gap_tree.py — the multi-pass detector schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from adapters.gap_tree import (
    count_by_pass,
    ensure_gap_tree_schema,
    fetch_research_question,
    gap_exists,
    insert_node,
    list_nodes,
    update_research_question,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path) -> sqlite3.Connection:
    """Open a fresh SQLite DB with the gap_tree schema applied."""
    conn = sqlite3.connect(str(tmp_path / "gt.sqlite"))
    conn.row_factory = sqlite3.Row
    ensure_gap_tree_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestEnsureGapTreeSchema:
    def test_creates_table(self, db):
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gap_tree'"
        ).fetchone()
        assert row is not None

    def test_idempotent(self, tmp_path):
        """Calling ensure_gap_tree_schema twice raises no error."""
        conn = sqlite3.connect(str(tmp_path / "gt2.sqlite"))
        ensure_gap_tree_schema(conn)
        ensure_gap_tree_schema(conn)  # should not raise
        # Inserts still work.
        ok = insert_node(
            conn, gap_id="IP1", tier=1, gap_type="intro_promise",
            evidence_target=120, detector_pass="A",
        )
        assert ok is True

    def test_indexes_present(self, db):
        idx_names = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='gap_tree'"
            ).fetchall()
        }
        assert "idx_gt_parent" in idx_names
        assert "idx_gt_tier" in idx_names
        assert "idx_gt_status" in idx_names
        assert "idx_gt_pass" in idx_names


# ---------------------------------------------------------------------------
# Insert + list round-trip
# ---------------------------------------------------------------------------

class TestInsertAndListNodes:
    def test_round_trip_top_level(self, db):
        ok = insert_node(
            db, gap_id="IP1", tier=1, gap_type="intro_promise",
            evidence_target=120, detector_pass="A",
            chapter="Chapter 1", claim_text="A claim", research_question="A question?",
        )
        assert ok is True

        rows = list_nodes(db, detector_pass="A")
        assert len(rows) == 1
        r = rows[0]
        assert r["gap_id"] == "IP1"
        assert r["tier"] == 1
        assert r["gap_type"] == "intro_promise"
        assert r["evidence_target"] == 120
        assert r["chapter"] == "Chapter 1"
        assert r["claim_text"] == "A claim"
        assert r["research_question"] == "A question?"
        assert r["depth"] == 0
        assert r["parent_gap_id"] is None
        assert r["status"] == "pending"

    def test_duplicate_pk_returns_false(self, db):
        ok1 = insert_node(
            db, gap_id="IP1", tier=1, gap_type="intro_promise",
            evidence_target=120, detector_pass="A",
        )
        ok2 = insert_node(
            db, gap_id="IP1", tier=1, gap_type="intro_promise",
            evidence_target=120, detector_pass="A",
        )
        assert ok1 is True
        assert ok2 is False

    def test_child_inherits_depth(self, db):
        insert_node(
            db, gap_id="IP1", tier=1, gap_type="intro_promise",
            evidence_target=120, detector_pass="A",
        )
        insert_node(
            db, gap_id="IP1.A", tier=2, gap_type="intro_promise",
            evidence_target=60, detector_pass="A",
            parent_gap_id="IP1",
        )
        row = db.execute(
            "SELECT depth, parent_gap_id FROM gap_tree WHERE gap_id = ?",
            ("IP1.A",),
        ).fetchone()
        assert row["depth"] == 1
        assert row["parent_gap_id"] == "IP1"

    def test_filter_by_tier(self, db):
        insert_node(db, gap_id="IP1", tier=1, gap_type="intro_promise",
                    evidence_target=120, detector_pass="A")
        insert_node(db, gap_id="IP2", tier=2, gap_type="intro_promise",
                    evidence_target=60, detector_pass="A")
        rows1 = list_nodes(db, tier=1)
        rows2 = list_nodes(db, tier=2)
        assert {r["gap_id"] for r in rows1} == {"IP1"}
        assert {r["gap_id"] for r in rows2} == {"IP2"}

    def test_filter_root_with_sentinel(self, db):
        insert_node(db, gap_id="IP1", tier=1, gap_type="intro_promise",
                    evidence_target=120, detector_pass="A")
        insert_node(db, gap_id="IP1.A", tier=2, gap_type="intro_promise",
                    evidence_target=60, detector_pass="A",
                    parent_gap_id="IP1")
        roots = list_nodes(db, parent_gap_id="<root>")
        assert {r["gap_id"] for r in roots} == {"IP1"}


# ---------------------------------------------------------------------------
# count_by_pass
# ---------------------------------------------------------------------------

class TestCountByPass:
    def test_multi_pass_counts(self, db):
        for i in range(3):
            insert_node(db, gap_id=f"IP{i+1}", tier=1, gap_type="intro_promise",
                        evidence_target=120, detector_pass="A")
        for i in range(5):
            insert_node(db, gap_id=f"TODO{i+1}", tier=1, gap_type="explicit_todo",
                        evidence_target=40, detector_pass="B")
        counts = count_by_pass(db)
        assert counts == {"A": 3, "B": 5}

    def test_unknown_pass_bucket(self, db):
        insert_node(db, gap_id="X1", tier=1, gap_type="explicit_todo",
                    evidence_target=40, detector_pass=None)
        counts = count_by_pass(db)
        assert counts.get("unknown") == 1


# ---------------------------------------------------------------------------
# Helper functions used by the resume path
# ---------------------------------------------------------------------------

class TestResumeHelpers:
    def test_gap_exists(self, db):
        assert gap_exists(db, "IP1") is False
        insert_node(db, gap_id="IP1", tier=1, gap_type="intro_promise",
                    evidence_target=120, detector_pass="A")
        assert gap_exists(db, "IP1") is True

    def test_fetch_research_question_returns_none_for_empty(self, db):
        insert_node(db, gap_id="IP1", tier=1, gap_type="intro_promise",
                    evidence_target=120, detector_pass="A",
                    research_question="")
        assert fetch_research_question(db, "IP1") is None

    def test_update_research_question(self, db):
        insert_node(db, gap_id="IP1", tier=1, gap_type="intro_promise",
                    evidence_target=120, detector_pass="A")
        update_research_question(db, "IP1", "What did Alibaba do in 2003?")
        rq = fetch_research_question(db, "IP1")
        assert rq == "What did Alibaba do in 2003?"
