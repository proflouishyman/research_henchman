"""Round-trip tests for ``layers.dossier_render``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from layers.dossier_render import (
    SOURCE_PRIORITY,
    absolutize_url,
    assemble_dossier,
    build_cross_gap_index,
    chapter_slug,
    dedupe_within_gap,
    norm_title,
    pick_primary,
)


def _make_conn() -> sqlite3.Connection:
    """Build a fresh in-memory DB with the minimal schema the renderer needs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
    return conn


def _insert_article(conn: sqlite3.Connection, **kwargs) -> int:
    """Insert one article row using sensible defaults."""
    defaults = {
        "doi": None,
        "authors": "",
        "journal": "",
        "pub_date": "2020",
        "abstract": "",
        "url": "",
        "pdf_path": None,
        "run_id": "test_run",
        "gap_topic": "Test Chapter",
        "gap_research_question": "What is X?",
        "relevance_score": None,
        "relevance_why": "",
    }
    defaults.update(kwargs)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(":" + k for k in defaults.keys())
    cur = conn.execute(
        f"INSERT INTO articles ({cols}) VALUES ({placeholders})",
        defaults,
    )
    return cur.lastrowid


def _insert_gap_tree(conn: sqlite3.Connection, gap_id: str, **kwargs) -> None:
    defaults = {
        "parent_gap_id": None,
        "depth": 0,
        "tier": 1,
        "gap_type": "company_profile",
        "chapter": "Test Chapter",
        "heading_path": "Test Chapter",
        "claim_text": "Test claim",
        "research_question": "What is X?",
        "source_locator": "company_profile",
        "evidence_target": 200,
        "detector_pass": "F",
        "status": "pulled",
        "rationale": "test",
    }
    defaults.update(kwargs)
    defaults["gap_id"] = gap_id
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(":" + k for k in defaults.keys())
    conn.execute(
        f"INSERT INTO gap_tree ({cols}) VALUES ({placeholders})",
        defaults,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_norm_title_basic():
    assert norm_title("Hello, World!") == "hello world"
    assert norm_title("") == ""
    assert norm_title("ab") == ""  # too short
    assert norm_title("AB CD") == "ab cd"


def test_chapter_slug_basic():
    assert chapter_slug("Mercado Libre") == "mercado_libre"
    assert chapter_slug("") == "00_uncategorized"
    assert chapter_slug("Foo / Bar — Baz") == "foo_bar_baz"


def test_absolutize_url_path_only():
    assert absolutize_url("/cgi/pt?id=abc", "hathitrust_fulltext") == "https://babel.hathitrust.org/cgi/pt?id=abc"
    assert absolutize_url("/c/12345", "ebsco_api") == "https://research.ebsco.com/c/12345"
    assert absolutize_url("https://example.com/x", "ebsco_api") == "https://example.com/x"
    assert absolutize_url("", "ebsco_api") == ""


# ---------------------------------------------------------------------------
# pick_primary + dedupe_within_gap
# ---------------------------------------------------------------------------

def test_pick_primary_prefers_pdf():
    conn = _make_conn()
    a = _insert_article(conn, gap_id="G1", source_id="hathitrust_fulltext", title="X", pdf_path=None)
    b = _insert_article(conn, gap_id="G1", source_id="hathitrust_fulltext", title="X", pdf_path="data/x.pdf")
    rows = conn.execute("SELECT * FROM articles WHERE gap_id='G1'").fetchall()
    primary, others = pick_primary(rows)
    assert primary["id"] == b
    assert [o["id"] for o in others] == [a]


def test_pick_primary_source_priority_tiebreak():
    conn = _make_conn()
    # No PDFs anywhere — EBSCO should win source priority over HathiTrust.
    h = _insert_article(conn, gap_id="G1", source_id="hathitrust_fulltext", title="Y")
    e = _insert_article(conn, gap_id="G1", source_id="ebsco_api", title="Y")
    rows = conn.execute("SELECT * FROM articles WHERE gap_id='G1'").fetchall()
    primary, _ = pick_primary(rows)
    assert primary["id"] == e
    assert SOURCE_PRIORITY["ebsco_api"] < SOURCE_PRIORITY["hathitrust_fulltext"]


def test_dedupe_within_gap_groups_by_norm_title():
    conn = _make_conn()
    _insert_article(conn, gap_id="G1", source_id="ebsco_api", title="Hello, World!")
    _insert_article(conn, gap_id="G1", source_id="hathitrust_fulltext", title="hello world")
    _insert_article(conn, gap_id="G1", source_id="ebsco_api", title="Different Title")
    rows = conn.execute("SELECT * FROM articles WHERE gap_id='G1'").fetchall()
    consolidated = dedupe_within_gap(rows)
    # Two unique titles after norm.
    assert len(consolidated) == 2
    by_norm = {e["norm"]: e for e in consolidated}
    # The grouped one carries both source_ids.
    assert sorted(by_norm["hello world"]["sources"]) == ["ebsco_api", "hathitrust_fulltext"]


# ---------------------------------------------------------------------------
# build_cross_gap_index — only score >= 1
# ---------------------------------------------------------------------------

def test_build_cross_gap_index_filters_zero_scores():
    conn = _make_conn()
    _insert_article(conn, gap_id="G1", source_id="ebsco_api", title="Foundational Paper", relevance_score=2)
    _insert_article(conn, gap_id="G2", source_id="ebsco_api", title="Foundational Paper", relevance_score=3)
    _insert_article(conn, gap_id="G3", source_id="ebsco_api", title="Foundational Paper", relevance_score=0)
    _insert_article(conn, gap_id="G4", source_id="ebsco_api", title="Different", relevance_score=2)

    idx = build_cross_gap_index(conn)
    norm = norm_title("Foundational Paper")
    assert sorted(idx[norm]) == ["G1", "G2"]  # G3 excluded (score=0), G4 different title.


# ---------------------------------------------------------------------------
# assemble_dossier — round-trip
# ---------------------------------------------------------------------------

def test_assemble_dossier_round_trip():
    conn = _make_conn()
    _insert_gap_tree(conn, "CP1", chapter="Acme Inc.", claim_text="Acme history",
                     research_question="What is Acme's history?", evidence_target=200)
    a = _insert_article(
        conn, gap_id="CP1", source_id="ebsco_api",
        title="Acme: A History", authors="Smith", pub_date="2020",
        abstract="An overview of Acme.", relevance_score=3,
        relevance_why="Definitive corporate history.",
        url="https://example.com/a", pdf_path="data/a.pdf", doi="10.1234/abc",
    )
    _insert_article(
        conn, gap_id="CP1", source_id="hathitrust_fulltext",
        title="Acme A History",  # fuzzy duplicate
        pub_date="2020",
        relevance_score=3,
        relevance_why="HathiTrust copy of same.",
        url="/cgi/pt?id=42",
    )
    _insert_article(
        conn, gap_id="CP1", source_id="ebsco_api",
        title="Tangential Mention", relevance_score=1,
        relevance_why="Mentions Acme briefly.",
    )
    _insert_article(
        conn, gap_id="CP1", source_id="ebsco_api",
        title="Noise Result", relevance_score=0,
    )
    # A second gap to populate cross-gap refs for "Acme: A History".
    _insert_gap_tree(conn, "CP2", chapter="Acme Inc.")
    _insert_article(
        conn, gap_id="CP2", source_id="ebsco_api",
        title="Acme: A History", relevance_score=2,
    )

    dossier = assemble_dossier(conn, "CP1")

    # Header carries gap_tree fields.
    assert dossier["gap"]["gap_id"] == "CP1"
    assert dossier["gap"]["chapter"] == "Acme Inc."
    assert dossier["gap"]["evidence_target"] == 200
    assert dossier["gap"]["tier"] == 1

    # Summary counts: 4 raw rows, 3 consolidated entries (the two
    # "Acme: A History" rows merge into one).
    assert dossier["summary"]["total_rows"] == 4
    assert dossier["summary"]["consolidated"] == 3
    assert dossier["summary"]["tier_counts"]["3"] == 1
    assert dossier["summary"]["tier_counts"]["1"] == 1
    assert dossier["summary"]["tier_counts"]["0"] == 1

    # Tier 3 entry merges sources and carries cross_gap_refs to CP2.
    tier3 = dossier["tiers"]["3"]
    assert len(tier3) == 1
    entry = tier3[0]
    assert entry["title"] == "Acme: A History"
    assert entry["source_id"] == "ebsco_api"  # primary picked by source priority + has-PDF
    assert entry["pdf_path"] == "data/a.pdf"
    assert "hathitrust_fulltext" in entry["also_in_sources"]
    assert entry["cross_gap_refs"] == ["CP2"]
    # WHY text propagated from primary row.
    assert entry["relevance_why"] == "Definitive corporate history."


def test_assemble_dossier_legacy_gap_without_gap_tree_row():
    """Legacy AUTO-NN-G1 gaps live only in articles; assembly must still work."""
    conn = _make_conn()
    _insert_article(
        conn, gap_id="AUTO-01-G1", source_id="ebsco_api",
        title="Legacy Source", relevance_score=2,
        gap_topic="Legacy Chapter", gap_research_question="Legacy Q?",
    )
    dossier = assemble_dossier(conn, "AUTO-01-G1")
    assert dossier["gap"]["gap_id"] == "AUTO-01-G1"
    # Falls back to articles.gap_topic when gap_tree row is missing.
    assert dossier["gap"]["chapter"] == "Legacy Chapter"
    assert dossier["gap"]["research_question"] == "Legacy Q?"
    assert dossier["summary"]["total_rows"] == 1
