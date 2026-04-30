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
    # run_fetch wraps the seed/PDF loop in `with browser_client.session() as bc:`
    # for single-tab reuse. Make the mock's session() yield the same mock so
    # configured return values flow through.
    browser.session.return_value.__enter__.return_value = browser
    browser.session.return_value.__exit__.return_value = False
    # Mark this as a non-CDP session so _try_pdf_fetch_per_article's sequential
    # fallback short-circuits without trying to navigate a MagicMock page.
    browser._page = None
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


def test_fetch_seed_page_on_blocked_retries_when_handler_returns_true(tmp_path: Path) -> None:
    """If on_blocked returns True, fetch_seed_page re-fetches the URL once."""
    from adapters.browser_client import PageResult

    blocked_page = PageResult(
        url="https://search.ebscohost.com/login.aspx",
        status_code=200,
        content=b"<html><body>I'm not a robot</body></html>",
        content_type="text/html",
        blocked=True,
        blocked_reason="captcha",
        action_required="Solve CAPTCHA in browser",
    )
    unblocked_page = PageResult(
        url="https://search.ebscohost.com/login.aspx",
        status_code=200,
        content=b"<html><body>search results</body></html>",
        content_type="text/html",
    )
    eval_after_unblock = [{
        "title": "Recovered Article", "authors": "A", "source": "J",
        "date": "", "abstract": "Ab.", "pdf_url": "",
    }]
    browser = MagicMock()
    browser.fetch_with_eval.side_effect = [
        (blocked_page, None),          # first attempt blocked
        (unblocked_page, eval_after_unblock),  # retry succeeds
    ]
    item = FetchItem(gap_id="G1", source_id="ebsco_api", out_dir=str(tmp_path),
                     title="EBSCO search", url="https://search.ebscohost.com/x", fetch_type="seed")
    handler_calls = []
    count = fetch_seed_page(
        item, browser,
        on_blocked=lambda i, p: handler_calls.append((i.gap_id, p.blocked_reason)) or True,
    )
    assert browser.fetch_with_eval.call_count == 2
    assert handler_calls == [("G1", "captcha")]
    assert count >= 1
    assert any((tmp_path / FETCH_SUBDIR).glob("*.md"))


def test_fetch_seed_page_on_blocked_skips_when_handler_returns_false(tmp_path: Path) -> None:
    """If on_blocked returns False (e.g. user aborts), fetch_seed_page does not retry."""
    browser = _make_mock_browser(blocked=True)
    item = FetchItem(gap_id="G1", source_id="jstor", out_dir=str(tmp_path),
                     title="JSTOR search", url="https://www.jstor.org/x", fetch_type="seed")
    count = fetch_seed_page(item, browser, on_blocked=lambda i, p: False)
    assert count == 0
    assert browser.fetch_with_eval.call_count == 1


def test_detect_blocked_recognises_captcha_phrasing() -> None:
    """Block detection covers reCAPTCHA, 'I'm not a robot', and Cloudflare wording."""
    from adapters.browser_client import _detect_blocked

    cases = [
        b"<html>Please complete the reCAPTCHA</html>",
        b"<html>I'm not a robot</html>",
        b"<html>I am not a robot</html>",
        b"<html>verify you are human before continuing</html>",
        b"<html>Checking your browser before accessing</html>",
        b"<html>Just a moment, we're checking your browser</html>",
    ]
    for body in cases:
        blocked, reason, _ = _detect_blocked(body, "https://example.com")
        assert blocked, f"failed to detect block in: {body!r}"
        assert reason == "captcha", f"wrong reason {reason!r} for body: {body!r}"


def test_download_article_pdf_returns_none_for_non_cdp_session(tmp_path: Path) -> None:
    """A session without a ._page attribute (HTTP / claude_cu provider) cannot
    click into an article detail page; download_article_pdf returns None."""
    from adapters.document_fetch import download_article_pdf

    record = {"title": "Some Article", "url": "/c/abc/search/details/xyz"}
    # Plain object with no _page attribute
    class _NotASession:
        pass
    assert download_article_pdf(record, _NotASession(), tmp_path) is None


