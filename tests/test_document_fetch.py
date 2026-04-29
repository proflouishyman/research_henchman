"""Tests for adapters/document_fetch.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from adapters.document_fetch import (
    FETCH_SUBDIR,
    FetchDocumentsStats,
    FetchItem,
    _classify_record,
    _write_ebsco_records,
    _write_jstor_records,
    collect_fetch_items,
    download_pdf,
    fetch_seed_page,
    preview_counts,
    run_fetch,
    save_abstract,
)
from contracts import GapPriority, GapType, PlannedGap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pull_dir(tmp_path: Path) -> Path:
    """Create a minimal pull_output directory with test JSON artifacts."""
    run_dir = tmp_path / "pull_outputs" / "run_test"
    seed_dir = run_dir / "AUTO-01-G1" / "jstor"
    seed_dir.mkdir(parents=True)
    (seed_dir / "query_results.json").write_text(
        json.dumps([
            {"title": "Some Article", "url": "https://www.jstor.org/search?q=test", "quality_label": "seed"},
            {"title": "Another Article", "url": "https://www.jstor.org/search?q=test2", "quality_label": "seed"},
        ]),
        encoding="utf-8",
    )

    pdf_dir = run_dir / "AUTO-01-G1" / "ebsco_api"
    pdf_dir.mkdir(parents=True)
    (pdf_dir / "ebsco_results.json").write_text(
        json.dumps([
            {
                "title": "PDF Article",
                "pdf_url": "https://example.com/article.pdf",
                "quality_label": "high",
            }
        ]),
        encoding="utf-8",
    )

    abstract_dir = run_dir / "AUTO-02-G1" / "project_muse"
    abstract_dir.mkdir(parents=True)
    (abstract_dir / "muse_results.json").write_text(
        json.dumps([
            {
                "title": "Abstract Only Article",
                "abstract": "This is the abstract text.",
                "authors": "Smith, J.",
                "journal": "Historical Review",
                "pub_date": "1994",
                "doi": "10.0000/test",
                "quality_label": "medium",
            }
        ]),
        encoding="utf-8",
    )
    return run_dir


# ---------------------------------------------------------------------------
# collect_fetch_items
# ---------------------------------------------------------------------------


def test_collect_fetch_items_classifies_all_types(tmp_path: Path) -> None:
    run_dir = _make_pull_dir(tmp_path)
    items = collect_fetch_items(run_dir, skip_already_fetched=False)
    types = {i.fetch_type for i in items}
    assert "seed" in types
    assert "pdf" in types
    assert "abstract" in types


def test_collect_fetch_items_gap_filter(tmp_path: Path) -> None:
    run_dir = _make_pull_dir(tmp_path)
    items = collect_fetch_items(run_dir, gap_filter="AUTO-01-G1", skip_already_fetched=False)
    assert all(i.gap_id == "AUTO-01-G1" for i in items)
    assert any(i.fetch_type == "seed" for i in items)


def test_collect_fetch_items_limit(tmp_path: Path) -> None:
    run_dir = _make_pull_dir(tmp_path)
    items = collect_fetch_items(run_dir, limit=2, skip_already_fetched=False)
    assert len(items) <= 2


def test_collect_fetch_items_skip_already_fetched(tmp_path: Path) -> None:
    run_dir = _make_pull_dir(tmp_path)
    # Pre-create the output file for the seed item
    fetch_dir = run_dir / "AUTO-01-G1" / "jstor" / FETCH_SUBDIR
    fetch_dir.mkdir(parents=True)
    (fetch_dir / "Some_Article.md").write_text("done", encoding="utf-8")

    items = collect_fetch_items(run_dir, skip_already_fetched=True)
    seed_titles = [i.title for i in items if i.fetch_type == "seed"]
    assert "Some Article" not in seed_titles


def test_collect_fetch_items_empty_dir(tmp_path: Path) -> None:
    items = collect_fetch_items(tmp_path / "nonexistent")
    assert items == []


# ---------------------------------------------------------------------------
# preview_counts
# ---------------------------------------------------------------------------


def test_preview_counts_returns_correct_breakdown(tmp_path: Path) -> None:
    run_dir = _make_pull_dir(tmp_path)
    counts = preview_counts(run_dir)
    assert counts["total"] >= 3
    assert counts.get("seed", 0) >= 1
    assert counts.get("pdf", 0) >= 1
    assert counts.get("abstract", 0) >= 1


# ---------------------------------------------------------------------------
# _classify_record
# ---------------------------------------------------------------------------


def test_classify_record_seed(tmp_path: Path) -> None:
    rec = {"title": "T", "url": "https://jstor.org/search?q=x", "quality_label": "seed"}
    item = _classify_record(rec, "G1", "jstor", tmp_path, skip_already_fetched=False)
    assert item is not None
    assert item.fetch_type == "seed"
    assert item.url == rec["url"]


def test_classify_record_pdf(tmp_path: Path) -> None:
    rec = {"title": "T", "pdf_url": "https://example.com/a.pdf", "quality_label": "high"}
    item = _classify_record(rec, "G1", "ebsco_api", tmp_path, skip_already_fetched=False)
    assert item is not None
    assert item.fetch_type == "pdf"
    assert item.url == rec["pdf_url"]


def test_classify_record_abstract(tmp_path: Path) -> None:
    rec = {"title": "T", "abstract": "Summary.", "quality_label": "medium"}
    item = _classify_record(rec, "G1", "project_muse", tmp_path, skip_already_fetched=False)
    assert item is not None
    assert item.fetch_type == "abstract"
    assert item.abstract == "Summary."


def test_classify_record_no_match(tmp_path: Path) -> None:
    # high quality without abstract or pdf_url — not fetchable
    rec = {"title": "T", "quality_label": "high", "url": ""}
    item = _classify_record(rec, "G1", "jstor", tmp_path, skip_already_fetched=False)
    assert item is None


# ---------------------------------------------------------------------------
# save_abstract
# ---------------------------------------------------------------------------


def test_save_abstract_creates_markdown(tmp_path: Path) -> None:
    item = FetchItem(
        gap_id="G1", source_id="project_muse",
        out_dir=str(tmp_path), title="Test Article",
        url="", fetch_type="abstract",
        abstract="This is the abstract.",
        authors="Smith, J.", journal="Historical Review",
        pub_date="1994", doi="10.0000/test",
    )
    path = save_abstract(item)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "Test Article" in text
    assert "This is the abstract." in text
    assert "Smith, J." in text


def test_save_abstract_idempotent(tmp_path: Path) -> None:
    item = FetchItem(
        gap_id="G1", source_id="jstor",
        out_dir=str(tmp_path), title="Test Article",
        url="", fetch_type="abstract",
        abstract="Abstract text.",
    )
    path1 = save_abstract(item)
    mtime1 = path1.stat().st_mtime
    path2 = save_abstract(item)
    assert path1 == path2
    assert path2.stat().st_mtime == mtime1  # not rewritten


# ---------------------------------------------------------------------------
# _write_ebsco_records / _write_jstor_records
# ---------------------------------------------------------------------------


def test_write_ebsco_records(tmp_path: Path) -> None:
    records = [{"title": "EBSCO Article", "authors": "A", "source": "J", "date": "1994", "abstract": "Ab.", "pdf_url": ""}]
    count = _write_ebsco_records(records, tmp_path)
    assert count == 1
    assert (tmp_path / "EBSCO_Article.md").exists()


def test_write_jstor_records(tmp_path: Path) -> None:
    records = [{"title": "JSTOR Article", "authors": "B", "source": "J2", "date": "1995", "abstract": ""}]
    count = _write_jstor_records(records, tmp_path)
    assert count == 1
    assert (tmp_path / "JSTOR_Article.md").exists()


def test_write_records_skips_no_title(tmp_path: Path) -> None:
    records = [{"title": "", "abstract": "no title"}]
    count = _write_ebsco_records(records, tmp_path)
    assert count == 0


def test_write_records_caps_at_max_articles(tmp_path: Path) -> None:
    records = [{"title": f"Article {i}", "abstract": ""} for i in range(15)]
    count = _write_ebsco_records(records, tmp_path)
    assert count <= 8  # MAX_ARTICLES


# ---------------------------------------------------------------------------
# fetch_seed_page (mocked BrowserClient)
# ---------------------------------------------------------------------------


def _make_mock_browser(eval_result: Any = None, blocked: bool = False) -> MagicMock:
    from adapters.browser_client import PageResult
    page_result = PageResult(
        url="https://example.com",
        status_code=200,
        content=b"<html><body>search results</body></html>",
        content_type="text/html",
        blocked=blocked,
        blocked_reason="login" if blocked else "",
    )
    browser = MagicMock()
    browser.fetch_with_eval.return_value = (page_result, eval_result)
    browser.fetch.return_value = page_result
    return browser


def test_fetch_seed_page_ebsco_extracts_records(tmp_path: Path) -> None:
    eval_result = [{"title": "EBSCO Article", "authors": "A", "source": "J", "date": "1994", "abstract": "Ab.", "pdf_url": ""}]
    browser = _make_mock_browser(eval_result=eval_result)
    item = FetchItem(gap_id="G1", source_id="ebsco_api", out_dir=str(tmp_path),
                     title="EBSCO search", url="https://search.ebscohost.com/...", fetch_type="seed")
    count = fetch_seed_page(item, browser)
    assert count >= 1
    fetched_dir = tmp_path / FETCH_SUBDIR
    assert any(fetched_dir.glob("*.md"))


def test_fetch_seed_page_blocked_emits_event_returns_zero(tmp_path: Path) -> None:
    browser = _make_mock_browser(blocked=True)
    events = []
    item = FetchItem(gap_id="G1", source_id="jstor", out_dir=str(tmp_path),
                     title="JSTOR search", url="https://www.jstor.org/...", fetch_type="seed")
    count = fetch_seed_page(item, browser, emit=lambda *a, **kw: events.append(a))
    assert count == 0
    assert any("blocked" in str(e) for e in events)


def test_fetch_seed_page_generic_saves_html(tmp_path: Path) -> None:
    browser = _make_mock_browser()
    item = FetchItem(gap_id="G1", source_id="gale_primary_sources", out_dir=str(tmp_path),
                     title="Gale search", url="https://go.gale.com/...", fetch_type="seed")
    count = fetch_seed_page(item, browser)
    assert count >= 1
    fetched_dir = tmp_path / FETCH_SUBDIR
    assert (fetched_dir / "search_results.html").exists()


# ---------------------------------------------------------------------------
# download_pdf (mocked)
# ---------------------------------------------------------------------------


def test_download_pdf_saves_file(tmp_path: Path) -> None:
    from adapters.browser_client import PageResult
    pdf_bytes = b"%PDF-1.4 test content"
    page_result = PageResult(url="https://example.com/a.pdf", status_code=200,
                              content=pdf_bytes, content_type="application/pdf")
    browser = MagicMock()
    browser.fetch.return_value = page_result

    item = FetchItem(gap_id="G1", source_id="jstor", out_dir=str(tmp_path),
                     title="Test PDF", url="https://example.com/a.pdf", fetch_type="pdf")

    with patch("urllib.request.urlopen", side_effect=Exception("no direct")):
        path = download_pdf(item, browser)

    assert path.exists()
    assert path.read_bytes() == pdf_bytes


def test_download_pdf_idempotent(tmp_path: Path) -> None:
    pdf_bytes = b"%PDF-1.4 test content"
    item = FetchItem(gap_id="G1", source_id="jstor", out_dir=str(tmp_path),
                     title="Test PDF", url="https://example.com/a.pdf", fetch_type="pdf")
    # Pre-create the file
    fetched_dir = tmp_path / FETCH_SUBDIR
    fetched_dir.mkdir(parents=True)
    slug_path = fetched_dir / "Test_PDF.pdf"
    slug_path.write_bytes(pdf_bytes)

    browser = MagicMock()
    path = download_pdf(item, browser)
    browser.fetch.assert_not_called()  # should not fetch if already present
    assert path == slug_path


# ---------------------------------------------------------------------------
# run_fetch (mocked BrowserClient — integration)
# ---------------------------------------------------------------------------


def test_run_fetch_returns_correct_stats(tmp_path: Path) -> None:
    eval_result = [{"title": "Article 1", "authors": "A", "source": "J", "date": "1994", "abstract": "", "pdf_url": ""}]
    browser = _make_mock_browser(eval_result=eval_result)

    items = [
        FetchItem(gap_id="G1", source_id="jstor", out_dir=str(tmp_path / "jstor"),
                  title="Search", url="https://jstor.org/...", fetch_type="seed"),
        FetchItem(gap_id="G1", source_id="project_muse", out_dir=str(tmp_path / "muse"),
                  title="Abstract", url="", fetch_type="abstract",
                  abstract="Abstract text.", authors="B", journal="J2"),
    ]

    stats = run_fetch(items, browser)
    assert stats.items_found == 2
    assert stats.seeds_attempted == 1
    assert stats.seeds_ok == 1
    assert stats.abstracts_saved == 1


def test_run_fetch_handles_browser_exception(tmp_path: Path) -> None:
    browser = MagicMock()
    browser.fetch_with_eval.side_effect = RuntimeError("CDP disconnected")
    browser.fetch.side_effect = RuntimeError("CDP disconnected")

    items = [
        FetchItem(gap_id="G1", source_id="jstor", out_dir=str(tmp_path),
                  title="Search", url="https://jstor.org/...", fetch_type="seed"),
    ]
    stats = run_fetch(items, browser)
    assert stats.seeds_failed == 1
    assert stats.seeds_ok == 0
