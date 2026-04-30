"""Post-run document fetching library.

Takes a completed pipeline run's pull_output directory, identifies seed-only
results (provider search URLs, abstract-only records, PDF links), and uses the
attached Chrome CDP session to fetch actual article content.

Designed to be called from an API endpoint — no interactive prompts.  Progress
is reported via a caller-supplied emit function so it works with both the FastAPI
event stream and CLI callers.

Usage:
    from adapters.document_fetch import collect_fetch_items, run_fetch
    items = collect_fetch_items(pull_root)
    result = run_fetch(items, browser_client, emit_fn=print)
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

# JS expression for extracting EBSCO search-results records from the live DOM.
# Primary selectors target the modern research.ebsco.com SPA (data-auto-* attrs);
# legacy selectors are kept as fallbacks for older EBSCOhost skins.
_EBSCO_JS = """() => {
    const results = [];
    const containers = document.querySelectorAll(
        'article[data-auto="search-result-item"], '
      + '.result-list-item, article.record, [data-auto="record"], li.results-list-item'
    );
    containers.forEach((el, idx) => {
        if (idx >= 8) return;
        const getText = sel => { const n = el.querySelector(sel); return n ? n.innerText.trim() : ''; };
        const getAttr = (sel, attr) => { const n = el.querySelector(sel); return n ? (n.getAttribute(attr)||'').trim() : ''; };
        const title    = getText('[data-auto="result-item-title__link"]') || getText('[data-auto="result-item-title"]') || getText('.title-link') || getText('h3.title') || getText('a.record__title') || '';
        const authors  = getText('[data-auto="result-item-metadata-content--contributors"]') || getText('.authors-list') || getText('[data-auto="result-item-authors"]') || '';
        const source   = getText('[data-auto="result-item-metadata-content--published"]') || getText('.source-content') || getText('[data-auto="result-item-source"]') || '';
        const database = getText('[data-auto="result-item-metadata-content--database"]') || '';
        const date     = getText('.date-content') || getText('[data-auto="result-item-date"]') || '';
        const abstract = getText('[data-auto="abstract-content"]') || getText('.abstract-value') || getText('.record__abstract') || getText('.abstract-text') || '';
        const titleHrefRaw = getAttr('[data-auto="result-item-title__link"]', 'href');
        let titleHref = titleHrefRaw;
        try { if (titleHrefRaw) titleHref = new URL(titleHrefRaw, location.origin).href; } catch (e) {}
        const pdfLink  = getAttr('a[href*="pdfviewer"], a.pdf-link, [data-auto="pdf-link"]', 'href');
        if (title) results.push({title, authors, source, database, date, abstract, url: titleHref, pdf_url: pdfLink});
    });
    return results;
}"""

# JS expression for extracting JSTOR search-results records.
_JSTOR_JS = """() => {
    const results = [];
    document.querySelectorAll('li.result').forEach((el, idx) => {
        if (idx >= 8) return;
        const title    = (el.querySelector('.title a, h2 a') || {}).innerText || '';
        const authors  = (el.querySelector('.authors') || {}).innerText || '';
        const pub      = (el.querySelector('.source') || {}).innerText || '';
        const date     = (el.querySelector('.date, time') || {}).innerText || '';
        const abstract = (el.querySelector('.abstract') || {}).innerText || '';
        const href     = (el.querySelector('.title a, h2 a') || {}).href || '';
        if (title) results.push({title, authors, source: pub, date, abstract, url: href});
    });
    return results;
}"""

# JS expression for extracting Project MUSE search results.
_MUSE_JS = """() => {
    const results = [];
    document.querySelectorAll('.search-result, .result-item, article').forEach((el, idx) => {
        if (idx >= 8) return;
        const title   = (el.querySelector('h2, h3, .title, a.record-title') || {}).innerText || '';
        const authors = (el.querySelector('.authors, .contributor') || {}).innerText || '';
        const source  = (el.querySelector('.journal-title, .source') || {}).innerText || '';
        const date    = (el.querySelector('.date, .year, time') || {}).innerText || '';
        const href    = (el.querySelector('h2 a, h3 a, a.record-title') || {}).href || '';
        if (title) results.push({title, authors, source, date, abstract: '', url: href});
    });
    return results;
}"""

MAX_ARTICLES   = 8       # max articles to extract per search-results page
FETCH_SUBDIR   = "fetched"
FETCH_TIMEOUT  = 30      # seconds per HTTP fetch

# Per-source result-list anchor selectors for content-driven waits in
# fetch_with_eval. When the anchor appears, results are ready and we evaluate
# immediately (typically much faster than the fixed wait_ms). If the anchor
# never appears within wait_ms, we still evaluate so empty/soft-fail pages are
# captured. Anchors include both modern and legacy selectors so older skins
# still resolve.
_WAIT_SELECTORS = {
    "ebsco_api":    'article[data-auto="search-result-item"], article.record, .result-list-item',
    "ebscohost":    'article[data-auto="search-result-item"], article.record, .result-list-item',
    "jstor":        'li.result',
    "project_muse": '.search-result, .result-item, article',
}
# Max wait for the anchor to appear before falling through to evaluation.
# 5 s gives SPA-heavy result pages (EBSCO/ProQuest) time to render but is
# rarely paid in full because the selector usually appears within ~1-2 s.
_WAIT_FOR_SELECTOR_MS = 5000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FetchItem:
    """A single item to fetch from a pull-output directory."""

    gap_id: str
    source_id: str
    out_dir: str           # source pull directory (fetched/ written here)
    title: str
    url: str
    fetch_type: str        # "seed" | "pdf" | "abstract"
    abstract: str = ""
    authors: str = ""
    journal: str = ""
    pub_date: str = ""
    doi: str = ""


@dataclass
class FetchDocumentsStats:
    """Summary counts returned by run_fetch()."""

    items_found: int = 0
    abstracts_saved: int = 0
    seeds_attempted: int = 0
    seeds_ok: int = 0
    seeds_failed: int = 0
    pdfs_attempted: int = 0
    pdfs_ok: int = 0
    pdfs_failed: int = 0
    articles_extracted: int = 0
    # Per-article click-in PDF fetch (run after search-results metadata
    # extraction): visits each article's detail page, looks for the PDF
    # viewer link, captures the PDF bytes from the response stream.
    inline_pdfs_attempted: int = 0
    inline_pdfs_ok: int = 0
    inline_pdfs_unavailable: int = 0   # no PDF link on the detail page
    inline_pdfs_failed: int = 0        # link found but capture failed
    inline_pdfs_throttled: int = 0     # navigation timed out (likely rate-limit)
    # Pool-level throttle events (one per pause, one per resume, one per exhaust).
    throttle_pauses: int = 0
    throttle_resumes: int = 0
    throttle_exhausted: int = 0

    @property
    def total_ok(self) -> int:
        return self.abstracts_saved + self.seeds_ok + self.pdfs_ok + self.inline_pdfs_ok


# ---------------------------------------------------------------------------
# Collect items from pull_output directories
# ---------------------------------------------------------------------------


def collect_fetch_items(
    pull_root: Path,
    *,
    gap_filter: Optional[str] = None,
    limit: Optional[int] = None,
    skip_already_fetched: bool = True,
) -> List[FetchItem]:
    """Walk a run's pull_output directory and return actionable fetch items.

    Classifies each JSON record as:
    - ``seed``     — provider search URL (quality_label == seed, needs browser)
    - ``pdf``      — direct PDF link (pdf_url present)
    - ``abstract`` — abstract-only medium/high record (no PDF; save immediately)
    """

    items: List[FetchItem] = []
    if not pull_root.exists():
        return items

    for gap_dir in sorted(pull_root.iterdir()):
        if not gap_dir.is_dir():
            continue
        gap_id = gap_dir.name
        if gap_filter and gap_id != gap_filter:
            continue

        for src_dir in sorted(gap_dir.iterdir()):
            if not src_dir.is_dir():
                continue
            source_id = src_dir.name

            # Skip the fetched sub-directory itself
            if source_id == FETCH_SUBDIR:
                continue

            for json_file in sorted(src_dir.glob("*.json")):
                try:
                    payload = json.loads(json_file.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    continue
                records = payload if isinstance(payload, list) else [payload]

                for rec in records:
                    if not isinstance(rec, dict):
                        continue
                    item = _classify_record(rec, gap_id, source_id, src_dir, skip_already_fetched)
                    if item:
                        items.append(item)

        if limit and len(items) >= limit:
            break

    return items[:limit] if limit else items


def _classify_record(
    rec: Dict[str, Any],
    gap_id: str,
    source_id: str,
    out_dir: Path,
    skip_already_fetched: bool,
) -> Optional[FetchItem]:
    """Return a FetchItem if this record has actionable content, else None."""

    ql       = str(rec.get("quality_label", "")).lower()
    url      = str(rec.get("url", "") or rec.get("pdf_url", "")).strip()
    pdf_url  = str(rec.get("pdf_url", "")).strip()
    abstract = str(rec.get("abstract", "")).strip()
    title    = str(rec.get("title", "")).strip()

    base = dict(
        gap_id=gap_id,
        source_id=source_id,
        out_dir=str(out_dir),
        title=title,
        url=url,
    )

    fetch_dir = out_dir / FETCH_SUBDIR

    # Already fetched — skip if requested
    if skip_already_fetched and title:
        slug = _slugify(title)[:60]
        if (fetch_dir / f"{slug}.md").exists() or (fetch_dir / f"{slug}.pdf").exists():
            return None

    if pdf_url:
        return FetchItem(**{**base, "url": pdf_url}, fetch_type="pdf")

    if ql == "seed" and url.startswith("http"):
        return FetchItem(**base, fetch_type="seed")

    if abstract and ql in ("medium", "high") and not pdf_url:
        return FetchItem(
            **base,
            fetch_type="abstract",
            abstract=abstract,
            authors=str(rec.get("authors", "")),
            journal=str(rec.get("journal", "")),
            pub_date=str(rec.get("pub_date", "")),
            doi=str(rec.get("doi", "")),
        )

    return None


def preview_counts(pull_root: Path, gap_filter: Optional[str] = None) -> Dict[str, int]:
    """Return item-count breakdown without building full FetchItem list."""
    items = collect_fetch_items(pull_root, gap_filter=gap_filter, skip_already_fetched=True)
    counts: Dict[str, int] = {"seed": 0, "pdf": 0, "abstract": 0, "total": len(items)}
    for item in items:
        counts[item.fetch_type] = counts.get(item.fetch_type, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Abstract saver (no network needed)
# ---------------------------------------------------------------------------


def save_abstract(item: FetchItem) -> Path:
    """Write an abstract-only record as a markdown file."""
    out = Path(item.out_dir) / FETCH_SUBDIR
    out.mkdir(parents=True, exist_ok=True)
    slug = _slugify(item.title or item.url)[:60]
    path = out / f"{slug}.md"
    if path.exists():
        return path
    lines = [
        f"# {item.title or '(untitled)'}",
        "",
        f"**Authors:** {item.authors or '—'}  ",
        f"**Journal:** {item.journal or '—'}  ",
        f"**Date:** {item.pub_date or '—'}  ",
        f"**DOI:** {item.doi or '—'}  ",
        "",
        "## Abstract",
        "",
        item.abstract or "_(no abstract)_",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Seed page fetch (browser, JS-evaluated DOM extraction)
# ---------------------------------------------------------------------------


def fetch_seed_page(
    item: FetchItem,
    browser_client: Any,
    *,
    emit: Optional[Callable] = None,
    on_blocked: Optional[Callable] = None,
    pdf_pool: Optional["_PdfWorkerPool"] = None,
) -> int:
    """Navigate a seed search URL, extract article records, save as markdown.

    Returns the number of article files written.

    Parameters
    ----------
    on_blocked:
        Optional ``on_blocked(item, page_result) -> bool`` handler. Invoked when
        the first fetch returns a blocked page (CAPTCHA / login wall / access
        denied). If the handler returns True, the URL is re-fetched once — used
        by the CLI to pause for the user to solve a CAPTCHA in the live browser
        before continuing.
    pdf_pool:
        Optional persistent ``_PdfWorkerPool`` for parallel per-article PDF
        fetch. When provided, each extracted record is submitted to the pool;
        when ``None``, the per-article PDF fetch uses the legacy
        ``ThreadPoolExecutor`` path (per-task setup overhead) or sequential
        single-page (for non-CDP sessions).
    """

    source_id = item.source_id
    out_dir   = Path(item.out_dir) / FETCH_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # Select JS extractor by source
    if source_id in ("ebsco_api", "ebscohost"):
        js_expr = _EBSCO_JS
    elif source_id == "jstor":
        js_expr = _JSTOR_JS
    elif source_id == "project_muse":
        js_expr = _MUSE_JS
    else:
        js_expr = None  # generic: save HTML only

    wait_for = _WAIT_SELECTORS.get(source_id)
    # When we have an anchor we can wait longer (up to _WAIT_FOR_SELECTOR_MS)
    # because we'll return as soon as it appears; without an anchor, keep the
    # legacy 2500 ms fixed wait so non-SPA pages don't block unnecessarily.
    wait_ms = _WAIT_FOR_SELECTOR_MS if wait_for else 2500

    def _attempt():
        if js_expr:
            return browser_client.fetch_with_eval(
                item.url, js_expr, wait_ms=wait_ms, wait_for=wait_for,
            )
        return browser_client.fetch(item.url), None

    page_result, eval_result = _attempt()

    # Surface blocked pages to the emit function so users can act
    if page_result.blocked:
        if emit:
            emit(
                "fetching",
                "blocked",
                f"[{item.gap_id}] {source_id}: {page_result.action_required or page_result.blocked_reason}",
                {
                    "gap_id": item.gap_id,
                    "source_id": source_id,
                    "blocked_reason": page_result.blocked_reason,
                    "action_required": page_result.action_required,
                    "url": item.url[:80],
                },
            )

        # Give the caller a chance to unblock (e.g., user solves CAPTCHA in
        # the live browser) and retry once.
        retry = False
        if on_blocked is not None:
            try:
                retry = bool(on_blocked(item, page_result))
            except Exception:
                retry = False
        if retry:
            page_result, eval_result = _attempt()
            if not page_result.blocked and emit:
                emit(
                    "fetching",
                    "unblocked",
                    f"[{item.gap_id}] {source_id}: retry succeeded after manual unblock",
                    {"gap_id": item.gap_id, "source_id": source_id},
                )

        if page_result.blocked:
            # Still blocked after retry (or no retry attempted) — save HTML
            # for manual inspection and skip extraction.
            _save_html(page_result.content, out_dir, suffix="_blocked")
            return 0

    # Save raw HTML for all providers (manual review backup)
    _save_html(page_result.content, out_dir)

    # Extract structured records
    if source_id in ("ebsco_api", "ebscohost"):
        count = _write_ebsco_records(eval_result or [], out_dir)
        # After metadata is saved, click into each article's detail page and
        # try to capture the actual PDF when EBSCO offers one. Emits per-
        # article events (pdf_inline_ok / unavailable / failed); run_fetch
        # tallies them into FetchDocumentsStats via its emit wrapper.
        _try_pdf_fetch_per_article(
            eval_result or [], browser_client, out_dir,
            gap_id=item.gap_id, source_id=source_id, emit=emit,
            pdf_pool=pdf_pool,
        )
        return count
    if source_id == "jstor":
        return _write_jstor_records(eval_result or [], out_dir)
    if source_id == "project_muse":
        return _write_muse_records(eval_result or [], out_dir)

    # Generic: HTML saved is count 1
    return 1


# ---------------------------------------------------------------------------
# PDF downloader
# ---------------------------------------------------------------------------


def download_pdf(
    item: FetchItem,
    browser_client: Any,
    *,
    emit: Optional[Callable] = None,
) -> Path:
    """Download a PDF URL to the fetched/ directory.

    Tries direct HTTP first (fast, no browser needed), then falls back to
    the CDP-backed browser session (respects authenticated cookie state).
    Raises RuntimeError if both fail.
    """

    out_dir = Path(item.out_dir) / FETCH_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(item.title or item.url)[:60]
    path = out_dir / f"{slug}.pdf"

    if path.exists():
        return path

    # Try direct HTTP (no JS, no auth — works for open-access PDFs)
    try:
        req = urllib.request.Request(
            item.url,
            headers={"User-Agent": "Mozilla/5.0 (research tool)"},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            content = resp.read(20_000_000)
        if content[:4] == b"%PDF" or content[:5] == b"\x25PDF":
            path.write_bytes(content)
            return path
    except Exception:
        pass

    # Fall back to CDP-backed fetch (authenticated session)
    try:
        result = browser_client.fetch(item.url)
        content = result.content
        if content and (content[:4] == b"%PDF" or content[:5] == b"\x25PDF"):
            path.write_bytes(content)
            return path
    except Exception:
        pass

    raise RuntimeError(f"Could not retrieve PDF: {item.url[:80]}")


# ---------------------------------------------------------------------------
# Per-article click-in PDF fetch (EBSCO research.ebsco.com)
# ---------------------------------------------------------------------------

# Base host EBSCO article URLs are relative to (extracted records use
# /c/<opid>/search/details/<id>?... paths).
_EBSCO_BASE_HOST = "https://research.ebsco.com"

# JS used by download_article_pdf to find the PDF viewer link on the
# article detail page. Returns the relative path or null.
_EBSCO_PDF_LINK_JS = """() => {
    const a = document.querySelector('a[href*="/viewer/pdf/"]');
    return a ? a.getAttribute('href') : null;
}"""


# PDF-fetch reason strings (returned by _download_pdf_with_page_detailed):
#   None                      — success (path is set)
#   "no_pdf_link"             — detail page rendered but has no /viewer/pdf/ link
#                               (genuine: many EBSCO records are abstract-only)
#   "navigation_timeout"      — page.goto timed out — almost always EBSCO throttle
#                               or a slow-walked response under per-IP rate limit
#   "navigation_error"        — non-timeout exception during navigation
#   "viewer_capture_failed"   — viewer URL loaded but no application/pdf response
#   "write_failed"            — PDF captured but disk write raised


def _is_playwright_timeout(exc: BaseException) -> bool:
    """True if exc is Playwright's TimeoutError (page.goto / waits)."""
    try:
        from playwright.sync_api import TimeoutError as _PWTimeout  # type: ignore
        return isinstance(exc, _PWTimeout)
    except Exception:
        # Fall back to class-name match if import fails.
        return exc.__class__.__name__ == "TimeoutError"