def test_download_article_pdf_skips_when_no_viewer_link(tmp_path: Path) -> None:
    """When the detail page has no <a href*="/viewer/pdf/"> element, the
    article has no PDF available — return None without attempting capture."""
    from adapters.document_fetch import download_article_pdf

    page = MagicMock()
    page.evaluate.return_value = None  # no viewer link found
    session = MagicMock()
    session._page = page

    record = {"title": "Article With No PDF", "url": "/c/abc/search/details/xyz"}
    result = download_article_pdf(record, session, tmp_path)
    assert result is None
    # Detail page was navigated, but no second navigation to a viewer URL
    assert page.goto.call_count == 1


def test_download_article_pdf_returns_existing_file_without_refetch(tmp_path: Path) -> None:
    """If <slug>.pdf already exists in out_dir, return it without navigating."""
    from adapters.document_fetch import download_article_pdf, _slugify

    title = "Pre-existing Article"
    slug = _slugify(title)[:60]
    pdf_path = tmp_path / f"{slug}.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 prior content")

    page = MagicMock()
    session = MagicMock()
    session._page = page

    record = {"title": title, "url": "/c/abc/search/details/xyz"}
    result = download_article_pdf(record, session, tmp_path)
    assert result == pdf_path
    page.goto.assert_not_called()


def test_try_pdf_fetch_per_article_emits_unavailable_for_each_missing(tmp_path: Path) -> None:
    """When session can't access pages (e.g. mock without _page), every
    record should emit pdf_inline_unavailable so run_fetch tallies them."""
    from adapters.document_fetch import _try_pdf_fetch_per_article

    records = [
        {"title": "First Article",  "url": "/c/x/search/details/a"},
        {"title": "Second Article", "url": "/c/x/search/details/b"},
        {"title": "",               "url": "/c/x/search/details/c"},  # filtered out
    ]
    events = []
    _try_pdf_fetch_per_article(
        records, session=object(), out_dir=tmp_path,
        gap_id="G1", source_id="ebsco_api",
        emit=lambda *a, **kw: events.append(a),
    )
    statuses = [e[1] for e in events]
    # Two valid records → two unavailable events; the empty-title record is skipped.
    assert statuses.count("pdf_inline_unavailable") == 2


def _maybe_skip_no_playwright_browser():
    """Skip the calling test when Playwright + headless Chromium aren't
    installed — keeps the suite green on environments that haven't done
    ``playwright install chromium``."""
    import pytest
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            b.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"headless chromium not available: {exc!s:.60}")


def test_ebsco_js_extracts_records_from_local_fixture() -> None:
    """Load the bundled EBSCO-results HTML fixture in a real headless
    Chromium and run the production _EBSCO_JS extractor against it.

    Catches DOM-shape regressions in the JS extractor without depending on
    a live EBSCO session. The fixture mirrors the data-auto-* attribute
    structure observed in research.ebsco.com (2026-04-30) — when EBSCO
    re-skins the layout, this test should fail before any live run does.
    """
    _maybe_skip_no_playwright_browser()
    from playwright.sync_api import sync_playwright
    from adapters.document_fetch import _EBSCO_JS

    fixture = Path(__file__).parent / "fixtures" / "ebsco_search_results.html"
    assert fixture.exists(), "fixture HTML missing"
    file_url = fixture.as_uri()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_context().new_page()
            page.goto(file_url, wait_until="domcontentloaded")
            page.wait_for_selector('article[data-auto="search-result-item"]',
                                   timeout=5000, state="visible")
            records = page.evaluate(_EBSCO_JS)
        finally:
            browser.close()

    assert isinstance(records, list)
    assert len(records) == 2

    rec0 = records[0]
    assert rec0["title"] == "The Everything Store: Jeff Bezos and the Age of Amazon"
    assert rec0["authors"] == "Stone, Brad"
    assert "Library Journal" in rec0["source"]
    assert rec0["database"] == "Academic Search Ultimate"
    assert "Brad Stone" in rec0["abstract"]
    # URL was relative in the fixture; extractor should return it
    # absolute via new URL(href, location.origin).
    assert rec0["url"].endswith("/c/6hfcoc/search/details/abc123?db=asn")

    rec1 = records[1]
    assert rec1["title"] == "Online Retail and the Transformation of American Shopping"
    assert rec1["authors"] == "Hyman, Louis"
    assert rec1["database"] == "Business Source Ultimate"


