"""Round-trip tests for the writing-companion library API."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as orchestrator_main


def _seed_db(db_path: Path) -> None:
    """Build a small fixture DB exercising all library endpoints."""
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
        """
    )
    # Two gaps in two different chapters.
    conn.execute(
        """INSERT INTO gap_tree (gap_id, depth, tier, gap_type, chapter,
                                 claim_text, research_question, evidence_target,
                                 detector_pass, status, rationale)
              VALUES ('CP31', 0, 1, 'company_profile', 'Mercado Libre',
                      'Mercado Libre history', 'What is Mercado Libre?', 200,
                      'F', 'pulled', 'test')"""
    )
    conn.execute(
        """INSERT INTO gap_tree (gap_id, depth, tier, gap_type, chapter,
                                 claim_text, research_question, evidence_target,
                                 detector_pass, status, rationale)
              VALUES ('IP1', 0, 1, 'intro_promise', 'Introduction',
                      'Intro promise about FedEx', 'How is FedEx introduced?', 60,
                      'A', 'pending', 'test')"""
    )

    rows = [
        # Tier 3 article in CP31 with PDF.
        ("CP31", "ebsco_api", "Mercado Libre IPO Coverage", "Smith", "2007",
         "Detailed coverage of MELI IPO.", 3, "Definitive primary source.",
         "https://example.com/x", "data/sample.pdf"),
        # Tier 3 dup of same title from another source.
        ("CP31", "hathitrust_fulltext", "mercado libre ipo coverage", "", "2007",
         "", 3, "OCR copy of same.", "/cgi/pt?id=42", None),
        # Tier 2 article.
        ("CP31", "ebsco_api", "Latin America E-commerce 2010", "Jones", "2010",
         "Regional context.", 2, "Adjacent context.",
         "https://example.com/y", None),
        # Tier 0 noise.
        ("CP31", "ebsco_api", "Random Noise Result", "", "1990",
         "", 0, "False positive.", "https://example.com/z", None),
        # IP1 article (different gap).
        ("IP1", "ebsco_api", "FedEx Corporate History", "Brown", "2015",
         "FedEx overview.", 2, "Tangential.", "https://example.com/fe", None),
    ]
    for r in rows:
        conn.execute(
            """INSERT INTO articles (gap_id, source_id, title, authors, pub_date,
                                     abstract, relevance_score, relevance_why,
                                     url, pdf_path, run_id, gap_topic,
                                     gap_research_question)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'rTest', ?, '')""",
            (*r, ""),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def client_with_db(tmp_path, monkeypatch):
    """Spin up a TestClient backed by a fresh fixture DB."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    db_path = state / "article_index.sqlite"
    _seed_db(db_path)
    monkeypatch.setenv("ORCH_DATA_ROOT", str(state))
    return TestClient(orchestrator_main.app)


def test_index_endpoint_groups_by_chapter(client_with_db):
    resp = client_with_db.get("/api/library/index")
    assert resp.status_code == 200
    payload = resp.json()
    chapters = {c["title"]: c for c in payload["chapters"]}
    assert "Mercado Libre" in chapters
    assert "Introduction" in chapters
    assert chapters["Mercado Libre"]["gap_count"] == 1
    cp31 = chapters["Mercado Libre"]["gaps"][0]
    assert cp31["gap_id"] == "CP31"
    # Article counts are joined into the gap row.
    assert cp31["total_rows"] == 4  # 4 CP31 articles
    assert cp31["tier_counts"]["3"] == 2
    assert cp31["tier_counts"]["2"] == 1
    assert cp31["tier_counts"]["0"] == 1
    assert payload["corpus_total_rows"] == 5  # all articles
    assert "ebsco_api" in payload["sources"]


def test_gaps_filter_by_chapter(client_with_db):
    resp = client_with_db.get("/api/library/gaps", params={"chapter": "Mercado Libre"})
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["gaps"]) == 1
    assert payload["gaps"][0]["gap_id"] == "CP31"


def test_gaps_filter_by_gap_type_csv(client_with_db):
    resp = client_with_db.get(
        "/api/library/gaps",
        params={"gap_type": "intro_promise,research_gap"},
    )
    assert resp.status_code == 200
    gaps = resp.json()["gaps"]
    assert {g["gap_id"] for g in gaps} == {"IP1"}


def test_single_gap_endpoint(client_with_db):
    resp = client_with_db.get("/api/library/gaps/CP31")
    assert resp.status_code == 200
    gap = resp.json()
    assert gap["gap_id"] == "CP31"
    assert gap["evidence_target"] == 200
    assert gap["tier_counts"]["3"] == 2


def test_single_gap_404(client_with_db):
    resp = client_with_db.get("/api/library/gaps/DOES_NOT_EXIST")
    assert resp.status_code == 404


def test_dossier_endpoint_round_trip(client_with_db):
    resp = client_with_db.get("/api/library/gaps/CP31/dossier")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gap"]["gap_id"] == "CP31"
    assert body["gap"]["chapter"] == "Mercado Libre"
    # 4 raw rows, 3 consolidated (the two MELI IPO rows merge).
    assert body["summary"]["total_rows"] == 4
    assert body["summary"]["consolidated"] == 3
    tier3 = body["tiers"]["3"]
    assert len(tier3) == 1  # merged
    assert tier3[0]["pdf_path"] == "data/sample.pdf"
    assert "hathitrust_fulltext" in tier3[0]["also_in_sources"]
    assert tier3[0]["relevance_why"] == "Definitive primary source."


def test_dossier_endpoint_404_for_unknown_gap(client_with_db):
    resp = client_with_db.get("/api/library/gaps/MISSING/dossier")
    assert resp.status_code == 404
