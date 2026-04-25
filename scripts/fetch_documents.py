#!/usr/bin/env python3
"""Interactive CLI: fetch full document content for a completed pipeline run.

For each gap the pipeline analyzed, this tool:
  1. Checks which sources returned seed-only results (search URLs, no full text)
  2. Prompts you to log in to library databases via the CDP browser
  3. Navigates each search URL and extracts article records
  4. Downloads available PDFs; saves abstracts and HTML where PDFs aren't accessible
  5. Writes everything to the existing pull-output folder so the export bundle picks it up

Usage:
    python scripts/fetch_documents.py
    python scripts/fetch_documents.py --run-id run_27f86e44394442
    python scripts/fetch_documents.py --gap-id AUTO-06-G1
    python scripts/fetch_documents.py --limit 20 --dry-run
    python scripts/fetch_documents.py --cdp-url http://localhost:9222

Requirements:
  - Chrome launched with --remote-debugging-port=9222 (or whatever --cdp-url points to)
  - playwright installed  (pip install playwright && playwright install chromium)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Bootstrap: add project root to path
# ---------------------------------------------------------------------------

SCRIPT_DIR  = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env before importing project modules
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, v = _line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from config import OrchestratorSettings  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FETCH_TIMEOUT   = 30        # seconds per page
MAX_ARTICLES    = 5         # max articles to fetch per search-results page
FETCHED_SUBDIR  = "fetched" # written inside the source pull dir

LOGIN_URLS = {
    "ebsco_api":   "https://search.ebscohost.com/login.aspx",
    "ebscohost":   "https://search.ebscohost.com/login.aspx",
    "jstor":       "https://www.jstor.org/",
    "project_muse":"https://muse.jhu.edu/",
    "proquest_historical_newspapers": "https://www.proquest.com/",
    "gale_primary_sources": "https://link.gale.com/",
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    settings = OrchestratorSettings.from_env()

    if args.cdp_url:
        os.environ["ORCH_PLAYWRIGHT_CDP_URL"] = args.cdp_url

    cdp_url = args.cdp_url or os.environ.get("ORCH_PLAYWRIGHT_CDP_URL", "http://localhost:9222")

    # ── 1. Resolve run ────────────────────────────────────────────────────
    run_id, pull_root = _resolve_run(args.run_id, settings)
    print(f"\n{'='*60}")
    print(f"  Run:  {run_id}")
    print(f"  Pull: {pull_root}")
    print(f"{'='*60}\n")

    # ── 2. Collect fetchable items ────────────────────────────────────────
    items = _collect_items(pull_root, gap_filter=args.gap_id, limit=args.limit)
    if not items:
        print("No fetchable items found in pull outputs.")
        return

    seeds  = [i for i in items if i["fetch_type"] == "seed"]
    pdfs   = [i for i in items if i["fetch_type"] == "pdf"]
    abstr  = [i for i in items if i["fetch_type"] == "abstract"]

    print(f"Found {len(items)} items across {len({i['gap_id'] for i in items})} gaps:")
    print(f"  Seed URLs (need login): {len(seeds)}")
    print(f"  PDF downloads:          {len(pdfs)}")
    print(f"  Abstracts (no PDF):     {len(abstr)}\n")

    if args.dry_run:
        print("[DRY RUN] — no files will be written.")
        for item in items[:20]:
            print(f"  [{item['fetch_type']:8}] {item['gap_id']:15} {item['source_id']:12} {item['url'][:70]}")
        return

    # ── 3. Save abstracts immediately (no network needed) ─────────────────
    if abstr:
        print(f"Saving {len(abstr)} abstracts...")
        for item in abstr:
            _save_abstract(item)
        print()

    # ── 4. Check CDP + login prompt ───────────────────────────────────────
    if seeds or pdfs:
        cdp_ok = _ping_cdp(cdp_url)
        if not cdp_ok:
            print("┌─ Chrome not reachable ─────────────────────────────────────┐")
            print("│ Start Chrome with remote debugging before continuing:        │")
            print("│                                                              │")
            print("│   /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\")
            print(f"│     --remote-debugging-port={_port_from_url(cdp_url)}                         │")
            print("│                                                              │")
            print("└──────────────────────────────────────────────────────────────┘")
            input("\nPress Enter once Chrome is running... ")
            cdp_ok = _ping_cdp(cdp_url)
            if not cdp_ok:
                print("Still can't reach Chrome. Exiting.")
                sys.exit(1)

        # Determine which sources need login
        needed_sources = {i["source_id"] for i in seeds}
        login_targets  = {s: u for s, u in LOGIN_URLS.items() if s in needed_sources}

        if login_targets:
            print("\n┌─ Login required ───────────────────────────────────────────┐")
            print("│ Opening sign-in pages in Chrome. Log in to each:            │")
            for src, url in login_targets.items():
                print(f"│   {src:20}  {url}  │")
            print("└──────────────────────────────────────────────────────────────┘\n")
            _open_tabs(cdp_url, list(login_targets.values()))
            input("Press Enter once you are logged in to all databases... ")

    # ── 5. Fetch seed pages via CDP ────────────────────────────────────────
    if seeds:
        print(f"\nFetching {len(seeds)} search-results pages via CDP...")
        n_ok, n_fail = 0, 0
        for i, item in enumerate(seeds, 1):
            tag = f"[{i}/{len(seeds)}] {item['gap_id']} / {item['source_id']}"
            print(f"  {tag}  ", end="", flush=True)
            try:
                count = _fetch_seed(item, cdp_url)
                print(f"→ {count} article(s) saved")
                n_ok += 1
            except Exception as exc:
                print(f"✗ {exc!s:.70}")
                n_fail += 1
        print(f"\nSeed fetch: {n_ok} ok, {n_fail} failed\n")

    # ── 6. Download PDFs ───────────────────────────────────────────────────
    if pdfs:
        print(f"Downloading {len(pdfs)} PDFs...")
        n_ok, n_fail = 0, 0
        for i, item in enumerate(pdfs, 1):
            tag = f"[{i}/{len(pdfs)}] {item['gap_id']}"
            print(f"  {tag}  ", end="", flush=True)
            try:
                path = _download_pdf(item, cdp_url)
                print(f"→ {path.name}")
                n_ok += 1
            except Exception as exc:
                print(f"✗ {exc!s:.70}")
                n_fail += 1
        print(f"\nPDF downloads: {n_ok} ok, {n_fail} failed\n")

    # ── 7. Summary ─────────────────────────────────────────────────────────
    total_fetched = sum(
        len(list((Path(i["out_dir"]) / FETCHED_SUBDIR).glob("*")))
        for i in items
        if (Path(i["out_dir"]) / FETCHED_SUBDIR).exists()
    )
    print(f"Done.  {total_fetched} files written under {pull_root}\n")


# ---------------------------------------------------------------------------
# Run resolution
# ---------------------------------------------------------------------------

def _resolve_run(run_id_arg: Optional[str], settings: OrchestratorSettings) -> Tuple[str, Path]:
    pull_root_base = settings.data_root / "pull_outputs"
    if run_id_arg:
        p = pull_root_base / run_id_arg
        if not p.exists():
            print(f"Run directory not found: {p}")
            sys.exit(1)
        return run_id_arg, p

    # Try API first
    try:
        resp = urllib.request.urlopen("http://localhost:8876/api/orchestrator/runs", timeout=5)
        data = json.loads(resp.read())
        runs = [r for r in data.get("runs", []) if r.get("status") in ("complete", "partial")]
        runs.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
        if runs:
            rid = runs[0]["run_id"]
            return rid, pull_root_base / rid
    except Exception:
        pass

    # Fall back to most-recently-modified directory
    dirs = sorted(pull_root_base.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
    dirs = [d for d in dirs if d.is_dir()]
    if not dirs:
        print(f"No run directories found under {pull_root_base}")
        sys.exit(1)
    return dirs[0].name, dirs[0]


# ---------------------------------------------------------------------------
# Collect fetchable items from pull_output directories
# ---------------------------------------------------------------------------

def _collect_items(
    pull_root: Path,
    gap_filter: Optional[str],
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    gap_dirs = sorted(pull_root.iterdir()) if pull_root.exists() else []
    for gap_dir in gap_dirs:
        if not gap_dir.is_dir():
            continue
        gap_id = gap_dir.name
        if gap_filter and gap_id != gap_filter:
            continue

        for src_dir in sorted(gap_dir.iterdir()):
            if not src_dir.is_dir():
                continue
            source_id = src_dir.name

            for json_file in sorted(src_dir.glob("*.json")):
                try:
                    payload = json.loads(json_file.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    continue
                records = payload if isinstance(payload, list) else [payload]

                for rec in records:
                    if not isinstance(rec, dict):
                        continue
                    item = _classify_record(rec, gap_id, source_id, src_dir)
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
) -> Optional[Dict[str, Any]]:
    """Return a fetch descriptor if the record has actionable content."""
    ql = str(rec.get("quality_label", "")).lower()
    url = str(rec.get("url", "") or rec.get("pdf_url", "")).strip()
    pdf_url = str(rec.get("pdf_url", "")).strip()
    abstract = str(rec.get("abstract", "")).strip()
    title = str(rec.get("title", "")).strip()

    base = {
        "gap_id": gap_id,
        "source_id": source_id,
        "out_dir": str(out_dir),
        "title": title,
        "url": url,
    }

    if pdf_url:
        return {**base, "fetch_type": "pdf", "url": pdf_url}
    if ql == "seed" and url.startswith("http"):
        return {**base, "fetch_type": "seed"}
    if abstract and ql in ("medium", "high") and not pdf_url:
        return {**base, "fetch_type": "abstract",
                "abstract": abstract,
                "authors": str(rec.get("authors", "")),
                "journal": str(rec.get("journal", "")),
                "pub_date": str(rec.get("pub_date", "")),
                "doi": str(rec.get("doi", ""))}
    return None


# ---------------------------------------------------------------------------
# Abstract saver (no network)
# ---------------------------------------------------------------------------

def _save_abstract(item: Dict[str, Any]) -> None:
    out = Path(item["out_dir"]) / FETCHED_SUBDIR
    out.mkdir(parents=True, exist_ok=True)
    slug = _slugify(item["title"] or item["url"])[:60]
    path = out / f"{slug}.md"
    if path.exists():
        return
    lines = [
        f"# {item['title'] or '(untitled)'}",
        "",
        f"**Authors:** {item.get('authors') or '—'}  ",
        f"**Journal:** {item.get('journal') or '—'}  ",
        f"**Date:** {item.get('pub_date') or '—'}  ",
        f"**DOI:** {item.get('doi') or '—'}  ",
        "",
        "## Abstract",
        "",
        item.get("abstract", "_(no abstract)_"),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Seed fetch (EBSCOhost / JSTOR / ProQuest search results via CDP)
# ---------------------------------------------------------------------------

def _fetch_seed(item: Dict[str, Any], cdp_url: str) -> int:
    """Navigate seed URL via CDP, extract article results, save as markdown + HTML."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:
        raise RuntimeError("playwright not installed — run: pip install playwright && playwright install chromium") from exc

    from adapters.cdp_utils import effective_cdp_url  # type: ignore
    effective = effective_cdp_url(cdp_url)

    out_dir = Path(item["out_dir"]) / FETCHED_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)

    source_id  = item["source_id"]
    url        = item["url"]
    count      = 0

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(effective)
        ctx     = browser.contexts[0] if browser.contexts else browser.new_context()
        page    = ctx.new_page()
        try:
            page.goto(url, timeout=FETCH_TIMEOUT * 1000, wait_until="domcontentloaded")
            time.sleep(2)  # let JS render

            if source_id in ("ebsco_api", "ebscohost"):
                count = _extract_ebsco_page(page, out_dir, item)
            elif source_id == "jstor":
                count = _extract_jstor_page(page, out_dir, item)
            elif source_id in ("project_muse", "proquest_historical_newspapers", "gale_primary_sources"):
                count = _extract_generic_page(page, out_dir, item)
            else:
                count = _extract_generic_page(page, out_dir, item)
        finally:
            page.close()

    return count