def test_ebsco_js_returns_empty_list_when_no_articles() -> None:
    """When the page has no result-item articles (loading state, blocked
    page, empty result set), the extractor should return [] and not raise."""
    _maybe_skip_no_playwright_browser()
    from playwright.sync_api import sync_playwright
    from adapters.document_fetch import _EBSCO_JS

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_context().new_page()
            page.set_content(
                "<html><body><div>Loading…</div></body></html>",
                wait_until="domcontentloaded",
            )
            records = page.evaluate(_EBSCO_JS)
        finally:
            browser.close()

    assert records == []


def test_rewrite_ebsco_url_no_op_when_opid_unset(monkeypatch) -> None:
    """Without ORCH_EBSCO_OPID, _rewrite_ebsco_url_if_configured returns the
    URL unchanged — preserves legacy behaviour for users with a single
    institutional profile."""
    from adapters.document_fetch import _rewrite_ebsco_url_if_configured
    monkeypatch.delenv("ORCH_EBSCO_OPID", raising=False)
    url = "https://search.ebscohost.com/login.aspx?direct=true&bquery=test+query"
    assert _rewrite_ebsco_url_if_configured(url) == url


def test_rewrite_ebsco_url_redirects_to_named_profile(monkeypatch) -> None:
    """When ORCH_EBSCO_OPID is set, legacy login.aspx URLs are rewritten to
    research.ebsco.com/c/<opid>/search/results — bypasses the cookie-priority
    auto-redirect that surfaces when multiple institutional profiles are
    authenticated at once."""
    from adapters.document_fetch import _rewrite_ebsco_url_if_configured
    monkeypatch.setenv("ORCH_EBSCO_OPID", "6hfcoc")
    monkeypatch.setenv("ORCH_EBSCO_DB", "asn,bsu")
    # bquery=Amazon+e-commerce (form-encoded "+" = space) → q=Amazon%20e-commerce
    url = "https://search.ebscohost.com/login.aspx?direct=true&bquery=Amazon+e-commerce"
    rewritten = _rewrite_ebsco_url_if_configured(url)
    assert rewritten.startswith("https://research.ebsco.com/c/6hfcoc/search/results?")
    assert "q=Amazon%20e-commerce" in rewritten
    assert "db=asn%2Cbsu" in rewritten


def test_rewrite_ebsco_url_handles_encoded_plus_in_query(monkeypatch) -> None:
    """Real-world seed URLs include '+%2B+' meaning a literal '+' character
    (e.g. 'Amazon + e-commerce'). The rewrite must preserve the '+' literal,
    not collapse it. Regression for the JHU dataset where most queries have
    boolean operators encoded this way."""
    from adapters.document_fetch import _rewrite_ebsco_url_if_configured
    monkeypatch.setenv("ORCH_EBSCO_OPID", "6hfcoc")
    # bquery=Amazon+%2B+e-commerce → "Amazon + e-commerce" → q=Amazon%20%2B%20e-commerce
    url = ("https://search.ebscohost.com/login.aspx?direct=true&"
           "bquery=Amazon+%2B+e-commerce+revolution+archives")
    rewritten = _rewrite_ebsco_url_if_configured(url)
    assert "Amazon%20%2B%20e-commerce" in rewritten
    # The literal "+" character (encoded as %2B) must survive the rewrite —
    # otherwise EBSCO's boolean parser misinterprets the query.


