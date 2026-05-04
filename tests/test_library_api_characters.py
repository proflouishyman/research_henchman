"""Round-trip tests for ``GET /api/library/characters``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as orchestrator_main


def _seed_db(db_path: Path) -> None:
    """Build a fixture DB with two company-profile gaps + one non-character gap."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            doi TEXT,
            title TEXT NOT NULL,
            authors TEXT,
            journal TEXT,
            pub_date TEXT,
            abstract TEXT,
            url TEXT,
            pdf_path TEXT,
            run_id TEXT,
            gap_id TEXT,
            source_id TEXT,
            gap_topic TEXT,
            gap_research_question TEXT,
            relevance_score INTEGER,
            relevance_why TEXT
        );

        CREATE TABLE gap_tree (
            gap_id TEXT PRIMARY KEY,
            parent_gap_id TEXT,
            depth INTEGER NOT NULL,
            tier INTEGER NOT NULL,
            gap_type TEXT NOT NULL,
            chapter TEXT,
            heading_path TEXT,
            claim_text TEXT,
            research_question TEXT,
            source_locator TEXT,
            evidence_target INTEGER NOT NULL,
            detector_pass TEXT,
            status TEXT,
            rationale TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO gap_tree (gap_id, depth, tier, gap_type, chapter, claim_text,
                              research_question, evidence_target,
                              detector_pass, status, rationale)
        VALUES
          ('CP_A', 0, 1, 'company_profile', 'Amazon', 'Amazon claim',
           'rq', 200, 'F', 'pulled', 'Empty section: needs primary source'),
          ('CP_B', 0, 1, 'company_profile', 'Mercado',
           'Mercado claim', 'rq', 100, 'F', 'pulled',
           'Thin section: only one tier-3 hit'),
          ('IP1',  0, 1, 'intro_promise', 'Intro', 'Intro promise',
           'rq', 60, 'A', 'pending', 'unrelated');
        """
    )

    rows = [
        # CP_A: 2 tier-3 hits.
        ("CP_A", "ebsco_api", "Amazon Annual Report 2010", 3),
        ("CP_A", "ebsco_api", "Amazon Founder Bezos Bio", 3),
        ("CP_A", "ebsco_api", "Adjacent Amazon piece", 2),
        # CP_B: 1 tier-3 hit.
        ("CP_B", "ebsco_api", "Mercado Libre IPO Coverage", 3),
        ("CP_B", "ebsco_api", "Latin America 2010", 1),
        # IP1: not a character — should not appear.
        ("IP1", "ebsco_api", "Random Intro Item", 2),
    ]
    for gap_id, source_id, title, score in rows:
        conn.execute(
            """INSERT INTO articles (gap_id, source_id, title, run_id,
                                     relevance_score, gap_topic,
                                     gap_research_question)
                  VALUES (?, ?, ?, 'rTest', ?, '', '')""",
            (gap_id, source_id, title, score),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def client_with_db(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    db_path = state / "article_index.sqlite"
    _seed_db(db_path)
    monkeypatch.setenv("ORCH_DATA_ROOT", str(state))
    return TestClient(orchestrator_main.app)


def test_characters_returns_only_company_profile_gaps(client_with_db):
    resp = client_with_db.get("/api/library/characters")
    assert resp.status_code == 200
    chars = resp.json()["characters"]
    ids = {c["gap_id"] for c in chars}
    assert ids == {"CP_A", "CP_B"}


def test_characters_sorted_by_tier3_count_desc(client_with_db):
    resp = client_with_db.get("/api/library/characters")
    chars = resp.json()["characters"]
    # CP_A has 2 tier-3 hits, CP_B has 1.
    assert [c["gap_id"] for c in chars] == ["CP_A", "CP_B"]


def test_characters_includes_top_tier3_titles(client_with_db):
    resp = client_with_db.get("/api/library/characters")
    chars = {c["gap_id"]: c for c in resp.json()["characters"]}
    cp_a = chars["CP_A"]
    assert len(cp_a["top_tier3_titles"]) == 2
    assert "Amazon Annual Report 2010" in cp_a["top_tier3_titles"]
    cp_b = chars["CP_B"]
    assert cp_b["top_tier3_titles"] == ["Mercado Libre IPO Coverage"]


def test_characters_tier_histogram_matches_counts(client_with_db):
    resp = client_with_db.get("/api/library/characters")
    chars = {c["gap_id"]: c for c in resp.json()["characters"]}
    hist = chars["CP_A"]["tier_histogram"]
    # 2 tier-3 + 1 tier-2 + 0 elsewhere = same shape as tier_counts.
    assert hist["3"] == 2
    assert hist["2"] == 1
    assert hist["1"] == 0
    assert hist["0"] == 0
    # tier_histogram must equal tier_counts (alias).
    assert hist == chars["CP_A"]["tier_counts"]


def test_characters_handles_empty_corpus(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    db_path = state / "article_index.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            run_id TEXT,
            gap_id TEXT,
            source_id TEXT,
            relevance_score INTEGER
        );
        CREATE TABLE gap_tree (
            gap_id TEXT PRIMARY KEY,
            parent_gap_id TEXT,
            depth INTEGER NOT NULL,
            tier INTEGER NOT NULL,
            gap_type TEXT NOT NULL,
            chapter TEXT,
            heading_path TEXT,
            claim_text TEXT,
            research_question TEXT,
            source_locator TEXT,
            evidence_target INTEGER NOT NULL,
            detector_pass TEXT,
            status TEXT,
            rationale TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("ORCH_DATA_ROOT", str(state))
    client = TestClient(orchestrator_main.app)
    resp = client.get("/api/library/characters")
    assert resp.status_code == 200
    assert resp.json() == {"characters": []}