def _download_pdf_with_page_detailed(
    page: Any,
    record: Dict[str, Any],
    out_dir: Path,
    *,
    detail_wait_ms: int = 1500,
    viewer_wait_ms: int = 3000,
) -> Tuple[Optional[Path], Optional[str]]:
    """Core PDF-fetch logic, returning ``(path, reason)``.

    Shared between the sequential session-based path and parallel worker
    threads. Best-effort: never raises. Distinguishes navigation timeouts
    (likely throttle) from genuine "no PDF link" so the pool can react.
    """
    title = (record.get("title") or "").strip()
    raw_url = (record.get("url") or "").strip()
    if not title or not raw_url:
        return None, "no_pdf_link"

    slug = _slugify(title)[:60]
    pdf_path = out_dir / f"{slug}.pdf"
    if pdf_path.exists():
        return pdf_path, None

    abs_url = raw_url if raw_url.startswith("http") else f"{_EBSCO_BASE_HOST}{raw_url}"

    # Step 1: detail page → look for the viewer link.
    try:
        page.goto(abs_url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(detail_wait_ms)
    except Exception as exc:
        return None, ("navigation_timeout" if _is_playwright_timeout(exc) else "navigation_error")

    try:
        viewer_href = page.evaluate(_EBSCO_PDF_LINK_JS)
    except Exception:
        viewer_href = None
    if not viewer_href:
        return None, "no_pdf_link"

    viewer_url = (
        viewer_href if viewer_href.startswith("http")
        else f"{_EBSCO_BASE_HOST}{viewer_href}"
    )

    # Step 2: viewer page with response listener — captures the PDF body.
    captured: Dict[str, Any] = {}
    viewer_timed_out = False

    def _on_response(resp):
        if "pdf" in captured:
            return
        try:
            ct = (resp.headers.get("content-type") or "").lower()
        except Exception:
            return
        if "application/pdf" not in ct:
            return
        try:
            body = resp.body()
        except Exception:
            return
        if body and body[:4] == b"%PDF":
            captured["pdf"] = body

    page.on("response", _on_response)
    try:
        try:
            page.goto(viewer_url, timeout=45000, wait_until="networkidle")
        except Exception as exc:
            # networkidle commonly times out before the PDF response fully
            # arrives — the listener may have already captured it. Track
            # whether this was a nav timeout so we can flag throttle on
            # an empty capture.
            if _is_playwright_timeout(exc):
                viewer_timed_out = True
        if "pdf" not in captured:
            page.wait_for_timeout(viewer_wait_ms)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    if "pdf" not in captured:
        # Empty capture + viewer goto timed out → likely throttle on the
        # viewer endpoint specifically. Empty without timeout → viewer
        # didn't serve a PDF (older record / region-locked / etc.).
        return None, ("navigation_timeout" if viewer_timed_out else "viewer_capture_failed")

    try:
        pdf_path.write_bytes(captured["pdf"])
        return pdf_path, None
    except Exception:
        return None, "write_failed"


def _download_pdf_with_page(
    page: Any,
    record: Dict[str, Any],
    out_dir: Path,
    *,
    detail_wait_ms: int = 1500,
    viewer_wait_ms: int = 3000,
) -> Optional[Path]:
    """Backward-compatible wrapper: returns ``Optional[Path]`` only."""
    path, _reason = _download_pdf_with_page_detailed(
        page, record, out_dir,
        detail_wait_ms=detail_wait_ms,
        viewer_wait_ms=viewer_wait_ms,
    )
    return path


def download_article_pdf(
    record: Dict[str, Any],
    session: Any,
    out_dir: Path,
    *,
    emit: Optional[Callable] = None,
    detail_wait_ms: int = 1500,
    viewer_wait_ms: int = 3000,
) -> Optional[Path]:
    """Visit an EBSCO article's detail page, find the PDF viewer URL, and
    capture the PDF bytes from the page's response stream.

    Pattern (validated end-to-end against research.ebsco.com):
      1. Navigate to the article's detail URL (from extracted record).
      2. Look for ``a[href*="/viewer/pdf/"]`` in the DOM.
      3. If absent, the article has no PDF available — return None (this is
         a normal outcome for many EBSCO records, especially older ones).
      4. If present, attach a response listener and navigate to the viewer
         URL. The viewer page triggers a GET to ``content.ebscohost.com``
         that returns ``Content-Type: application/pdf``.
      5. Listener captures the body bytes. Write to ``<slug>.pdf`` and
         return the path.

    Best-effort: never raises, never blocks the metadata pipeline. Returns
    None for any failure (no PDF, page error, capture timeout, etc.).
    Requires a CDP-backed session (``session._page`` available); for HTTP
    or claude_cu providers, returns None immediately.
    """
    title = (record.get("title") or "").strip()
    raw_url = (record.get("url") or "").strip()
    if not title or not raw_url:
        return None

    # Persistent CDP page; non-CDP sessions can't click into detail pages.
    page = getattr(session, "_page", None)
    if page is None:
        return None

    return _download_pdf_with_page(
        page, record, out_dir,
        detail_wait_ms=detail_wait_ms,
        viewer_wait_ms=viewer_wait_ms,
    )


class _PdfWorkerPool:
    """Persistent pool of N worker threads, each owning its own Playwright
    session + page for the lifetime of the pool.

    Concurrency model
    -----------------
    - N worker threads, started in ``__enter__``, joined in ``__exit__``.
    - Each worker enters its own ``sync_playwright`` block, connects to the
      same CDP browser (so cookies / auth are shared), and opens one
      persistent page.
    - Workers consume ``(record, out_dir)`` tasks from a shared queue and
      push ``(record, path, reason)`` to a results queue. ``reason`` is
      ``None`` on success or one of the typed strings documented above
      ``_download_pdf_with_page_detailed`` (no_pdf_link / navigation_timeout
      / viewer_capture_failed / etc.).
    - ``None`` on the task queue is the shutdown sentinel — one per worker
      sent on exit.

    Throttle handling
    -----------------
    EBSCO rate-limits per-IP under sustained concurrent load — observed live
    when running 4 parallel workers, EBSCO began slow-walking detail-page
    responses past the ``page.goto`` timeout. Without a counter-measure,
    timeouts fall through as "no_pdf_link" and the pipeline silently misses
    a window of articles.

    The pool now:
    1. Counts consecutive ``navigation_timeout`` results across all workers.
    2. When the count reaches ``throttle_threshold``, sets a pause flag.
       Workers consult the flag before pulling each task and block until
       it clears.
    3. A cooldown thread sleeps for ``cooldown_base_sec * (1 + total_pauses)``
       seconds (linear backoff: 5min, 10min, 15min...) then clears the
       flag — workers resume.
    4. After ``max_pauses`` pauses, the pool gives up and drains all
       remaining tasks with reason ``throttle_exhausted`` so the run can
       finish gracefully without another infinite throttle loop.
    5. Each state transition (``throttled`` / ``resumed`` / ``exhausted``)
       triggers ``on_state_change`` callback so the CLI can emit events,
       ping Telegram, and surface the pause to the user.

    Inter-request jitter
    --------------------
    Workers sleep ``random.uniform(0, jitter_ms)`` ms before each task so
    the request stream looks less mechanical and reduces same-instant
    bursts to EBSCO's edge.

    Errors during worker startup (Playwright import error, CDP connect
    failure) are propagated by draining outstanding tasks with a fixed
    reason string so callers don't hang.
    """

    # Default tuning (overridable via env vars; see make_pdf_worker_pool)
    DEFAULT_THROTTLE_THRESHOLD = 3
    DEFAULT_COOLDOWN_BASE_SEC  = 300        # 5 min × 1, 2, 3, ... linear
    DEFAULT_MAX_PAUSES         = 3
    DEFAULT_JITTER_MS          = 800

    def __init__(
        self,
        cdp_url: str,
        workers: int,
        *,
        throttle_threshold: int = DEFAULT_THROTTLE_THRESHOLD,
        cooldown_base_sec:  int = DEFAULT_COOLDOWN_BASE_SEC,
        max_pauses:         int = DEFAULT_MAX_PAUSES,
        jitter_ms:          int = DEFAULT_JITTER_MS,
        on_state_change:    Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.cdp_url = cdp_url
        self.workers = max(1, workers)
        self.throttle_threshold = max(1, throttle_threshold)
        self.cooldown_base_sec  = max(0, cooldown_base_sec)
        self.max_pauses         = max(0, max_pauses)
        self.jitter_ms          = max(0, jitter_ms)
        self._on_state_change   = on_state_change

        self._task_queue: "queue.Queue[Optional[Tuple[Dict[str, Any], Path]]]" = queue.Queue()
        self._result_queue: "queue.Queue[Tuple[Dict[str, Any], Optional[Path], Optional[str]]]" = queue.Queue()
        self._threads: List[threading.Thread] = []
        self._submitted = 0

        # Throttle state — protected by self._lock.
        self._lock                  = threading.RLock()
        self._consecutive_throttles = 0
        self._total_pauses          = 0
        self._exhausted             = False
        # Event semantics: SET = workers are FREE to run (no pause).
        # CLEAR = workers must wait. Start in SET state.
        self._free_event            = threading.Event()
        self._free_event.set()

    @property
    def total_pauses(self) -> int:
        with self._lock:
            return self._total_pauses

    @property
    def is_paused(self) -> bool:
        return not self._free_event.is_set()

    def __enter__(self) -> "_PdfWorkerPool":
        for i in range(self.workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"pdf-worker-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        return self

    def __exit__(self, *_: Any) -> None:
        # Make sure workers aren't blocked on a paused event when we shut
        # down — release them so they can pull the sentinel.
        self._free_event.set()
        for _w in range(self.workers):
            self._task_queue.put(None)
        for t in self._threads:
            t.join(timeout=15)

    def submit(self, record: Dict[str, Any], out_dir: Path) -> None:
        """Queue one article for processing."""
        self._task_queue.put((record, out_dir))
        self._submitted += 1

    def drain(self, n: int, timeout: float = 1800.0) -> Iterator[Tuple[Dict[str, Any], Optional[Path], Optional[str]]]:
        """Yield exactly ``n`` results in arrival order (not submission order).

        Per-result timeout is generous (default 30 min) because workers may
        be paused for a cooldown that exceeds the previous 5-min default —
        a paused pool isn't a stuck pool.
        """
        for _ in range(n):
            try:
                yield self._result_queue.get(timeout=timeout)
            except queue.Empty:
                yield ({}, None, "worker_pool_timeout")

    # -- internals -----------------------------------------------------

    def _worker_loop(self) -> None:
        """Run forever: consume tasks, process them on the persistent page."""
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            self._drain_with_reason("playwright_not_installed")
            return

        try:
            from adapters.cdp_utils import effective_cdp_url
        except ImportError:
            self._drain_with_reason("cdp_utils_unavailable")
            return

        effective = effective_cdp_url(self.cdp_url)
        import random as _random

        try:
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.connect_over_cdp(effective)
                except Exception as exc:  # noqa: BLE001
                    self._drain_with_reason(f"cdp_connect_failed: {exc!s:.60}")
                    return

                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
                try:
                    while True:
                        # Wait while the pool is paused for a throttle cooldown.
                        # Use timed waits so we can still respond to shutdown.
                        while not self._free_event.wait(timeout=1.0):
                            if self._exhausted:
                                # Pool gave up; drain remaining tasks with error.
                                self._drain_with_reason("throttle_exhausted")
                                return

                        task = self._task_queue.get()
                        if task is None:
                            return
                        record, out_dir = task

                        # Inter-request jitter — spread out the request
                        # stream from this thread.
                        if self.jitter_ms > 0:
                            time.sleep(_random.uniform(0, self.jitter_ms) / 1000.0)

                        try:
                            path, reason = _download_pdf_with_page_detailed(
                                page, record, out_dir,
                            )
                        except Exception as exc:  # noqa: BLE001
                            path, reason = None, f"worker_exception: {exc!s:.60}"

                        self._result_queue.put((record, path, reason))
                        self._update_throttle_state(reason)
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
        except Exception as exc:  # noqa: BLE001
            self._drain_with_reason(f"worker_init_failed: {exc!s:.60}")

    def _update_throttle_state(self, reason: Optional[str]) -> None:
        """Track consecutive throttles; trigger pause when threshold crossed."""
        with self._lock:
            if reason == "navigation_timeout":
                self._consecutive_throttles += 1
                if (
                    self._consecutive_throttles >= self.throttle_threshold
                    and self._free_event.is_set()        # not already paused
                    and not self._exhausted
                ):
                    self._initiate_pause_locked()
            elif reason is None or reason == "no_pdf_link":
                # Reset only on outcomes that indicate EBSCO is responding
                # promptly. Ambiguous reasons (write_failed, viewer_capture_failed,
                # worker_exception) leave the counter alone — they shouldn't
                # cancel a brewing throttle but shouldn't escalate it either.
                self._consecutive_throttles = 0

    def _initiate_pause_locked(self) -> None:
        """Called under self._lock. Pause workers; spawn cooldown thread."""
        self._total_pauses += 1
        if self._total_pauses > self.max_pauses:
            self._exhausted = True
            self._notify("throttle_exhausted", {
                "total_pauses": self._total_pauses - 1,
                "max_pauses":   self.max_pauses,
            })
            return

        cooldown_sec = self.cooldown_base_sec * self._total_pauses
        self._free_event.clear()                  # block workers
        self._notify("throttle_paused", {
            "consecutive_throttles": self._consecutive_throttles,
            "cooldown_sec":          cooldown_sec,
            "total_pauses":          self._total_pauses,
        })
        threading.Thread(
            target=self._cooldown_thread,
            args=(cooldown_sec,),
            daemon=True,
            name=f"pdf-pool-cooldown-{self._total_pauses}",
        ).start()

    def _cooldown_thread(self, cooldown_sec: int) -> None:
        time.sleep(cooldown_sec)
        with self._lock:
            self._consecutive_throttles = 0
            self._free_event.set()                # release workers
            self._notify("throttle_resumed", {
                "cooldown_sec":  cooldown_sec,
                "total_pauses":  self._total_pauses,
            })

    def _notify(self, state: str, meta: Dict[str, Any]) -> None:
        """Best-effort callback fire — never raises."""
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(state, meta)
        except Exception:
            pass

    def _drain_with_reason(self, reason: str) -> None:
        """Empty the task queue, putting a synthetic reason per task so
        callers don't hang waiting for results."""
        while True:
            task = self._task_queue.get()
            if task is None:
                return
            record, _ = task
            self._result_queue.put((record, None, reason))


@contextlib.contextmanager
def make_pdf_worker_pool(
    cdp_url: Optional[str],
    workers: int,
    *,
    throttle_threshold: Optional[int] = None,
    cooldown_base_sec:  Optional[int] = None,
    max_pauses:         Optional[int] = None,
    jitter_ms:          Optional[int] = None,
    on_state_change:    Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Iterator[Optional[_PdfWorkerPool]]:
    """Yield an active worker pool when CDP is reachable; ``None`` otherwise.

    Use as ``with make_pdf_worker_pool(url, n, ...) as pool: ...``. Callers
    branch on ``pool is None`` to fall back to non-pool behavior.

    Tuning knobs (each accepts an explicit override; ``None`` falls back to
    the matching ``ORCH_PDF_*`` env var, then to the class defaults):

    - ``throttle_threshold`` — consecutive timeouts that trigger a pause.
    - ``cooldown_base_sec``  — base cooldown; doubled, tripled, ... on each
      successive pause (linear backoff: 5min, 10min, 15min, ...).
    - ``max_pauses``         — give up and drain remaining tasks after this
      many pauses to avoid an infinite throttle loop.
    - ``jitter_ms``          — per-worker per-task random sleep upper bound
      to spread out the request stream.
    """
    if not cdp_url or not isinstance(cdp_url, str) or workers <= 1:
        yield None
        return

    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, str(default)))
        except (TypeError, ValueError):
            return default

    pool = _PdfWorkerPool(
        cdp_url, workers,
        throttle_threshold=(
            throttle_threshold
            if throttle_threshold is not None
            else _env_int("ORCH_PDF_THROTTLE_THRESHOLD", _PdfWorkerPool.DEFAULT_THROTTLE_THRESHOLD)
        ),
        cooldown_base_sec=(
            cooldown_base_sec
            if cooldown_base_sec is not None
            else _env_int("ORCH_PDF_THROTTLE_COOLDOWN_SEC", _PdfWorkerPool.DEFAULT_COOLDOWN_BASE_SEC)
        ),
        max_pauses=(
            max_pauses
            if max_pauses is not None
            else _env_int("ORCH_PDF_MAX_THROTTLE_PAUSES", _PdfWorkerPool.DEFAULT_MAX_PAUSES)
        ),
        jitter_ms=(
            jitter_ms
            if jitter_ms is not None
            else _env_int("ORCH_PDF_JITTER_MS", _PdfWorkerPool.DEFAULT_JITTER_MS)
        ),
        on_state_change=on_state_change,
    )
    pool.__enter__()
    try:
        yield pool
    finally:
        pool.__exit__(None, None, None)


def _fetch_pdf_in_worker(
    record: Dict[str, Any],
    cdp_url: str,
    out_dir: Path,
) -> Tuple[Dict[str, Any], Optional[Path], Optional[str]]:
    """Worker function for parallel PDF fetch.

    Each call creates its own ``sync_playwright`` session (Playwright sync
    state is per-thread-not-shareable), connects to the same CDP browser,
    opens a transient page, runs ``_download_pdf_with_page``, and tears
    down. Multiple workers run truly concurrently because each owns
    independent Playwright state but shares the browser's cookie jar.

    Returns ``(record, path_or_None, error_or_None)``.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return record, None, "playwright_not_installed"

    from adapters.cdp_utils import effective_cdp_url  # local import
    effective = effective_cdp_url(cdp_url)

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(effective)
            except Exception as exc:  # noqa: BLE001
                return record, None, f"cdp_connect_failed: {exc!s:.60}"
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                path, reason = _download_pdf_with_page_detailed(page, record, out_dir)
                return record, path, reason
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        return record, None, str(exc)[:120]


def _emit_pdf_outcome(
    record: Dict[str, Any],
    path: Optional[Path],
    reason: Optional[str],
    *,
    gap_id: str,
    source_id: str,
    emit: Optional[Callable],
) -> None:
    """Translate a (path, reason) result into the pipeline's emit vocabulary.

    Reasons (from ``_download_pdf_with_page_detailed`` + the worker pool):
      None                  → ok (path is set)
      "no_pdf_link"         → unavailable (legitimate: article has no PDF)
      "navigation_timeout"  → throttled (likely EBSCO rate-limit)
      "throttle_exhausted"  → throttled (pool gave up after max pauses)
      "worker_pool_timeout" → failed (worker pool drain timed out)
      anything else         → failed (capture / write / worker error)
    """
    if not emit:
        return
    title_short = (record.get("title") or "")[:60]
    meta = {"gap_id": gap_id, "source_id": source_id, "title": title_short}
    if path is not None and not reason:
        emit("fetching", "pdf_inline_ok",
             f"[{gap_id}] PDF saved: {path.name}",
             {**meta, "file": path.name})
    elif reason == "no_pdf_link":
        emit("fetching", "pdf_inline_unavailable",
             f"[{gap_id}] no PDF for: {title_short}", meta)
    elif reason in ("navigation_timeout", "throttle_exhausted"):
        emit("fetching", "pdf_inline_throttled",
             f"[{gap_id}] throttled ({reason}) on: {title_short}",
             {**meta, "reason": reason})
    else:
        emit("fetching", "pdf_inline_failed",
             f"[{gap_id}] PDF errored for: {title_short} — {(reason or 'unknown')[:60]}",
             {**meta, "reason": reason or "unknown"})


def _try_pdf_fetch_per_article(
    records: List[Dict[str, Any]],
    session: Any,
    out_dir: Path,
    *,
    gap_id: str,
    source_id: str,
    emit: Optional[Callable] = None,
    pdf_pool: Optional["_PdfWorkerPool"] = None,
) -> None:
    """For each extracted EBSCO article record, attempt a click-in PDF fetch.

    Emits one event per article:
      - ``pdf_inline_ok``          — PDF bytes captured and saved
      - ``pdf_inline_unavailable`` — detail page has no /viewer/pdf/ link
      - ``pdf_inline_failed``      — link found but capture failed

    Concurrency model (in priority order)
    -------------------------------------
    1. **Persistent worker pool** (preferred): when ``pdf_pool`` is provided,
       articles are submitted to the pool's queue and results are drained as
       workers complete them. Workers reuse persistent pages across the
       whole run — no per-task playwright setup overhead.
    2. **Per-task ``ThreadPoolExecutor``**: when no pool is given but the
       session is CDP-backed and ``ORCH_PDF_WORKERS > 1``, each task spawns
       its own ``sync_playwright`` session. Pays ~1-2 s setup overhead per
       task; preserved for callers that don't manage a pool (e.g. direct
       library use, the FastAPI endpoint).
    3. **Sequential single-page**: when running against a non-CDP session
       (HTTP, mock) or workers=1, processes articles one at a time using
       ``session._page``.
    """
    valid = [r for r in (records or [])[:MAX_ARTICLES]
             if isinstance(r, dict) and r.get("title")]
    if not valid:
        return

    # ── 1. Persistent pool path (preferred when available) ──────────────
    if pdf_pool is not None:
        for rec in valid:
            pdf_pool.submit(rec, out_dir)
        for record, path, error in pdf_pool.drain(len(valid)):
            _emit_pdf_outcome(
                record, path, error,
                gap_id=gap_id, source_id=source_id, emit=emit,
            )
        return

    # ── 2. Per-task ThreadPoolExecutor path ─────────────────────────────
    workers = max(1, int(os.environ.get("ORCH_PDF_WORKERS", "4")))
    cdp_url = None
    client = getattr(session, "_client", None)
    if client is not None:
        candidate = getattr(client, "cdp_url", None)
        if isinstance(candidate, str) and candidate.startswith("http"):
            cdp_url = candidate

    can_parallelize = (
        workers > 1
        and cdp_url is not None
        and getattr(session, "_page", None) is not None
    )

    if can_parallelize:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(_fetch_pdf_in_worker, rec, cdp_url, out_dir)
                for rec in valid
            ]
            for fut in as_completed(futures):
                try:
                    record, path, error = fut.result()
                except Exception as exc:  # noqa: BLE001
                    record, path, error = {}, None, str(exc)[:120]
                _emit_pdf_outcome(
                    record, path, error,
                    gap_id=gap_id, source_id=source_id, emit=emit,
                )
        return

    # ── 3. Sequential fallback ──────────────────────────────────────────
    page = getattr(session, "_page", None)
    for rec in valid:
        if page is None:
            # Mock or non-CDP session — emit a placeholder unavailable.
            _emit_pdf_outcome(
                rec, None, "no_pdf_link",
                gap_id=gap_id, source_id=source_id, emit=emit,
            )
            continue
        try:
            path, reason = _download_pdf_with_page_detailed(page, rec, out_dir)
        except Exception as exc:  # noqa: BLE001
            path, reason = None, f"sequential_exception: {exc!s:.60}"
        _emit_pdf_outcome(
            rec, path, reason,
            gap_id=gap_id, source_id=source_id, emit=emit,
        )


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def run_fetch(
    items: List[FetchItem],
    browser_client: Any,
    *,
    emit: Optional[Callable] = None,
    on_blocked: Optional[Callable] = None,
) -> FetchDocumentsStats:
    """Execute the full fetch pass for a list of FetchItems.

    Parameters
    ----------
    items:
        Output of ``collect_fetch_items()``.
    browser_client:
        A ``BrowserClient`` instance (or any object with ``.fetch()``,
        ``.fetch_with_eval()``, and ``.is_available()`` methods).
    emit:
        Optional ``emit(stage, status, message, meta)`` callable used to send
        structured events to the run's event stream.
    on_blocked:
        Optional ``on_blocked(item, page_result) -> bool`` handler invoked when
        a seed page is blocked. Forwarded to ``fetch_seed_page``; if the
        handler returns True, the page is re-fetched once after the user has a
        chance to solve a CAPTCHA / sign-in in the live browser session.

    Returns
    -------
    FetchDocumentsStats with counts for every outcome type.
    """

    stats = FetchDocumentsStats(items_found=len(items))
    user_emit = emit or (lambda *a, **kw: None)

    # Wrap emit so inline-PDF events from fetch_seed_page (which doesn't have
    # access to stats) get tallied into FetchDocumentsStats automatically.
    def _emit(stage: str, status: str, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
        if status == "pdf_inline_ok":
            stats.inline_pdfs_attempted += 1
            stats.inline_pdfs_ok += 1
        elif status == "pdf_inline_unavailable":
            stats.inline_pdfs_attempted += 1
            stats.inline_pdfs_unavailable += 1
        elif status == "pdf_inline_failed":
            stats.inline_pdfs_attempted += 1
            stats.inline_pdfs_failed += 1
        elif status == "pdf_inline_throttled":
            stats.inline_pdfs_attempted += 1
            stats.inline_pdfs_throttled += 1
        elif status == "throttle_paused":
            stats.throttle_pauses += 1
        elif status == "throttle_resumed":
            stats.throttle_resumes += 1
        elif status == "throttle_exhausted":
            stats.throttle_exhausted += 1
        user_emit(stage, status, message, meta)

    # Pool state-change callback: surfaces pause / resume / exhaust events
    # through the same emit pipeline that the rest of the run uses. The CLI
    # wraps its emit to also ping Telegram on these statuses.
    def _on_pool_state_change(state: str, meta: Dict[str, Any]) -> None:
        if state == "throttle_paused":
            cd = meta.get("cooldown_sec", "?")
            tp = meta.get("total_pauses", "?")
            ct = meta.get("consecutive_throttles", "?")
            _emit("fetching", "throttle_paused",
                  f"PDF pool paused after {ct} consecutive timeouts — cooling down {cd}s (pause #{tp})",
                  meta)
        elif state == "throttle_resumed":
            cd = meta.get("cooldown_sec", "?")
            _emit("fetching", "throttle_resumed",
                  f"PDF pool resumed after {cd}s cooldown",
                  meta)
        elif state == "throttle_exhausted":
            tp = meta.get("total_pauses", "?")
            _emit("fetching", "throttle_exhausted",
                  f"PDF pool exhausted retry budget after {tp} pauses — remaining articles will skip",
                  meta)

    seeds     = [i for i in items if i.fetch_type == "seed"]
    pdfs      = [i for i in items if i.fetch_type == "pdf"]
    abstracts = [i for i in items if i.fetch_type == "abstract"]

    # ── Abstracts: no network required ────────────────────────────────────
    for item in abstracts:
        try:
            save_abstract(item)
            stats.abstracts_saved += 1
            _emit("fetching", "abstract_saved",
                  f"[{item.gap_id}] abstract saved: {item.title[:60]}",
                  {"gap_id": item.gap_id, "source_id": item.source_id})
        except Exception as exc:
            _emit("fetching", "warning",
                  f"[{item.gap_id}] abstract save failed: {exc!s:.80}",
                  {"gap_id": item.gap_id, "source_id": item.source_id})

    # Open ONE persistent browser tab for the whole batch so we don't
    # steal focus on every fetch. session() yields a session-shaped object
    # that exposes the same fetch / fetch_with_eval interface as
    # browser_client itself; for non-CDP providers it's a passthrough.
    with browser_client.session() as bc:

        # Determine if we can run a persistent worker pool for parallel
        # per-article PDF fetches. The pool reuses N persistent pages
        # across the whole run, eliminating per-task playwright setup
        # overhead. Only available when the session is CDP-backed.
        _pool_cdp_url: Optional[str] = None
        _client = getattr(bc, "_client", None)
        if _client is not None:
            _candidate = getattr(_client, "cdp_url", None)
            if isinstance(_candidate, str) and _candidate.startswith("http"):
                _pool_cdp_url = _candidate
        _pool_workers = max(1, int(os.environ.get("ORCH_PDF_WORKERS", "4")))

        with make_pdf_worker_pool(
            _pool_cdp_url,
            _pool_workers,
            on_state_change=_on_pool_state_change,
        ) as pdf_pool:

            # ── Seed pages: browser required ──────────────────────────────
            for i, item in enumerate(seeds, 1):
                tag = f"[{i}/{len(seeds)}] {item.gap_id}/{item.source_id}"
                _emit("fetching", "seed_start",
                      f"{tag}: fetching {item.url[:70]}",
                      {"gap_id": item.gap_id, "source_id": item.source_id,
                       "url": item.url[:80], "index": i, "total": len(seeds)})
                stats.seeds_attempted += 1
                try:
                    count = fetch_seed_page(
                        item, bc,
                        emit=_emit, on_blocked=on_blocked, pdf_pool=pdf_pool,
                    )
                    stats.seeds_ok += 1
                    stats.articles_extracted += count
                    _emit("fetching", "seed_ok",
                          f"{tag}: {count} article(s) saved",
                          {"gap_id": item.gap_id, "source_id": item.source_id, "count": count})
                except Exception as exc:
                    stats.seeds_failed += 1
                    _emit("fetching", "seed_failed",
                          f"{tag}: {exc!s:.80}",
                          {"gap_id": item.gap_id, "source_id": item.source_id, "error": str(exc)[:120]})

        # ── PDFs ──────────────────────────────────────────────────────────
        for i, item in enumerate(pdfs, 1):
            tag = f"[{i}/{len(pdfs)}] {item.gap_id}"
            _emit("fetching", "pdf_start",
                  f"{tag}: downloading {item.url[:70]}",
                  {"gap_id": item.gap_id, "source_id": item.source_id,
                   "url": item.url[:80], "index": i, "total": len(pdfs)})
            stats.pdfs_attempted += 1
            try:
                path = download_pdf(item, bc, emit=_emit)
                stats.pdfs_ok += 1
                _emit("fetching", "pdf_ok",
                      f"{tag}: saved {path.name}",
                      {"gap_id": item.gap_id, "source_id": item.source_id, "file": path.name})
            except Exception as exc:
                stats.pdfs_failed += 1
                _emit("fetching", "pdf_failed",
                      f"{tag}: {exc!s:.80}",
                      {"gap_id": item.gap_id, "source_id": item.source_id, "error": str(exc)[:120]})

    return stats


# ---------------------------------------------------------------------------
# Per-provider record writers
# ---------------------------------------------------------------------------


def _write_ebsco_records(records: Any, out_dir: Path) -> int:
    count = 0
    for rec in (records or [])[:MAX_ARTICLES]:
        if not isinstance(rec, dict) or not rec.get("title"):
            continue
        slug = _slugify(rec["title"])[:60]
        path = out_dir / f"{slug}.md"
        if path.exists():
            count += 1
            continue
        lines = [
            f"# {rec['title']}",
            "",
            f"**Authors:** {rec.get('authors') or '—'}  ",
            f"**Source:** {rec.get('source') or '—'}  ",
            f"**Date:** {rec.get('date') or '—'}  ",
        ]
        if rec.get("database"):
            lines.append(f"**Database:** {rec['database']}  ")
        if rec.get("url"):
            lines.append(f"**URL:** {rec['url']}  ")
        if rec.get("pdf_url"):
            lines.append(f"**PDF:** {rec['pdf_url']}  ")
        lines += ["", "## Abstract", "", rec.get("abstract") or "_(not available)_", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        count += 1
    return count


def _write_jstor_records(records: Any, out_dir: Path) -> int:
    count = 0
    for rec in (records or [])[:MAX_ARTICLES]:
        if not isinstance(rec, dict) or not rec.get("title"):
            continue
        slug = _slugify(rec["title"])[:60]
        path = out_dir / f"{slug}.md"
        if path.exists():
            count += 1
            continue
        lines = [
            f"# {rec['title']}",
            "",
            f"**Authors:** {rec.get('authors') or '—'}  ",
            f"**Published in:** {rec.get('source') or '—'}  ",
            f"**Date:** {rec.get('date') or '—'}  ",
        ]
        if rec.get("url"):
            lines.append(f"**URL:** {rec['url']}  ")
        lines += ["", "## Abstract", "", rec.get("abstract") or "_(not available)_", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        count += 1
    return count


def _write_muse_records(records: Any, out_dir: Path) -> int:
    count = 0
    for rec in (records or [])[:MAX_ARTICLES]:
        if not isinstance(rec, dict) or not rec.get("title"):
            continue
        slug = _slugify(rec["title"])[:60]
        path = out_dir / f"{slug}.md"
        if path.exists():
            count += 1
            continue
        lines = [
            f"# {rec['title']}",
            "",
            f"**Authors:** {rec.get('authors') or '—'}  ",
            f"**Published in:** {rec.get('source') or '—'}  ",
            f"**Date:** {rec.get('date') or '—'}  ",
        ]
        if rec.get("url"):
            lines.append(f"**URL:** {rec['url']}  ")
        lines += ["", "## Abstract", "", rec.get("abstract") or "_(not available)_", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        count += 1
    return count


# ---------------------------------------------------------------------------
# HTML save helper
# ---------------------------------------------------------------------------


def _save_html(content: bytes, out_dir: Path, suffix: str = "") -> None:
    """Save raw page HTML for manual review (skips if already present)."""
    target = out_dir / f"search_results{suffix}.html"
    if target.exists():
        return
    try:
        target.write_bytes(content)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "").strip()
    return re.sub(r"[\s_-]+", "_", text).strip("_") or "document"