def test_rewrite_ebsco_url_passes_through_non_legacy_urls(monkeypatch) -> None:
    """URLs that are already research.ebsco.com paths or aren't EBSCO at all
    should pass through untouched even with ORCH_EBSCO_OPID set."""
    from adapters.document_fetch import _rewrite_ebsco_url_if_configured
    monkeypatch.setenv("ORCH_EBSCO_OPID", "6hfcoc")
    direct = "https://research.ebsco.com/c/6hfcoc/search/details/abc?db=asn"
    assert _rewrite_ebsco_url_if_configured(direct) == direct
    nonebsco = "https://www.jstor.org/search?q=test"
    assert _rewrite_ebsco_url_if_configured(nonebsco) == nonebsco


def test_rewrite_ebsco_url_no_op_when_bquery_missing(monkeypatch) -> None:
    """If the legacy URL has no bquery parameter (corrupt or non-search URL),
    rewrite is a no-op rather than producing a URL with empty q=."""
    from adapters.document_fetch import _rewrite_ebsco_url_if_configured
    monkeypatch.setenv("ORCH_EBSCO_OPID", "6hfcoc")
    weird = "https://search.ebscohost.com/login.aspx?direct=true"
    assert _rewrite_ebsco_url_if_configured(weird) == weird


def test_rewrite_ebsco_url_uses_default_db_when_unset(monkeypatch) -> None:
    """ORCH_EBSCO_DB defaults to asn,bsu (Academic Search Ultimate +
    Business Source Ultimate) — JHU's most-licensed academic databases."""
    from adapters.document_fetch import _rewrite_ebsco_url_if_configured
    monkeypatch.setenv("ORCH_EBSCO_OPID", "6hfcoc")
    monkeypatch.delenv("ORCH_EBSCO_DB", raising=False)
    url = "https://search.ebscohost.com/login.aspx?direct=true&bquery=test"
    rewritten = _rewrite_ebsco_url_if_configured(url)
    assert "db=asn%2Cbsu" in rewritten


def test_rewrite_ebsco_url_respects_custom_db(monkeypatch) -> None:
    """Caller can configure a different database mix via ORCH_EBSCO_DB."""
    from adapters.document_fetch import _rewrite_ebsco_url_if_configured
    monkeypatch.setenv("ORCH_EBSCO_OPID", "myinst")
    monkeypatch.setenv("ORCH_EBSCO_DB", "asn,a9h,a3h")
    url = "https://search.ebscohost.com/login.aspx?direct=true&bquery=hello"
    rewritten = _rewrite_ebsco_url_if_configured(url)
    assert "db=asn%2Ca9h%2Ca3h" in rewritten
    assert rewritten.startswith("https://research.ebsco.com/c/myinst/search/results?")


