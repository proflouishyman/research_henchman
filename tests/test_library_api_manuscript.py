"""Tests for the manuscript reader API endpoints.

Uses the TestClient + a mocked parse_manuscript so tests don't require
the live manuscript file on disk.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App import (with overridden DB path)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fixture_db_path(tmp_path: Path) -> Path:
    """Create a minimal SQLite DB with gap_tree table."""
    db = tmp_path / "article_index.sqlite"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE gap_tree (
            gap_id TEXT PRIMARY KEY,
            parent_gap_id TEXT,
            depth INTEGER DEFAULT 0,
            tier INTEGER DEFAULT 1,
            gap_type TEXT DEFAULT 'explicit',
            chapter TEXT,
            heading_path TEXT,
            claim_text TEXT,
            research_question TEXT,
            source_locator TEXT,
            evidence_target INTEGER DEFAULT 0,
            detector_pass TEXT,
            status TEXT DEFAULT 'pending',
            rationale TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "INSERT INTO gap_tree (gap_id, chapter, heading_path, claim_text, detector_pass) "
        "VALUES ('IP1', 'Introduction', 'Introduction > The Revolution', 'trust as ancient problem', 'A')"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS articles (id INTEGER PRIMARY KEY, gap_id TEXT, relevance_score INTEGER, source_id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS user_marks (article_id INTEGER PRIMARY KEY, starred INTEGER DEFAULT 0, read INTEGER DEFAULT 0, note TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def client(fixture_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with DB path overridden to the fixture DB."""
    import os
    monkeypatch.setenv("ORCH_DATA_ROOT", str(fixture_db_path.parent))

    from main import app
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Mock paragraph data
# ---------------------------------------------------------------------------

_MOCK_PARAS = [
    {
        "para_id": "aabbcc112233",
        "chapter": "Introduction",
        "heading_path": "Introduction > The Revolution",
        "text": "The revolution began in 1994.",
        "is_heading": False,
        "heading_level": 0,
        "footnote_count": 2,
        "bracketed_todos": [],
        "char_offset": 0,
    },
    {
        "para_id": "ddeeff445566",
        "chapter": "Introduction",
        "heading_path": "Introduction > The Revolution",
        "text": "[ADD DATA ON MARKET SIZE] More context follows.",
        "is_heading": False,
        "heading_level": 0,
        "footnote_count": 0,
        "bracketed_todos": ["ADD DATA ON MARKET SIZE"],
        "char_offset": 30,
    },
]

_MOCK_CHAPTERS = [
    {
        "title": "Introduction",
        "slug": "introduction",
        "sections": [
            {
                "heading": "The Revolution",
                "paragraphs": [
                    {
                        "para_id": "aabbcc112233",
                        "text": "The revolution began in 1994.",
                        "is_heading": False,
                        "heading_level": 0,
                        "footnote_count": 2,
                        "bracketed_todos": [],
                        "gap_ids": ["IP1"],
                    },
                    {
                        "para_id": "ddeeff445566",
                        "text": "[ADD DATA ON MARKET SIZE] More context follows.",
                        "is_heading": False,
                        "heading_level": 0,
                        "footnote_count": 0,
                        "bracketed_todos": ["ADD DATA ON MARKET SIZE"],
                        "gap_ids": [],
                    },
                ],
            }
        ],
    }
]


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------

class TestManuscriptStructure:
    def test_returns_chapters_key(self, client: TestClient) -> None:
        """GET /api/library/manuscript/structure should return {chapters: [...]}."""
        with patch("layers.manuscript_parse.parse_manuscript", return_value=_MOCK_PARAS), \
             patch("layers.manuscript_parse.paragraph_gap_links", return_value={"aabbcc112233": ["IP1"]}), \
             patch("layers.manuscript_parse.group_into_chapters", return_value=_MOCK_CHAPTERS):
            res = client.get("/api/library/manuscript/structure")
        assert res.status_code == 200
        data = res.json()
        assert "chapters" in data
        assert isinstance(data["chapters"], list)

    def test_chapter_has_sections(self, client: TestClient) -> None:
        with patch("layers.manuscript_parse.parse_manuscript", return_value=_MOCK_PARAS), \
             patch("layers.manuscript_parse.paragraph_gap_links", return_value={"aabbcc112233": ["IP1"]}), \
             patch("layers.manuscript_parse.group_into_chapters", return_value=_MOCK_CHAPTERS):
            res = client.get("/api/library/manuscript/structure")
        data = res.json()
        chapter = data["chapters"][0]
        assert "sections" in chapter
        assert len(chapter["sections"]) > 0

    def test_paragraph_has_gap_ids(self, client: TestClient) -> None:
        with patch("layers.manuscript_parse.parse_manuscript", return_value=_MOCK_PARAS), \
             patch("layers.manuscript_parse.paragraph_gap_links", return_value={"aabbcc112233": ["IP1"]}), \
             patch("layers.manuscript_parse.group_into_chapters", return_value=_MOCK_CHAPTERS):
            res = client.get("/api/library/manuscript/structure")
        data = res.json()
        paras = data["chapters"][0]["sections"][0]["paragraphs"]
        first_para = paras[0]
        assert "gap_ids" in first_para
        assert "IP1" in first_para["gap_ids"]

    def test_missing_docx_returns_404(self, client: TestClient) -> None:
        res = client.get("/api/library/manuscript/structure?docx=/nonexistent/path/file.docx")
        assert res.status_code == 404


class TestManuscriptParagraph:
    def test_returns_paragraph_detail(self, client: TestClient) -> None:
        with patch("layers.manuscript_parse.parse_manuscript", return_value=_MOCK_PARAS), \
             patch("layers.manuscript_parse.paragraph_gap_links", return_value={"aabbcc112233": ["IP1"]}):
            res = client.get("/api/library/manuscript/paragraph/aabbcc112233")
        assert res.status_code == 200
        data = res.json()
        assert data["para_id"] == "aabbcc112233"
        assert "gap_ids" in data
        assert "gap_rows" in data

    def test_missing_para_id_returns_404(self, client: TestClient) -> None:
        with patch("layers.manuscript_parse.parse_manuscript", return_value=_MOCK_PARAS), \
             patch("layers.manuscript_parse.paragraph_gap_links", return_value={}):
            res = client.get("/api/library/manuscript/paragraph/zzznotexist")
        assert res.status_code == 404
