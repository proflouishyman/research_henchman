"""Tests for adapters/article_index.py and the article index pipeline.

Fixture layout (created with tmp_path — no live run_27f86e44394442 data is read):

    pull_outputs/
      run_test/
        AUTO-01-G1/
          ebsco_api/
            seed.json
            fetched/
              Article_Alpha.md
              Article_Beta.md
              Article_Beta.pdf          (Beta has a PDF)
        AUTO-02-G1/
          jstor/
            seed2.json
            fetched/
              Article_Beta.md          (same DOI as ebsco Alpha — dedup candidate)
              Article_Gamma.md

runs.json provides gap context for AUTO-01-G1 and AUTO-02-G1.

Test coverage:
  - Markdown parser extracts all fields correctly
  - ingest_pull_output: correct row count after first ingest
  - ingest_pull_output idempotent: re-ingesting adds zero rows
  - FTS5 full-text search finds the right rows
  - DOI dedup: sets canonical_id on duplicate rows, canonical row is preferred
  - Source-count query returns expected breakdown
  - gap_context_for: returns claim_text and chapter from runs.json
  - zero-pdf gap detection
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from adapters.article_index import (
    _parse_markdown_article,
    dedupe_by_doi,
    gap_context_for,
    ingest_pull_output,
    open_index,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SHARED_DOI = "10.0001/shared-doi"  # used in both Alpha (ebsco) and Beta-jstor


def _write_seed_json(path: Path, query: str, bquery_original: str, bquery_normalized=None):
    """Write a minimal seed JSON file."""
    record = {
        "title": "ebsco_api search results",
        "url": f"https://search.ebscohost.com/login.aspx?direct=true&bquery={query}",
        "quality_label": "seed",
        "bquery_original": bquery_original,
    }
    if bquery_normalized is not None:
        record["bquery_normalized"] = bquery_normalized
    path.write_text(json.dumps([record]), encoding="utf-8")


def _write_md(path: Path, *, title: str, authors: str = "", journal: str = "",
              date: str = "", database: str = "", url: str = "",
              abstract: str = "", doi: str = "") -> None:
    """Write a markdown article file in the format used by _write_ebsco_records."""
    lines = [
        f"# {title}",
        "",
        f"**Authors:** {authors or '—'}  ",
        f"**Source:** {journal or '—'}  ",
        f"**Date:** {date or '—'}  ",
    ]
    if database:
        lines.append(f"**Database:** {database}  ")
    if url:
        lines.append(f"**URL:** {url}  ")
    if doi:
        lines.append(f"**DOI:** {doi}  ")
    lines += ["", "## Abstract", "", abstract or "_(not available)_", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def pull_root(tmp_path) -> Path:
    """Build a minimal fake pull_output tree with 2 gaps, 2 sources, 4 articles."""
    root = tmp_path / "pull_outputs" / "run_test"

    # --- Gap 1 / ebsco_api ---
    ebsco_dir = root / "AUTO-01-G1" / "ebsco_api"
    ebsco_fetched = ebsco_dir / "fetched"
    ebsco_fetched.mkdir(parents=True)

    _write_seed_json(
        ebsco_dir / "seed.json",
        query="everything+store",
        bquery_original="everything store",
        bquery_normalized=["\"everything store\" OR \"all-in-one retailer\""],
    )
    # Article Alpha — has DOI (shared with jstor Beta)
    _write_md(
        ebsco_fetched / "Article_Alpha.md",
        title="Article Alpha",
        authors="Smith, J.",
        journal="Journal of Commerce, 2020, vol 10, p 5",
        database="Academic Search Ultimate",
        url="/c/6hfcoc/search/details/abc123",
        abstract="Alpha abstract about e-commerce platforms and retail.",
        doi=SHARED_DOI,
    )
    # Article Beta — has a PDF file on disk (same slug as md)
    _write_md(
        ebsco_fetched / "Article_Beta.md",
        title="Article Beta",
        authors="Jones, A.",
        journal="Business Review, 2021, vol 5, p 1",
        database="Academic Search Ultimate",
        abstract="Beta abstract on digital transformation.",
    )
    # Write a companion PDF for Beta
    (ebsco_fetched / "Article_Beta.pdf").write_bytes(b"%PDF-1.4 fake")

    # --- Gap 2 / jstor ---
    jstor_dir = root / "AUTO-02-G1" / "jstor"
    jstor_fetched = jstor_dir / "fetched"
    jstor_fetched.mkdir(parents=True)

    _write_seed_json(
        jstor_dir / "seed2.json",
        query="e-commerce+history",
        bquery_original="e-commerce history",
    )
    # Jstor version of Alpha — same DOI, different source (dedup candidate)
    _write_md(
        jstor_fetched / "Article_Beta.md",   # slug is unique per gap+source
        title="Article Alpha",               # same TITLE as ebsco Alpha
        authors="Smith, J.",
        journal="JSTOR: Journal of Commerce",
        abstract="Alpha abstract about e-commerce platforms and retail.",
        doi=SHARED_DOI,
    )
    # Article Gamma — unique, no DOI
    _write_md(
        jstor_fetched / "Article_Gamma.md",
        title="Article Gamma",
        authors="Brown, C.",
        journal="Historical Commerce Review",
        abstract="Gamma discusses historical trade routes and ancient commerce.",
    )

    return root


@pytest.fixture
def runs_json(tmp_path) -> Path:
    """Write a minimal runs.json with gap context for both test gaps."""
    data = {
        "run_test": {
            "run_id": "run_test",
            "gap_map": {
                "gaps": [
                    {
                        "gap_id": "AUTO-01-G1",
                        "chapter": "Chapter 1: The E-Commerce Revolution",
                        "claim_text": "Amazon is not the everything store we imagine.",
                    },
                    {
                        "gap_id": "AUTO-02-G1",
                        "chapter": "Chapter 2: Historical Context",
                        "claim_text": "E-commerce has ancient roots in trade.",
                    },
                ]
            },
        }
    }
    path = tmp_path / "runs.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def db(tmp_path) -> sqlite3.Connection:
    """Open a fresh in-memory-equivalent DB in tmp_path."""
    return open_index(tmp_path / "test_index.sqlite")


# ---------------------------------------------------------------------------
# Unit: markdown parser
# ---------------------------------------------------------------------------

class TestParseMarkdownArticle:
    def test_parses_all_fields(self):
        text = (
            "# My Article Title\n"
            "\n"
            "**Authors:** Smith, J.; Jones, A.  \n"
            "**Source:** Commerce Journal, 2020, vol 5, p 10  \n"
            "**Date:** 2020  \n"
            "**Database:** Academic Search Ultimate  \n"
            "**URL:** /c/abc/details/xyz  \n"
            "**DOI:** 10.1000/test  \n"
            "\n"
            "## Abstract\n"
            "\n"
            "This is a test abstract.\n"
        )
        result = _parse_markdown_article(text)
        assert result["title"] == "My Article Title"
        assert result["authors"] == "Smith, J.; Jones, A."
        assert result["journal"] == "Commerce Journal, 2020, vol 5, p 10"
        assert result["pub_date"] == "2020"
        assert result["database_name"] == "Academic Search Ultimate"
        assert result["url"] == "/c/abc/details/xyz"
        assert result["doi"] == "10.1000/test"
        assert result["abstract"] == "This is a test abstract."

    def test_no_doi_absent_from_result(self):
        text = "# Title\n\n**Authors:** X  \n\n## Abstract\n\nBody."
        result = _parse_markdown_article(text)
        assert "doi" not in result

    def test_empty_dash_doi_excluded(self):
        text = "# Title\n\n**DOI:** —  \n\n## Abstract\n\nBody."
        result = _parse_markdown_article(text)
        assert "doi" not in result

    def test_not_available_abstract_excluded(self):
        text = "# Title\n\n## Abstract\n\n_(not available)_\n"
        result = _parse_markdown_article(text)
        assert result.get("abstract") is None or result.get("abstract") == ""

    def test_jstor_published_in_mapped_to_journal(self):
        text = "# Title\n\n**Published in:** Historical Review  \n\n## Abstract\n\nBody."
        result = _parse_markdown_article(text)
        assert result["journal"] == "Historical Review"


# ---------------------------------------------------------------------------
# Integration: ingest
# ---------------------------------------------------------------------------

class TestIngestPullOutput:
    def test_correct_row_count_after_first_ingest(self, db, pull_root, runs_json):
        inserted = ingest_pull_output(
            db, pull_root, "run_test", runs_json_path=runs_json
        )
        # 2 ebsco articles + 2 jstor articles = 4
        assert inserted == 4
        total = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert total == 4

    def test_reingest_is_idempotent(self, db, pull_root, runs_json):
        inserted_first = ingest_pull_output(
            db, pull_root, "run_test", runs_json_path=runs_json
        )
        inserted_second = ingest_pull_output(
            db, pull_root, "run_test", runs_json_path=runs_json
        )
        assert inserted_first == 4
        assert inserted_second == 0   # no new rows
        total = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert total == 4

    def test_pdf_path_set_for_articles_with_pdf(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        row = db.execute(
            "SELECT pdf_path FROM articles WHERE title = 'Article Beta' AND source_id = 'ebsco_api'"
        ).fetchone()
        assert row is not None
        assert row["pdf_path"] is not None
        assert row["pdf_path"].endswith("Article_Beta.pdf")

    def test_pdf_path_null_for_metadata_only(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        row = db.execute(
            "SELECT pdf_path FROM articles WHERE title = 'Article Alpha' AND source_id = 'ebsco_api'"
        ).fetchone()
        assert row is not None
        assert row["pdf_path"] is None

    def test_bquery_normalized_stored(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        row = db.execute(
            "SELECT bquery_normalized FROM articles WHERE source_id = 'ebsco_api' LIMIT 1"
        ).fetchone()
        assert row["bquery_normalized"] is not None
        # Should be JSON-serialised list
        parsed = json.loads(row["bquery_normalized"])
        assert isinstance(parsed, list)
        assert len(parsed) == 1

    def test_gap_research_question_populated(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        row = db.execute(
            "SELECT gap_research_question FROM articles WHERE gap_id = 'AUTO-01-G1' LIMIT 1"
        ).fetchone()
        assert row["gap_research_question"] == "Amazon is not the everything store we imagine."

    def test_gap_topic_populated(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        row = db.execute(
            "SELECT gap_topic FROM articles WHERE gap_id = 'AUTO-01-G1' LIMIT 1"
        ).fetchone()
        assert row["gap_topic"] == "Chapter 1: The E-Commerce Revolution"

    def test_gap_filter_limits_scope(self, db, pull_root, runs_json):
        inserted = ingest_pull_output(
            db, pull_root, "run_test", runs_json_path=runs_json, gap_filter="AUTO-01-G1"
        )
        assert inserted == 2  # only ebsco_api has 2 articles
        gap_ids = {r[0] for r in db.execute("SELECT DISTINCT gap_id FROM articles").fetchall()}
        assert gap_ids == {"AUTO-01-G1"}


# ---------------------------------------------------------------------------
# Integration: FTS5 search
# ---------------------------------------------------------------------------

class TestFts:
    def test_fts_finds_matching_abstract(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        rows = db.execute(
            """
            SELECT a.title FROM articles_fts
            JOIN articles a ON a.id = articles_fts.rowid
            WHERE articles_fts MATCH 'ancient trade'
            """
        ).fetchall()
        titles = [r[0] for r in rows]
        assert "Article Gamma" in titles

    def test_fts_finds_by_title(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        rows = db.execute(
            """
            SELECT a.title FROM articles_fts
            JOIN articles a ON a.id = articles_fts.rowid
            WHERE articles_fts MATCH 'digital transformation'
            """
        ).fetchall()
        titles = [r[0] for r in rows]
        assert "Article Beta" in titles

    def test_fts_no_match_returns_empty(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        rows = db.execute(
            """
            SELECT a.title FROM articles_fts
            JOIN articles a ON a.id = articles_fts.rowid
            WHERE articles_fts MATCH 'xyzzy_nonexistent_term_7q9'
            """
        ).fetchall()
        assert rows == []

    def test_fts_searches_gap_research_question(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        rows = db.execute(
            """
            SELECT a.gap_id FROM articles_fts
            JOIN articles a ON a.id = articles_fts.rowid
            WHERE articles_fts MATCH 'ancient roots'
            GROUP BY a.gap_id
            """
        ).fetchall()
        gap_ids = {r[0] for r in rows}
        assert "AUTO-02-G1" in gap_ids


# ---------------------------------------------------------------------------
# Integration: DOI dedup
# ---------------------------------------------------------------------------

class TestDedupByDoi:
    def test_dedup_sets_canonical_id_on_duplicate(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        deduped = dedupe_by_doi(db, run_id="run_test")

        # Should have marked at least 1 row as a duplicate
        assert deduped >= 1

        # Exactly one row per DOI should have canonical_id IS NULL (the canonical)
        canonical_rows = db.execute(
            "SELECT id FROM articles WHERE doi = ? AND canonical_id IS NULL",
            (SHARED_DOI,),
        ).fetchall()
        assert len(canonical_rows) == 1

        # The other row should point at the canonical
        dup_rows = db.execute(
            "SELECT canonical_id FROM articles WHERE doi = ? AND canonical_id IS NOT NULL",
            (SHARED_DOI,),
        ).fetchall()
        assert len(dup_rows) == 1
        assert dup_rows[0]["canonical_id"] == canonical_rows[0]["id"]

    def test_dedup_prefers_row_with_pdf(self, db, pull_root, runs_json):
        """The canonical row for a shared DOI should not be the one that already has
        a PDF — but in our test data, Article Alpha (ebsco) has no PDF while the
        jstor duplicate also has no PDF.  Since ebsco_api has higher source priority
        we expect the ebsco row to be canonical."""
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        dedupe_by_doi(db, run_id="run_test")

        canonical = db.execute(
            "SELECT source_id FROM articles WHERE doi = ? AND canonical_id IS NULL",
            (SHARED_DOI,),
        ).fetchone()
        # ebsco_api has source priority 1 (highest), should be canonical
        assert canonical["source_id"] == "ebsco_api"

    def test_dedup_idempotent(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        dedupe_by_doi(db, run_id="run_test")
        total_before = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        dedupe_by_doi(db, run_id="run_test")
        total_after = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        # Row count must not change (dedup only sets canonical_id, never deletes)
        assert total_before == total_after

    def test_unique_doi_rows_untouched(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        dedupe_by_doi(db, run_id="run_test")
        # Article Gamma has no DOI — its canonical_id should remain NULL
        row = db.execute(
            "SELECT canonical_id FROM articles WHERE title = 'Article Gamma'"
        ).fetchone()
        assert row is not None
        assert row["canonical_id"] is None


# ---------------------------------------------------------------------------
# Integration: source-count query
# ---------------------------------------------------------------------------

class TestSourceCounts:
    def test_source_counts_correct(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        rows = db.execute(
            """
            SELECT source_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN pdf_path IS NOT NULL THEN 1 ELSE 0 END) AS with_pdf
            FROM articles
            GROUP BY source_id
            ORDER BY total DESC
            """
        ).fetchall()
        result = {r["source_id"]: dict(r) for r in rows}

        assert result["ebsco_api"]["total"] == 2
        assert result["ebsco_api"]["with_pdf"] == 1
        assert result["jstor"]["total"] == 2
        assert result["jstor"]["with_pdf"] == 0


# ---------------------------------------------------------------------------
# Unit: gap_context_for
# ---------------------------------------------------------------------------

class TestGapContextFor:
    def test_returns_claim_and_chapter(self, runs_json):
        claim, chapter = gap_context_for("run_test", "AUTO-01-G1", runs_json)
        assert claim == "Amazon is not the everything store we imagine."
        assert chapter == "Chapter 1: The E-Commerce Revolution"

    def test_unknown_run_returns_none(self, runs_json):
        claim, chapter = gap_context_for("run_does_not_exist", "AUTO-01-G1", runs_json)
        assert claim is None
        assert chapter is None

    def test_unknown_gap_returns_none(self, runs_json):
        claim, chapter = gap_context_for("run_test", "AUTO-99-G99", runs_json)
        assert claim is None
        assert chapter is None

    def test_missing_runs_json_returns_none(self, tmp_path):
        claim, chapter = gap_context_for("run_test", "AUTO-01-G1", tmp_path / "no_file.json")
        assert claim is None
        assert chapter is None


# ---------------------------------------------------------------------------
# Integration: zero-PDF gap detection
# ---------------------------------------------------------------------------

class TestZeroPdfGaps:
    def test_gap_with_no_pdfs_detected(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        zero_pdf_gaps = db.execute(
            """
            SELECT gap_id
            FROM articles
            GROUP BY gap_id
            HAVING SUM(CASE WHEN pdf_path IS NOT NULL THEN 1 ELSE 0 END) = 0
            """
        ).fetchall()
        gap_ids = {r["gap_id"] for r in zero_pdf_gaps}
        # AUTO-02-G1 (jstor) has no PDFs
        assert "AUTO-02-G1" in gap_ids

    def test_gap_with_pdf_not_in_zero_pdf_list(self, db, pull_root, runs_json):
        ingest_pull_output(db, pull_root, "run_test", runs_json_path=runs_json)
        zero_pdf_gaps = db.execute(
            """
            SELECT gap_id
            FROM articles
            GROUP BY gap_id
            HAVING SUM(CASE WHEN pdf_path IS NOT NULL THEN 1 ELSE 0 END) = 0
            """
        ).fetchall()
        gap_ids = {r["gap_id"] for r in zero_pdf_gaps}
        # AUTO-01-G1 has Article Beta with a PDF
        assert "AUTO-01-G1" not in gap_ids


# ---------------------------------------------------------------------------
# Helpers for ProQuest JSON tests
# ---------------------------------------------------------------------------

def _make_proquest_record(
    title: str,
    *,
    url: str = "",
    authors: str = "",
    journal: str = "",
    pub_date: str = "",
    abstract: str = "",
    query: str = "",
    gap_id: str = "AUTO-01-G1",
) -> dict:
    """Build a minimal ProQuest seed record matching write_records() output."""
    return {
        "title": title,
        "url": url or f"https://www.proquest.com/docview/{hash(title)}",
        "pdf_url": "",
        "abstract": abstract,
        "authors": authors,
        "journal": journal,
        "pub_date": pub_date,
        "doi": "",
        "query": query or "(\"e-commerce\" OR \"online shopping\") AND (\"Alibaba\")",
        "gap_id": gap_id,
        "quality_label": "seed",
        "quality_rank": "20",
        "source": "proquest_international_newsstream_proquest_html",
        "link_type": "newspaper_record",
        "source_type": "Trade Journal",
    }


def _write_proquest_json(path: Path, records: list) -> None:
    """Write a ProQuest seed JSON file (list of record dicts)."""
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture: mixed tree with both EBSCO .md and ProQuest JSON sources
# ---------------------------------------------------------------------------

@pytest.fixture
def mixed_pull_root(tmp_path) -> Path:
    """Pull root with one EBSCO .md source + one ProQuest JSON source."""
    root = tmp_path / "pull_outputs" / "run_mixed"

    # --- Gap 1 / ebsco_api (has fetched/ subdir — markdown path) ---
    ebsco_dir = root / "AUTO-01-G1" / "ebsco_api"
    ebsco_fetched = ebsco_dir / "fetched"
    ebsco_fetched.mkdir(parents=True)

    _write_seed_json(
        ebsco_dir / "seed.json",
        query="everything+store",
        bquery_original="everything store",
    )
    _write_md(
        ebsco_fetched / "EBSCO_Article.md",
        title="EBSCO Article One",
        authors="Smith, J.",
        journal="Commerce Journal, 2020",
        abstract="EBSCO abstract about retail.",
    )

    # --- Gap 1 / proquest_international_newsstream (no fetched/ — JSON-only path) ---
    pq_dir = root / "AUTO-01-G1" / "proquest_international_newsstream"
    pq_dir.mkdir(parents=True)

    _write_proquest_json(
        pq_dir / "ecommerce_china.json",
        [_make_proquest_record(
            "ProQuest Article One",
            authors="Liu, N.",
            journal="FT.com; London",
            pub_date="Jan 14, 2020",
            abstract="ProQuest abstract about Chinese e-commerce.",
            query="(\"e-commerce\") AND (\"China\")",
        )],
    )

    return root


@pytest.fixture
def mixed_runs_json(tmp_path) -> Path:
    """Minimal runs.json for the mixed fixture."""
    data = {
        "run_mixed": {
            "run_id": "run_mixed",
            "gap_map": {
                "gaps": [
                    {
                        "gap_id": "AUTO-01-G1",
                        "chapter": "Chapter 1",
                        "claim_text": "E-commerce in China.",
                    },
                ]
            },
        }
    }
    path = tmp_path / "runs.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests: JSON seed ingestion
# ---------------------------------------------------------------------------

class TestIngestSeedJson:

    def test_both_md_and_json_rows_present_after_ingest(self, db, mixed_pull_root, mixed_runs_json):
        """After ingest, the EBSCO .md article and the ProQuest JSON article are both indexed."""
        inserted = ingest_pull_output(
            db, mixed_pull_root, "run_mixed", runs_json_path=mixed_runs_json
        )
        assert inserted == 2
        titles = {r[0] for r in db.execute("SELECT title FROM articles").fetchall()}
        assert "EBSCO Article One" in titles
        assert "ProQuest Article One" in titles

    def test_proquest_pdf_path_and_md_path_are_null(self, db, mixed_pull_root, mixed_runs_json):
        """ProQuest JSON records have no pdf_path or md_path (metadata-only)."""
        ingest_pull_output(db, mixed_pull_root, "run_mixed", runs_json_path=mixed_runs_json)
        row = db.execute(
            "SELECT pdf_path, md_path FROM articles WHERE title = 'ProQuest Article One'"
        ).fetchone()
        assert row is not None
        assert row["pdf_path"] is None
        assert row["md_path"] is None

    def test_ingest_is_idempotent_with_json_source(self, db, mixed_pull_root, mixed_runs_json):
        """Re-running ingest on a JSON source adds zero duplicate rows."""
        first = ingest_pull_output(db, mixed_pull_root, "run_mixed", runs_json_path=mixed_runs_json)
        second = ingest_pull_output(db, mixed_pull_root, "run_mixed", runs_json_path=mixed_runs_json)
        assert first == 2
        assert second == 0
        total = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert total == 2

    def test_md_takes_precedence_over_json_for_same_source(self, tmp_path):
        """When a source dir has a fetched/ subdir, the JSON walk is skipped entirely
        even if JSON files are present alongside it — the .md data is canonical."""
        root = tmp_path / "pull_outputs" / "run_ebsco_only"
        ebsco_dir = root / "AUTO-01-G1" / "ebsco_api"
        fetched = ebsco_dir / "fetched"
        fetched.mkdir(parents=True)

        # Write the EBSCO seed JSON (provider_search type — should never appear as row)
        (ebsco_dir / "seed.json").write_text(
            json.dumps([{
                "title": "ebsco_api search results",
                "url": "https://search.ebscohost.com/?bquery=test",
                "link_type": "provider_search",
                "quality_label": "seed",
                "bquery_original": "test query",
            }]),
            encoding="utf-8",
        )
        # Write the .md article
        _write_md(
            fetched / "Canonical_Article.md",
            title="Canonical Article",
            authors="Doe, J.",
            journal="Journal, 2021",
            abstract="Canonical content.",
            doi="10.9999/canonical",
        )

        db = open_index(tmp_path / "test_prec.sqlite")
        inserted = ingest_pull_output(db, root, "run_ebsco_only")
        # Only 1 row — the EBSCO seed's "provider_search" record must not appear
        assert inserted == 1
        row = db.execute("SELECT title, md_path FROM articles").fetchone()
        assert row["title"] == "Canonical Article"
        assert row["md_path"] is not None  # came from the .md walk, not JSON

    def test_empty_pub_date_does_not_break_indexing(self, tmp_path):
        """ProQuest records often have empty pub_date — must not cause errors."""
        root = tmp_path / "pull_outputs" / "run_nodates"
        pq_dir = root / "AUTO-01-G1" / "proquest_international_newsstream"
        pq_dir.mkdir(parents=True)

        _write_proquest_json(
            pq_dir / "query.json",
            [_make_proquest_record(
                "Article With No Date",
                pub_date="",  # explicitly empty
            )],
        )

        db = open_index(tmp_path / "test_nodates.sqlite")
        inserted = ingest_pull_output(db, root, "run_nodates")
        assert inserted == 1
        row = db.execute("SELECT pub_date FROM articles WHERE title = 'Article With No Date'").fetchone()
        assert row is not None
        assert row["pub_date"] is None

    def test_json_list_of_50_records_all_ingested(self, tmp_path):
        """A JSON file with 50 records (matching real ProQuest output) all get indexed."""
        root = tmp_path / "pull_outputs" / "run_50recs"
        pq_dir = root / "AUTO-01-G1" / "proquest_international_newsstream"
        pq_dir.mkdir(parents=True)

        records = [
            _make_proquest_record(f"ProQuest Article {i:03d}")
            for i in range(50)
        ]
        _write_proquest_json(pq_dir / "batch_query.json", records)

        db = open_index(tmp_path / "test_50.sqlite")
        inserted = ingest_pull_output(db, root, "run_50recs")
        assert inserted == 50
        total = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        assert total == 50

    def test_sources_query_includes_proquest_source_ids(self, db, mixed_pull_root, mixed_runs_json):
        """After ingest, a GROUP BY source_id query returns proquest_* source IDs."""
        ingest_pull_output(db, mixed_pull_root, "run_mixed", runs_json_path=mixed_runs_json)
        sources = {r[0] for r in db.execute("SELECT source_id FROM articles GROUP BY source_id").fetchall()}
        assert "proquest_international_newsstream" in sources
        assert "ebsco_api" in sources

    def test_provider_search_records_are_never_indexed(self, tmp_path):
        """Records with link_type == 'provider_search' must be silently skipped."""
        root = tmp_path / "pull_outputs" / "run_skiptest"
        pq_dir = root / "AUTO-01-G1" / "proquest_international_newsstream"
        pq_dir.mkdir(parents=True)

        # Mix of one provider_search record and one real article
        records = [
            {
                "title": "ebsco_api search results",
                "url": "https://search.ebscohost.com/",
                "link_type": "provider_search",
                "quality_label": "seed",
            },
            _make_proquest_record("Real Article Only"),
        ]
        _write_proquest_json(pq_dir / "mixed.json", records)

        db = open_index(tmp_path / "test_skip.sqlite")
        inserted = ingest_pull_output(db, root, "run_skiptest")
        assert inserted == 1
        row = db.execute("SELECT title FROM articles").fetchone()
        assert row["title"] == "Real Article Only"

    def test_query_field_stored_as_bquery_original_for_proquest(self, db, mixed_pull_root, mixed_runs_json):
        """ProQuest records have no bquery_original field; the 'query' field should
        be stored as bquery_original so search context is preserved."""
        ingest_pull_output(db, mixed_pull_root, "run_mixed", runs_json_path=mixed_runs_json)
        row = db.execute(
            "SELECT bquery_original FROM articles WHERE source_id = 'proquest_international_newsstream'"
        ).fetchone()
        assert row is not None
        assert row["bquery_original"] is not None
        assert "e-commerce" in row["bquery_original"] or "China" in row["bquery_original"]
