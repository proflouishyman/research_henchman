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

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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

    @property
    def total_ok(self) -> int:
        return self.abstracts_saved + self.seeds_ok + self.pdfs_ok


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
) -> int:
    """Navigate a seed search URL, extract article records, save as markdown.

    Returns the number of article files written.
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

    if js_expr:
        page_result, eval_result = browser_client.fetch_with_eval(
            item.url, js_expr, wait_ms=2500
        )
    else:
        page_result = browser_client.fetch(item.url)
        eval_result = None

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
        # Still save the HTML so users can inspect and retry manually
        _save_html(page_result.content, out_dir, suffix="_blocked")
        return 0

    # Save raw HTML for all providers (manual review backup)
    _save_html(page_result.content, out_dir)

    # Extract structured records
    if source_id in ("ebsco_api", "ebscohost"):
        return _write_ebsco_records(eval_result or [], out_dir)
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
# Top-level orchestrator
# ---------------------------------------------------------------------------


def run_fetch(
    items: List[FetchItem],
    browser_client: Any,
    *,
    emit: Optional[Callable] = None,
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

    Returns
    -------
    FetchDocumentsStats with counts for every outcome type.
    """

    stats = FetchDocumentsStats(items_found=len(items))
    _emit = emit or (lambda *a, **kw: None)

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

    # ── Seed pages: browser required ──────────────────────────────────────
    for i, item in enumerate(seeds, 1):
        tag = f"[{i}/{len(seeds)}] {item.gap_id}/{item.source_id}"
        _emit("fetching", "seed_start",
              f"{tag}: fetching {item.url[:70]}",
              {"gap_id": item.gap_id, "source_id": item.source_id,
               "url": item.url[:80], "index": i, "total": len(seeds)})
        stats.seeds_attempted += 1
        try:
            count = fetch_seed_page(item, browser_client, emit=_emit)
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

    # ── PDFs ──────────────────────────────────────────────────────────────
    for i, item in enumerate(pdfs, 1):
        tag = f"[{i}/{len(pdfs)}] {item.gap_id}"
        _emit("fetching", "pdf_start",
              f"{tag}: downloading {item.url[:70]}",
              {"gap_id": item.gap_id, "source_id": item.source_id,
               "url": item.url[:80], "index": i, "total": len(pdfs)})
        stats.pdfs_attempted += 1
        try:
            path = download_pdf(item, browser_client, emit=_emit)
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