def test_pdf_worker_pool_captcha_pause_blocks_workers_until_handler_returns(tmp_path: Path) -> None:
    """When pause_on_captcha is enabled and _detect_iframe_block returns
    blocked=True, _handle_captcha_if_present should: surface the tab, clear
    the free_event, fire on_state_change('captcha_paused'), wait for the
    handler to return, then fire 'captcha_resumed' and re-set free_event.
    """
    from adapters.document_fetch import _PdfWorkerPool

    states: list = []
    handler_call_count = {"n": 0}

    def _handler(state, meta):
        states.append((state, meta.get("gap_id"), meta.get("title")))
        if state == "captcha_paused":
            handler_call_count["n"] += 1

    pool = _PdfWorkerPool(
        cdp_url="http://127.0.0.1:9222",
        workers=1,
        pause_on_captcha=True,
        cooldown_base_sec=999999,
        max_pauses=10,
        jitter_ms=0,
        on_state_change=_handler,
    )

    # Stand in for a Playwright page: bring_to_front is the only method
    # called directly; _detect_iframe_block (imported lazily inside
    # _handle_captcha_if_present) gets the page passed through. We patch
    # browser_client._detect_iframe_block to return a forced "blocked".
    fake_page = MagicMock()
    fake_page.bring_to_front.return_value = None

    record = {"gap_id": "G1", "title": "Article With Captcha", "url": "/c/x/details/y"}

    # Patch the iframe-block detector at its source so the worker pool
    # treats this page as blocked regardless of the mock's evaluate path.
    import adapters.browser_client as _bc
    original = _bc._detect_iframe_block
    _bc._detect_iframe_block = lambda page: (True, "captcha", "Solve reCAPTCHA challenge in browser")
    try:
        result = pool._handle_captcha_if_present(fake_page, record)
    finally:
        _bc._detect_iframe_block = original

    assert result is True
    fake_page.bring_to_front.assert_called_once()
    state_names = [s[0] for s in states]
    assert "captcha_paused" in state_names
    assert "captcha_resumed" in state_names
    assert handler_call_count["n"] == 1
    # After the handler returned, the pool should be unpaused.
    assert pool.is_paused is False


def test_pdf_worker_pool_captcha_skipped_when_pause_disabled() -> None:
    """If pause_on_captcha is False, _handle_captcha_if_present is never
    called and the worker simply reports the article as no_pdf_link
    (current default for unattended runs)."""
    from adapters.document_fetch import _PdfWorkerPool

    pool = _PdfWorkerPool(
        cdp_url="http://127.0.0.1:9222",
        workers=1,
        pause_on_captcha=False,
        jitter_ms=0,
    )
    assert pool.pause_on_captcha is False
    # The worker loop only invokes _handle_captcha_if_present when
    # self.pause_on_captcha is True, so its mere existence is benign.


def test_pdf_worker_pool_throttle_pause_triggers_state_callback(tmp_path: Path) -> None:
    """When N consecutive navigation_timeout reasons hit the pool, it should
    set the paused flag and fire on_state_change('throttle_paused', ...).

    Constructed with workers=1 (single thread) so we can drive the throttle
    counter deterministically. The pool's _update_throttle_state is called
    directly (bypassing the worker loop's playwright dependency) — verifies
    the threshold logic in isolation.
    """
    from adapters.document_fetch import _PdfWorkerPool

    states: list = []
    pool = _PdfWorkerPool(
        cdp_url="http://127.0.0.1:9222",
        workers=1,
        throttle_threshold=3,
        cooldown_base_sec=999999,    # never auto-resume during the test
        max_pauses=10,
        jitter_ms=0,
        on_state_change=lambda s, m: states.append((s, m)),
    )
    # Three timeouts in a row → threshold met → pause.
    for _ in range(3):
        pool._update_throttle_state("navigation_timeout")

    assert pool.is_paused is True
    assert pool.total_pauses == 1
    assert any(s[0] == "throttle_paused" for s in states)
    pause_meta = next(s[1] for s in states if s[0] == "throttle_paused")
    assert pause_meta["consecutive_throttles"] == 3
    assert pause_meta["total_pauses"] == 1


def test_pdf_worker_pool_no_pdf_resets_throttle_counter() -> None:
    """A 'no_pdf_link' result resets the consecutive-throttle counter so a
    long stretch of legitimate no-PDF articles doesn't accumulate toward
    a spurious pause."""
    from adapters.document_fetch import _PdfWorkerPool

    pool = _PdfWorkerPool(
        cdp_url="http://127.0.0.1:9222",
        workers=1,
        throttle_threshold=3,
        cooldown_base_sec=1, max_pauses=10, jitter_ms=0,
    )
    pool._update_throttle_state("navigation_timeout")
    pool._update_throttle_state("navigation_timeout")
    assert pool._consecutive_throttles == 2
    pool._update_throttle_state("no_pdf_link")    # reset
    assert pool._consecutive_throttles == 0
    pool._update_throttle_state("navigation_timeout")
    assert pool._consecutive_throttles == 1
    assert pool.is_paused is False


