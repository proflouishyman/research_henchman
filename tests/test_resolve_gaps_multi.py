"""Tests for cross-gap sibling resolution in POST /api/library/articles/resolve_gaps.

Phase 5 semantics: the mapping value for each article_id must contain ALL
distinct gap_ids the article appears in across the corpus, not just its own
primary gap_id. Siblings are detected via shared doi, url, hathi_id, or
(source_id, title) pairs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared DDL — mirrors the shape in test_library_api_marks.py (no
# relevance_score columns to ensure the endpoint doesn't depend on them).
# ---------------------------------------------------------------------------

_ARTICLES_DDL = """
CREATE TABLE articles (
    id            INTEGER PRIMARY KEY,
    gap_id        TEXT,
    source_id     TEXT,
    title         TEXT NOT NULL DEFAULT '',
    authors       TEXT, journal TEXT, pub_date TEXT,
    abstract      TEXT, url TEXT, pdf_path TEXT, md_path TEXT,
    run_id        TEXT NOT NULL DEFAULT '',
    doi           TEXT,
    hathi_id      TEXT,
    canonical_id  INTEGER,
    database_name TEXT, bquery_original TEXT, bquery_normalized TEXT,
    variant_index INTEGER, gap_research_question TEXT, gap_topic TEXT,
    relevance_why TEXT,
    indexed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, gap_id, source_id, title)
)
"""

_GAP_TREE_DDL = """
CREATE TABLE gap_tree (
    gap_id TEXT PRIMARY KEY,
    parent_gap_id TEXT, depth INTEGER DEFAULT 0,
    tier INTEGER DEFAULT 1, gap_type TEXT DEFAULT 'explicit',
    chapter TEXT, heading_path TEXT, claim_text TEXT,
    research_question TEXT, source_locator TEXT,
    evidence_target INTEGER DEFAULT 0, detector_pass TEXT,
    status TEXT DEFAULT 'pending', rationale TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_MARKS_DDL = """
CREATE TABLE IF NOT EXISTS user_marks (
    article_id INTEGER PRIMARY KEY,
    starred INTEGER DEFAULT 0,
    read INTEGER DEFAULT 0,
    note TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


def _make_db(tmp_path: Path, rows: list[dict]) -> Path:
    """Create a minimal article_index.sqlite with the given article rows."""
    db = tmp_path / "article_index.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(_ARTICLES_DDL)
    conn.execute(_GAP_TREE_DDL)
    conn.execute(_MARKS_DDL)
    for row in rows:
        # Build a safe INSERT from whichever columns are provided.
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT INTO articles ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Factory: given a list of article row dicts, return a TestClient."""
    def _factory(rows: list[dict]) -> TestClient:
        db_path = _make_db(tmp_path, rows)
        monkeypatch.setenv("ORCH_DATA_ROOT", str(db_path.parent))
        # Re-import so the monkeypatched env var is picked up on each factory
        # call within the same test session shard.
        import importlib
        import main as _main
        importlib.reload(_main)
        from main import app
        return TestClient(app, raise_server_exceptions=True)
    return _factory


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _resolve(client: TestClient, *ids: int) -> dict:
    res = client.post(
        "/api/library/articles/resolve_gaps",
        json={"article_ids": list(ids)},
    )
    assert res.status_code == 200
    return res.json()["mapping"]


# ---------------------------------------------------------------------------
# Test 1: same doi → both gaps, own gap first
# ---------------------------------------------------------------------------

class TestSameDoi:
    _ROWS = [
        {"id": 1, "gap_id": "GAP_A", "doi": "10.1000/test", "title": "Article One",   "run_id": "r1", "source_id": "src1"},
        {"id": 2, "gap_id": "GAP_B", "doi": "10.1000/test", "title": "Article Two",   "run_id": "r2", "source_id": "src2"},
    ]

    def test_article_1_sees_gap_b(self, make_client) -> None:
        mapping = _resolve(make_client(self._ROWS), 1)
        assert mapping["1"][0] == "GAP_A", "primary gap must be first"
        assert "GAP_B" in mapping["1"]

    def test_article_2_sees_gap_a(self, make_client) -> None:
        mapping = _resolve(make_client(self._ROWS), 2)
        assert mapping["2"][0] == "GAP_B", "primary gap must be first"
        assert "GAP_A" in mapping["2"]

    def test_no_duplicates(self, make_client) -> None:
        mapping = _resolve(make_client(self._ROWS), 1)
        assert len(mapping["1"]) == len(set(mapping["1"]))


# ---------------------------------------------------------------------------
# Test 2: same url → both gaps
# ---------------------------------------------------------------------------

class TestSameUrl:
    _ROWS = [
        {"id": 10, "gap_id": "GAP_X", "url": "https://example.com/paper", "title": "Paper X", "run_id": "r1", "source_id": "src1"},
        {"id": 11, "gap_id": "GAP_Y", "url": "https://example.com/paper", "title": "Paper Y", "run_id": "r2", "source_id": "src2"},
    ]

    def test_sibling_via_url(self, make_client) -> None:
        mapping = _resolve(make_client(self._ROWS), 10)
        assert mapping["10"][0] == "GAP_X"
        assert "GAP_Y" in mapping["10"]

    def test_other_direction(self, make_client) -> None:
        mapping = _resolve(make_client(self._ROWS), 11)
        assert mapping["11"][0] == "GAP_Y"
        assert "GAP_X" in mapping["11"]


# ---------------------------------------------------------------------------
# Test 3: same (source_id, title) → both gaps
# ---------------------------------------------------------------------------

class TestSameSourceTitle:
    _ROWS = [
        {"id": 20, "gap_id": "GAP_P", "title": "Shared Title", "source_id": "pubmed", "run_id": "r1"},
        {"id": 21, "gap_id": "GAP_Q", "title": "Shared Title", "source_id": "pubmed", "run_id": "r2"},
    ]

    def test_sibling_via_source_title(self, make_client) -> None:
        mapping = _resolve(make_client(self._ROWS), 20)
        assert mapping["20"][0] == "GAP_P"
        assert "GAP_Q" in mapping["20"]

    def test_alphabetical_order_of_extra_gaps(self, make_client) -> None:
        """Sibling gaps beyond the primary must be sorted alphabetically."""
        rows = [
            {"id": 30, "gap_id": "GAP_Z", "title": "Multi", "source_id": "pub", "run_id": "r1"},
            {"id": 31, "gap_id": "GAP_A", "title": "Multi", "source_id": "pub", "run_id": "r2"},
            {"id": 32, "gap_id": "GAP_M", "title": "Multi", "source_id": "pub", "run_id": "r3"},
        ]
        mapping = _resolve(make_client(rows), 30)
        assert mapping["30"][0] == "GAP_Z"
        assert mapping["30"][1:] == ["GAP_A", "GAP_M"]


# ---------------------------------------------------------------------------
# Test 4: same title DIFFERENT source_id, no other shared key → no false positive
# ---------------------------------------------------------------------------

class TestTitleOnlyNoMatch:
    _ROWS = [
        {"id": 40, "gap_id": "GAP_1", "title": "Common Title", "source_id": "src_a", "run_id": "r1"},
        {"id": 41, "gap_id": "GAP_2", "title": "Common Title", "source_id": "src_b", "run_id": "r2"},
    ]

    def test_different_source_no_sibling(self, make_client) -> None:
        """Articles sharing only title but with different source_ids must NOT merge."""
        mapping = _resolve(make_client(self._ROWS), 40)
        assert mapping["40"] == ["GAP_1"], (
            "Different source_id means (source_id, title) pair doesn't match — no sibling"
        )


# ---------------------------------------------------------------------------
# Test 5: no siblings → single-element list (regression)
# ---------------------------------------------------------------------------

class TestNoSiblings:
    _ROWS = [
        {"id": 50, "gap_id": "SOLO", "title": "Lone Article", "run_id": "r1", "source_id": "src1"},
        {"id": 51, "gap_id": "OTHER", "title": "Different",   "run_id": "r2", "source_id": "src2"},
    ]

    def test_single_gap_returned(self, make_client) -> None:
        mapping = _resolve(make_client(self._ROWS), 50)
        assert mapping["50"] == ["SOLO"]

    def test_empty_request_still_works(self, make_client) -> None:
        client = make_client(self._ROWS)
        res = client.post("/api/library/articles/resolve_gaps", json={"article_ids": []})
        assert res.status_code == 200
        assert res.json()["mapping"] == {}


# ---------------------------------------------------------------------------
# Test 6: hathi_id match → both gaps (bonus coverage of that key)
# ---------------------------------------------------------------------------

class TestSameHathiId:
    _ROWS = [
        {"id": 60, "gap_id": "GAP_H1", "hathi_id": "mdp.123", "title": "Hathi One", "run_id": "r1", "source_id": "s1"},
        {"id": 61, "gap_id": "GAP_H2", "hathi_id": "mdp.123", "title": "Hathi Two", "run_id": "r2", "source_id": "s2"},
    ]

    def test_sibling_via_hathi_id(self, make_client) -> None:
        mapping = _resolve(make_client(self._ROWS), 60)
        assert mapping["60"][0] == "GAP_H1"
        assert "GAP_H2" in mapping["60"]


# ---------------------------------------------------------------------------
# Test 7: null/empty doi must not cause false-positive cross-article match
# ---------------------------------------------------------------------------

class TestNullDoiNoFalsePositive:
    _ROWS = [
        {"id": 70, "gap_id": "GAP_NA", "doi": None,  "title": "No DOI One", "run_id": "r1", "source_id": "s1"},
        {"id": 71, "gap_id": "GAP_NB", "doi": None,  "title": "No DOI Two", "run_id": "r2", "source_id": "s2"},
        {"id": 72, "gap_id": "GAP_NC", "doi": "",    "title": "Empty DOI",  "run_id": "r3", "source_id": "s3"},
    ]

    def test_null_doi_no_match(self, make_client) -> None:
        mapping = _resolve(make_client(self._ROWS), 70)
        assert mapping["70"] == ["GAP_NA"]

    def test_empty_string_doi_no_match(self, make_client) -> None:
        mapping = _resolve(make_client(self._ROWS), 72)
        assert mapping["72"] == ["GAP_NC"]