def _extract_ebsco_page(page: Any, out_dir: Path, item: Dict[str, Any]) -> int:
    """Extract article results from an EBSCOhost search-results page."""
    try:
        records = page.evaluate("""() => {
            const results = [];
            const containers = document.querySelectorAll(
                '.result-list-item, article.record, [data-auto="record"], li.results-list-item'
            );
            containers.forEach((el, idx) => {
                if (idx >= 8) return;
                const getText = sel => { const n = el.querySelector(sel); return n ? n.innerText.trim() : ''; };
                const getAttr = (sel, attr) => { const n = el.querySelector(sel); return n ? (n.getAttribute(attr)||'').trim() : ''; };
                const title    = getText('.title-link') || getText('[data-auto="result-item-title"]') || getText('h3.title') || getText('a.record__title') || '';
                const authors  = getText('.authors-list') || getText('[data-auto="result-item-authors"]') || '';
                const source   = getText('.source-content') || getText('[data-auto="result-item-source"]') || '';
                const date     = getText('.date-content') || getText('[data-auto="result-item-date"]') || '';
                const abstract = getText('.abstract-value') || getText('.record__abstract') || getText('.abstract-text') || '';
                const pdfLink  = getAttr('a[href*="pdfviewer"], a.pdf-link, [data-auto="pdf-link"]', 'href');
                if (title) results.push({title, authors, source, date, abstract, pdf_url: pdfLink});
            });
            return results;
        }""")
    except Exception:
        records = []

    count = 0
    for rec in (records or [])[:MAX_ARTICLES]:
        if not rec.get("title"):
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
        if rec.get("pdf_url"):
            lines += [f"**PDF:** {rec['pdf_url']}  "]
        lines += ["", "## Abstract", "", rec.get("abstract") or "_(not available)_", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        count += 1

    # Also save the full search-results HTML for manual review
    if not (out_dir / "search_results.html").exists():
        try:
            html = page.content()
            (out_dir / "search_results.html").write_text(html, encoding="utf-8", errors="ignore")
        except Exception:
            pass

    return count


def _extract_jstor_page(page: Any, out_dir: Path, item: Dict[str, Any]) -> int:
    """Extract article results from a JSTOR search page."""
    try:
        records = page.evaluate("""() => {
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
        }""")
    except Exception:
        records = []

    count = 0
    for rec in (records or [])[:MAX_ARTICLES]:
        if not rec.get("title"):
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
            lines += [f"**URL:** {rec['url']}  "]
        lines += ["", "## Abstract", "", rec.get("abstract") or "_(not available)_", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        count += 1

    _save_page_html(page, out_dir)
    return count


def _extract_generic_page(page: Any, out_dir: Path, item: Dict[str, Any]) -> int:
    """Generic fallback: save the full HTML of the search-results page."""
    _save_page_html(page, out_dir)
    return 1


def _save_page_html(page: Any, out_dir: Path) -> None:
    target = out_dir / "search_results.html"
    if target.exists():
        return
    try:
        html = page.content()
        target.write_text(html, encoding="utf-8", errors="ignore")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PDF downloader
# ---------------------------------------------------------------------------

def _download_pdf(item: Dict[str, Any], cdp_url: str) -> Path:
    out_dir = Path(item["out_dir"]) / FETCHED_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    slug    = _slugify(item["title"] or item["url"])[:60]
    path    = out_dir / f"{slug}.pdf"
    if path.exists():
        return path

    # Try direct HTTP first
    try:
        req = urllib.request.Request(
            item["url"],
            headers={"User-Agent": "Mozilla/5.0 (research tool)"},
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            content = resp.read(20_000_000)  # 20 MB cap
        if b"%PDF" in content[:8]:
            path.write_bytes(content)
            return path
    except Exception:
        pass

    # Fallback: fetch via CDP (authenticated session)
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        from adapters.cdp_utils import effective_cdp_url  # type: ignore
        effective = effective_cdp_url(cdp_url)
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(effective)
            ctx     = browser.contexts[0] if browser.contexts else browser.new_context()
            resp    = ctx.request.get(item["url"], timeout=FETCH_TIMEOUT * 1000)
            content = resp.body()
        if content:
            path.write_bytes(content)
            return path
    except Exception:
        pass

    raise RuntimeError(f"Could not fetch PDF: {item['url'][:80]}")


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

def _ping_cdp(cdp_url: str) -> bool:
    try:
        req = urllib.request.Request(
            f"{cdp_url.rstrip('/')}/json/version",
            headers={"Host": urllib.parse.urlparse(cdp_url).netloc},
        )
        urllib.request.urlopen(req, timeout=4)
        return True
    except Exception:
        return False


def _open_tabs(cdp_url: str, urls: List[str]) -> None:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        from adapters.cdp_utils import effective_cdp_url  # type: ignore
        effective = effective_cdp_url(cdp_url)
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(effective)
            ctx     = browser.contexts[0] if browser.contexts else browser.new_context()
            for url in urls:
                try:
                    page = ctx.new_page()
                    page.goto(url, timeout=15_000)
                except Exception:
                    pass
    except Exception as exc:
        print(f"  (could not open tabs automatically: {exc!s:.60})")
        print("  Please open these URLs manually:")
        for url in urls:
            print(f"    {url}")


def _port_from_url(cdp_url: str) -> str:
    try:
        return urllib.parse.urlparse(cdp_url).port or 9222
    except Exception:
        return 9222


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "").strip()
    return re.sub(r"[\s_-]+", "_", text).strip("_") or "document"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch full document content for a completed pipeline run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--run-id",  help="Run ID to fetch documents for (default: most recent)")
    p.add_argument("--gap-id",  help="Fetch documents for a single gap only")
    p.add_argument("--limit",   type=int, help="Max number of items to fetch")
    p.add_argument("--cdp-url", default=None, help="CDP endpoint (default: from .env)")
    p.add_argument("--dry-run", action="store_true", help="Print what would be fetched; write nothing")
    return p.parse_args()


if __name__ == "__main__":
    main()