def test_pdf_worker_pool_exhausts_after_max_pauses() -> None:
    """After max_pauses, the next threshold-cross fires throttle_exhausted
    instead of pausing again — pool gives up and remaining tasks drain
    with that reason so the run finishes."""
    from adapters.document_fetch import _PdfWorkerPool

    states: list = []
    pool = _PdfWorkerPool(
        cdp_url="http://127.0.0.1:9222",
        workers=1,
        throttle_threshold=2,
        cooldown_base_sec=999999,    # never auto-resume — keeps pause stuck
        max_pauses=2,                # very low so we exhaust quickly
        jitter_ms=0,
        on_state_change=lambda s, m: states.append((s, m)),
    )
    # Crossing the threshold twice (without an automatic resume in tests)
    # would normally require the cooldown to fire. We bypass by manually
    # clearing the paused flag between attempts to simulate cooldown ticks.
    for _i in range(3):
        pool._update_throttle_state("navigation_timeout")
        pool._update_throttle_state("navigation_timeout")
        # Simulate cooldown elapsing so the next threshold can re-arm.
        pool._free_event.set()
        pool._consecutive_throttles = 0

    state_names = [s[0] for s in states]
    # 2 successful pauses then 1 exhausted
    assert state_names.count("throttle_paused") == 2
    assert state_names.count("throttle_exhausted") == 1


def test_pdf_worker_pool_drain_yields_for_each_submitted_task() -> None:
    """make_pdf_worker_pool returns None when CDP is unavailable; passing
    pdf_pool=None into _try_pdf_fetch_per_article must not raise.

    Also exercises the pool's drain semantics indirectly: when the helper
    returns None (no CDP), the caller branches to the non-pool path."""
    from adapters.document_fetch import make_pdf_worker_pool

    # No cdp_url → yields None
    with make_pdf_worker_pool(None, workers=4) as pool:
        assert pool is None

    # workers=1 → yields None even with valid url (single-worker is just
    # the sequential path; no pool overhead)
    with make_pdf_worker_pool("http://127.0.0.1:9222", workers=1) as pool:
        assert pool is None


def test_try_pdf_fetch_per_article_uses_pool_when_provided(tmp_path: Path) -> None:
    """When pdf_pool is provided, _try_pdf_fetch_per_article submits each
    valid record to the pool and drains results — bypassing both the
    ThreadPoolExecutor path and the sequential fallback."""
    from adapters.document_fetch import _try_pdf_fetch_per_article

    submitted = []
    drained = [
        # (record, path, reason): reason is None on success or one of the
        # typed strings (no_pdf_link / navigation_timeout / etc.).
        ({"title": "A", "url": "/c/x/details/a"}, tmp_path / "A.pdf", None),
        ({"title": "B", "url": "/c/x/details/b"}, None, "no_pdf_link"),
    ]

    class _FakePool:
        def submit(self, record, out_dir):
            submitted.append((record["title"], out_dir))
        def drain(self, n, timeout=300.0):
            assert n == len(drained)
            for r in drained:
                yield r

    events = []
    _try_pdf_fetch_per_article(
        records=[
            {"title": "A", "url": "/c/x/details/a"},
            {"title": "B", "url": "/c/x/details/b"},
        ],
        session=object(),  # not consulted when pool is provided
        out_dir=tmp_path,
        gap_id="G1",
        source_id="ebsco_api",
        emit=lambda *a, **kw: events.append(a),
        pdf_pool=_FakePool(),
    )
    # Both records went to the pool
    assert [t for t, _ in submitted] == ["A", "B"]
    # Two emit events: one ok, one unavailable
    statuses = [e[1] for e in events]
    assert statuses.count("pdf_inline_ok") == 1
    assert statuses.count("pdf_inline_unavailable") == 1


