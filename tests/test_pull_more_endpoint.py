"""Tests for Phase 3 pull-more / pull-status endpoints.

Tests the pull_jobs lifecycle and both API endpoints using FastAPI's
TestClient with an in-process SQLite DB (no live LLM or network needed).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# App bootstrap — mirrors conftest.py pattern
# ---------------------------------------------------------------------------

@pytest.fixture()
def test_db(tmp_path: Path) -> Path:
    """Create a minimal article_index.sqlite with gap_tree + pull_jobs tables."""
    from adapters.article_index import open_index
    db_path = tmp_path / "article_index.sqlite"
    conn = open_index(db_path)

    # Minimal gap_tree row for testing.
    try:
        conn.execute(
            """INSERT OR IGNORE INTO gap_tree
               (gap_id, parent_gap_id, depth, tier, gap_type, chapter,
                heading_path, claim_text, research_question, source_locator,
                evidence_target, detector_pass, status, rationale)
               VALUES (?, NULL, 0, 1, 'research_gap', 'Test Chapter',
                       'test', 'Test claim text for gap CP99', '',
                       '', 5, 'wave2', 'pending', '')""",
            ("CP99",),
        )
    except Exception:
        pass
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def client(test_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return a TestClient with the router pointing at the test DB."""
    monkeypatch.setenv("ORCH_DATA_ROOT", str(test_db.parent))
    from main import app
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# pull_jobs lifecycle helpers
# ---------------------------------------------------------------------------

def _insert_job(db_path: Path, gap_id: str, run_id: str, status: str) -> None:
    """Insert a synthetic pull_jobs row for testing."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pull_jobs (
               gap_id TEXT NOT NULL, run_id TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'running',
               started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               finished_at TEXT, records_pulled INTEGER DEFAULT 0,
               sources_used TEXT, errors TEXT,
               PRIMARY KEY (gap_id, run_id))"""
    )
    conn.execute(
        "INSERT INTO pull_jobs (gap_id, run_id, status) VALUES (?,?,?)",
        (gap_id, run_id, status),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests: /api/library/gaps/{gap_id}/pull-status
# ---------------------------------------------------------------------------

class TestPullStatus:
    def test_404_when_no_jobs(self, client: TestClient) -> None:
        """Pull-status returns 404 for a gap with no history."""
        resp = client.get("/api/library/gaps/CP99/pull-status")
        assert resp.status_code == 404

    def test_returns_latest_job(self, client: TestClient, test_db: Path) -> None:
        """Pull-status returns the latest job row."""
        _insert_job(test_db, "CP99", "run_abc123", "done")
        resp = client.get("/api/library/gaps/CP99/pull-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gap_id"] == "CP99"
        assert data["run_id"] == "run_abc123"
        assert data["status"] == "done"

    def test_running_job_status(self, client: TestClient, test_db: Path) -> None:
        """Running job returns status='running'."""
        _insert_job(test_db, "CP99", "run_running1", "running")
        resp = client.get("/api/library/gaps/CP99/pull-status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"


# ---------------------------------------------------------------------------
# Tests: POST /api/library/gaps/{gap_id}/pull-more
# ---------------------------------------------------------------------------

class TestPullMore:
    def test_starts_job_and_returns_run_id(
        self, client: TestClient, test_db: Path
    ) -> None:
        """Pull-more creates a pull_jobs row and returns {run_id, status:'started'}."""
        # Patch the background task so it doesn't actually run.
        with patch("routers.library._run_pull_in_background"):
            resp = client.post("/api/library/gaps/CP99/pull-more")

        assert resp.status_code == 200
        data = resp.json()
        assert "run_id" in data
        assert data["status"] == "started"
        assert data["run_id"].startswith("pullmore_")

    def test_409_when_already_running(
        self, client: TestClient, test_db: Path
    ) -> None:
        """Posting pull-more while a job is running returns 409."""
        _insert_job(test_db, "CP99", "run_already_running", "running")
        with patch("routers.library._run_pull_in_background"):
            resp = client.post("/api/library/gaps/CP99/pull-more")
        assert resp.status_code == 409

    def test_allows_new_job_after_done(
        self, client: TestClient, test_db: Path
    ) -> None:
        """A new pull can be started once the previous job is 'done'."""
        _insert_job(test_db, "CP99", "run_done_old", "done")
        with patch("routers.library._run_pull_in_background"):
            resp = client.post("/api/library/gaps/CP99/pull-more")
        assert resp.status_code == 200

    def test_job_appears_in_status_after_start(
        self, client: TestClient, test_db: Path
    ) -> None:
        """After pull-more, pull-status returns the new running job."""
        with patch("routers.library._run_pull_in_background"):
            post_resp = client.post("/api/library/gaps/CP99/pull-more")
        assert post_resp.status_code == 200
        run_id = post_resp.json()["run_id"]

        get_resp = client.get("/api/library/gaps/CP99/pull-status")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["run_id"] == run_id
        assert data["status"] == "running"
