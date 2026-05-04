"""Tests for the marks API endpoints (POST /api/library/marks, GET /api/library/marks,
POST /api/library/articles/resolve_gaps).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixture DB + client
# ---------------------------------------------------------------------------

@pytest.fixture()
def fixture_db_path(tmp_path: Path) -> Path:
    """Create a minimal SQLite DB with articles and gap_tree tables."""
    db = tmp_path / "article_index.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            gap_id TEXT,
            relevance_score INTEGER,
            source_id TEXT,
            title TEXT NOT NULL DEFAULT '',
            authors TEXT, journal TEXT, pub_date TEXT,
            abstract TEXT, url TEXT, pdf_path TEXT, md_path TEXT,
            run_id TEXT NOT NULL DEFAULT '',
            doi TEXT, canonical_id INTEGER,
            database_name TEXT, bquery_original TEXT, bquery_normalized TEXT,
            variant_index INTEGER, gap_research_question TEXT, gap_topic TEXT,
            relevance_why TEXT,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, gap_id, source_id, title)
        )"""
    )
    conn.execute("INSERT INTO articles (id, gap_id, title, run_id, source_id) VALUES (1, 'IP1', 'Test Article', 'run1', 'ebsco_api')")
    conn.execute("INSERT INTO articles (id, gap_id, title, run_id, source_id) VALUES (2, 'IP2', 'Second Article', 'run1', 'ebsco_api')")
    conn.execute(
        """CREATE TABLE gap_tree (
            gap_id TEXT PRIMARY KEY,
            parent_gap_id TEXT, depth INTEGER DEFAULT 0,
            tier INTEGER DEFAULT 1, gap_type TEXT DEFAULT 'explicit',
            chapter TEXT, heading_path TEXT, claim_text TEXT,
            research_question TEXT, source_locator TEXT,
            evidence_target INTEGER DEFAULT 0, detector_pass TEXT,
            status TEXT DEFAULT 'pending', rationale TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS user_marks (article_id INTEGER PRIMARY KEY, starred INTEGER DEFAULT 0, read INTEGER DEFAULT 0, note TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def client(fixture_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ORCH_DATA_ROOT", str(fixture_db_path.parent))
    from main import app
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# POST /api/library/marks
# ---------------------------------------------------------------------------

class TestMarksUpsert:
    def test_star_article(self, client: TestClient) -> None:
        res = client.post("/api/library/marks", json={"article_id": 1, "starred": True})
        assert res.status_code == 200
        data = res.json()
        assert data["article_id"] == 1
        assert data["starred"] is True

    def test_mark_read(self, client: TestClient) -> None:
        res = client.post("/api/library/marks", json={"article_id": 1, "read": True})
        assert res.status_code == 200
        assert res.json()["read"] is True

    def test_upsert_idempotent(self, client: TestClient) -> None:
        """Calling twice with the same payload should not error and keep the data."""
        client.post("/api/library/marks", json={"article_id": 2, "starred": True})
        res = client.post("/api/library/marks", json={"article_id": 2, "starred": True})
        assert res.status_code == 200
        assert res.json()["starred"] is True

    def test_unstar_removes_empty_row(self, client: TestClient) -> None:
        """Un-starring with read=False should delete the row (no note)."""
        client.post("/api/library/marks", json={"article_id": 2, "starred": True})
        res = client.post("/api/library/marks", json={"article_id": 2, "starred": False, "read": False})
        assert res.status_code == 200
        assert res.json()["starred"] is False

    def test_note_persisted(self, client: TestClient) -> None:
        res = client.post("/api/library/marks", json={"article_id": 1, "starred": True, "note": "cite this"})
        assert res.status_code == 200
        assert res.json()["note"] == "cite this"


# ---------------------------------------------------------------------------
# GET /api/library/marks
# ---------------------------------------------------------------------------

class TestMarksList:
    def setup_marks(self, client: TestClient) -> None:
        """Populate marks for list tests."""
        client.post("/api/library/marks", json={"article_id": 1, "starred": True})
        client.post("/api/library/marks", json={"article_id": 2, "read": True})

    def test_list_all(self, client: TestClient) -> None:
        self.setup_marks(client)
        res = client.get("/api/library/marks")
        assert res.status_code == 200
        data = res.json()
        assert "marks" in data
        assert len(data["marks"]) >= 2

    def test_filter_starred(self, client: TestClient) -> None:
        self.setup_marks(client)
        res = client.get("/api/library/marks?starred=true")
        assert res.status_code == 200
        marks = res.json()["marks"]
        assert all(m["starred"] for m in marks)
        assert any(m["article_id"] == 1 for m in marks)

    def test_filter_read(self, client: TestClient) -> None:
        self.setup_marks(client)
        res = client.get("/api/library/marks?read=true")
        assert res.status_code == 200
        marks = res.json()["marks"]
        assert all(m["read"] for m in marks)

    def test_empty_db_returns_empty_list(self, client: TestClient) -> None:
        res = client.get("/api/library/marks?starred=true")
        assert res.status_code == 200
        assert res.json()["marks"] == []


# ---------------------------------------------------------------------------
# POST /api/library/articles/resolve_gaps
# ---------------------------------------------------------------------------

class TestResolveGaps:
    def test_returns_mapping(self, client: TestClient) -> None:
        res = client.post("/api/library/articles/resolve_gaps", json={"article_ids": [1, 2]})
        assert res.status_code == 200
        data = res.json()
        assert "mapping" in data
        assert data["mapping"]["1"] == ["IP1"]
        assert data["mapping"]["2"] == ["IP2"]

    def test_empty_list_returns_empty(self, client: TestClient) -> None:
        res = client.post("/api/library/articles/resolve_gaps", json={"article_ids": []})
        assert res.status_code == 200
        assert res.json()["mapping"] == {}

    def test_nonexistent_id_omitted(self, client: TestClient) -> None:
        res = client.post("/api/library/articles/resolve_gaps", json={"article_ids": [9999]})
        assert res.status_code == 200
        assert "9999" not in res.json()["mapping"]