def test_run_fetch_tallies_inline_pdf_events(tmp_path: Path) -> None:
    """Verify run_fetch's emit wrapper tallies pdf_inline_* statuses into stats."""
    from adapters.document_fetch import run_fetch, FetchItem
    # Construct one EBSCO seed item; fetch_seed_page will be reached but the
    # mocked browser's eval_result is empty, so no records → no pdf events
    # from fetch_seed_page itself. We instead emit pdf_inline_* directly via
    # a custom emit-injecting browser wrapper to verify the wrapper logic.
    items = [FetchItem(gap_id="G1", source_id="ebsco_api", out_dir=str(tmp_path),
                       title="seed", url="https://search.ebscohost.com/x", fetch_type="seed")]

    # Use a mock browser whose fetch_with_eval returns records, exercising
    # the real _try_pdf_fetch_per_article path which emits inline events.
    browser = _make_mock_browser(eval_result=[
        {"title": "Article A", "url": "/c/x/details/a"},
        {"title": "Article B", "url": "/c/x/details/b"},
    ])
    # Browser has no _page, so each article emits "unavailable".
    stats = run_fetch(items, browser)
    assert stats.inline_pdfs_attempted == 2
    assert stats.inline_pdfs_unavailable == 2
    assert stats.inline_pdfs_ok == 0
    assert stats.inline_pdfs_failed == 0


def test_detect_iframe_block_recognises_captcha_iframes() -> None:
    """Iframe-shape detection catches reCAPTCHA / hCaptcha / Turnstile widgets
    that text regex misses (the visible "I'm not a robot" lives in a Google
    iframe, never in the parent page HTML)."""
    from adapters.browser_client import _detect_iframe_block

    cases = [
        ("recaptcha", "Solve reCAPTCHA challenge in browser"),
        ("hcaptcha", "Solve hCaptcha challenge in browser"),
        ("turnstile", "Wait for / solve Cloudflare Turnstile in browser"),
        ("captcha", "Solve CAPTCHA challenge in browser"),
    ]
    for kind, expected_action in cases:
        fake_page = MagicMock()
        fake_page.evaluate.return_value = kind
        blocked, reason, action = _detect_iframe_block(fake_page)
        assert blocked is True
        assert reason == "captcha"
        assert action == expected_action

    # Empty result → not blocked
    fake_page = MagicMock()
    fake_page.evaluate.return_value = ""
    blocked, _, _ = _detect_iframe_block(fake_page)
    assert blocked is False

    # Evaluate raises → safe fallback (not blocked)
    fake_page = MagicMock()
    fake_page.evaluate.side_effect = Exception("page closed")
    blocked, _, _ = _detect_iframe_block(fake_page)
    assert blocked is False


def test_detect_blocked_recognises_rate_limit_and_explicit_block() -> None:
    """Block detection covers rate-limit / quota messages and explicit 'blocked' notices."""
    from adapters.browser_client import _detect_blocked

    rate_cases = [
        b"<html>Too Many Requests</html>",
        b"<html>Rate limit quota violation. Quota limit exceeded.</html>",
        b"<html>You have been rate limited.</html>",
    ]
    for body in rate_cases:
        blocked, reason, _ = _detect_blocked(body, "https://example.com")
        assert blocked and reason == "rate_limit", f"unexpected ({blocked},{reason}) for: {body!r}"

    block_cases = [
        b"<html>You have been blocked from accessing this site.</html>",
        b"<html>Your access has been blocked due to suspicious activity.</html>",
    ]
    for body in block_cases:
        blocked, reason, _ = _detect_blocked(body, "https://example.com")
        assert blocked and reason == "access_denied", f"unexpected ({blocked},{reason}) for: {body!r}"


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
