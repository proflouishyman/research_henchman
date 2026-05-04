"""Round-trip tests for ``GET /api/library/articles/search``.

The fixture DB mirrors the production schema closely (FTS5 virtual table
+ triggers) so search behaviour matches what the frontend will see.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as orchestrator_main


def _seed_db(db_path: Path) -> None:
    """Build a fixture DB with FTS5 mirror so search round-trips end to end."""
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

        CREATE VIRTUAL TABLE articles_fts USING fts5(
            title, authors, abstract, journal, gap_research_question,
            content='articles', content_rowid='id', tokenize='porter'
        );

        CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
            INSERT INTO articles_fts(rowid, title, authors, abstract, journal, gap_research_question)
            VALUES (new.id,
                    COALESCE(new.title, ''),
                    COALESCE(new.authors, ''),
                    COALESCE(new.abstract, ''),
                    COALESCE(new.journal, ''),
                    COALESCE(new.gap_research_question, ''));
        END;

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
        VALUES ('CP31', 0, 1, 'company_profile', 'Mercado Libre',
                'Mercado Libre history', 'What is Mercado Libre?', 200,
                'F', 'pulled', 'test'),
               ('CP1',  0, 1, 'company_profile', 'Amazon',
                'Amazon history', 'Bezos founded Amazon', 200,
                'F', 'pulled', 'test');
        """
    )

    rows = [
        # CP31 — Mercado Libre tier 3 with PDF.
        ("CP31", "ebsco_api", "Mercado Libre IPO Coverage", "Smith", "2007",
         "Detailed coverage of Mercado Libre IPO in Brazil and Argentina.",
         3, "Definitive primary source.",
         "https://example.com/x", "data/sample.pdf"),
        # CP31 — tier 2.
        ("CP31", "ebsco_api", "Latin America E-commerce 2010", "Jones", "2010",
         "Regional context for Mercado Libre and competitors.", 2,
         "Adjacent context.", "https://example.com/y", None),
        # CP31 — tier 0 noise (excluded by score_min=2).
        ("CP31", "ebsco_api", "Random Mercado Noise", "", "1990",
         "", 0, "False positive.", "https://example.com/z", None),
        # CP1 — Amazon tier 3 (Bezos hits the search).
        ("CP1", "ebsco_api", "Bezos and Amazon's Long-Term Strategy", "Brown", "2015",
         "Jeff Bezos' founder mentality and shareholder letters.", 3,
         "Definitive Bezos coverage.", "https://example.com/bezos",
         "data/bezos.pdf"),
        # CP1 — Amazon tier 2 from HathiTrust (no PDF).
        ("CP1", "hathitrust_fulltext", "Amazon: An Online Bookseller's Story",
         "Author Two", "2002",
         "Early days of Amazon, Bezos in Seattle warehouse.", 2,
         "Useful narrative source.", "/cgi/pt?id=99", None),
        # CP1 — Amazon tier 0 noise (Bezos misspelled to test relevance filter).
        ("CP1", "ebsco_api", "Bezzos Random Hit", "", "2001",
         "Random hit only matches if score_min=0.", 0, "Noise.",
         "https://example.com/n", None),
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
    """TestClient backed by a fresh fixture DB with FTS5 wired up."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    db_path = state / "article_index.sqlite"
    _seed_db(db_path)
    monkeypatch.setenv("ORCH_DATA_ROOT", str(state))
    return TestClient(orchestrator_main.app)


def test_search_basic_hit_returns_snippet(client_with_db):
    resp = client_with_db.get("/api/library/articles/search", params={"q": "Bezos"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    titles = [r["title"] for r in body["results"]]
    assert any("Bezos" in t for t in titles)
    # Snippet must wrap the hit in <mark>.
    assert any("<mark>" in r["snippet"] for r in body["results"])


def test_search_empty_query_400(client_with_db):
    # Required min_length=1 — FastAPI returns 422 for missing/empty.
    resp = client_with_db.get("/api/library/articles/search", params={"q": ""})
    assert resp.status_code in (400, 422)


def test_search_sanitizes_fts_special_chars(client_with_db):
    # "*+-^():" used in user query must not crash FTS5.
    resp = client_with_db.get("/api/library/articles/search", params={"q": "Bezos*+()"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1


def test_search_score_min_excludes_noise(client_with_db):
    # score_min=2 excludes the tier-0 "Bezzos Random Hit" but the term
    # 'Bezos' won't even match it (different spelling). Use 'Mercado'
    # which has both a tier-3 (CP31 IPO) and tier-0 noise.
    resp = client_with_db.get(
        "/api/library/articles/search",
        params={"q": "Mercado", "score_min": 2},
    )
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.json()["results"]]
    assert "Random Mercado Noise" not in titles
    assert any("Mercado" in t for t in titles)


def test_search_source_filter(client_with_db):
    resp = client_with_db.get(
        "/api/library/articles/search",
        params={"q": "Amazon", "source_id": "hathitrust_fulltext"},
    )
    assert resp.status_code == 200
    sources = {r["source_id"] for r in resp.json()["results"]}
    assert sources == {"hathitrust_fulltext"} or len(sources) == 0


def test_search_gap_id_filter(client_with_db):
    resp = client_with_db.get(
        "/api/library/articles/search",
        params={"q": "Amazon", "gap_id": "CP1"},
    )
    assert resp.status_code == 200
    gaps = {r["gap_id"] for r in resp.json()["results"]}
    assert gaps <= {"CP1"}


def test_search_has_pdf_filter(client_with_db):
    resp = client_with_db.get(
        "/api/library/articles/search",
        params={"q": "Bezos", "has_pdf": "true"},
    )
    assert resp.status_code == 200
    for r in resp.json()["results"]:
        assert r["pdf_path"]


def test_search_year_range(client_with_db):
    # Restrict to 2010+ — the 2007 Mercado IPO row should be excluded.
    resp = client_with_db.get(
        "/api/library/articles/search",
        params={"q": "Mercado", "year_from": 2010},
    )
    assert resp.status_code == 200
    for r in resp.json()["results"]:
        # Either the year extracted is >= 2010 or pub_date was empty.
        if r["pub_date"]:
            yr = int(r["pub_date"][:4])
            assert yr >= 2010


def test_search_pagination(client_with_db):
    resp = client_with_db.get(
        "/api/library/articles/search",
        params={"q": "Amazon", "limit": 1, "offset": 0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) <= 1
    # Total counts the full hit set, independent of limit.
    assert body["total"] >= 1


def test_search_url_absolutized_for_hathitrust(client_with_db):
    # CP1's HathiTrust row stores a path-only URL; result must be absolute.
    resp = client_with_db.get(
        "/api/library/articles/search",
        params={"q": "Bookseller"},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results
    hit = next((r for r in results if r["source_id"] == "hathitrust_fulltext"), None)
    assert hit is not None
    assert hit["url"].startswith("https://babel.hathitrust.org/")
