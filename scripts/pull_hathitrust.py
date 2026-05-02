#!/usr/bin/env python3
"""Pull HathiTrust full-text book coverage for manuscript gaps.

Targets HathiTrust's OCR-indexed full-text search endpoint
(``babel.hathitrust.org/cgi/ls``), which searches inside the books rather
than just metadata. The metadata-only catalog endpoint
(``catalog.hathitrust.org``) is too thin for this manuscript's topics
(4 hits for "Sears Roebuck mail order catalog" vs. 443,571 via full-text).

For each gap, generate a natural-language full-text query via the local
LLM, navigate the public HathiTrust UI (no auth required for search),
and persist matched records as JSON seed files in the existing
pull_output layout. The article indexer (``adapters/article_index.py``)
auto-discovers new ``<gap_id>/<source_id>/*.json`` files on next run.

Discovery date: 2026-05-02. DOM probed against
``https://babel.hathitrust.org/cgi/ls?q1=mail+order+catalog+Sears&field1=ocr&a=srchls``
which returned 100 ``article.record`` elements with stable selectors
``h3.record-title``, ``dl.metadata > div.grid > dt|dd``,
``a[data-clicktype="catalog"]``, ``a[data-clicktype="pt"]``.

Usage:
    python3 scripts/pull_hathitrust.py --run-id run_27f86e44394442
    python3 scripts/pull_hathitrust.py --run-id ... --limit 5 --dry-run
    python3 scripts/pull_hathitrust.py --run-id ... --gap-ids AUTO-181-G1

Requirements:
  - CDP-attached Chrome on :9222 (any session — auth not required).
  - Ollama running locally for the query-rewrite step.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env for ORCH_* / TELEGRAM_*
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, v = _line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from config import OrchestratorSettings  # noqa: E402
from layers.llm_client import make_llm_client  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# HathiTrust full-text OCR search. `pgs=100` requests 100 results per page
# (the live UI defaults to 25; the larger page is cleaner since we only
# scrape page 1). `field1=ocr` selects the inside-the-book search; the
# alternative ``all`` mixes catalog metadata and OCR which dilutes recall.
HATHITRUST_FULLTEXT_URL = (
    "https://babel.hathitrust.org/cgi/ls?"
    "q1={q}&field1=ocr&a=srchls&pgs=100&anchor=search"
)

# DOM extractor for the full-text results page. Parses ``article.record``
# elements rendered by HathiTrust's Solr-backed search UI. Selectors were
# discovered 2026-05-02 against a live page; layout is shared with the
# catalog metadata search at catalog.hathitrust.org.
HATHITRUST_EXTRACTOR_JS = """() => {
    const articles = document.querySelectorAll('article.record');
    const out = [];
    articles.forEach((art, idx) => {
        if (idx >= 100) return;  // page-size cap
        // Title — h3.record-title; sometimes wraps a span.title, take the
        // outer innerText so either form works.
        const titleEl = art.querySelector('h3.record-title');
        const title = titleEl ? (titleEl.innerText || '').trim() : '';

        // Metadata pairs live in dl.metadata > div.grid > <dt><dd>. The
        // dt label is the field name ("Published", "Author", "Subject",
        // etc.) and the dd is the value.
        const meta = {};
        art.querySelectorAll('dl.metadata div.grid').forEach(g => {
            const dt = g.querySelector('dt');
            const dd = g.querySelector('dd');
            if (dt && dd) {
                meta[(dt.innerText || '').trim()] = (dd.innerText || '').trim();
            }
        });

        // Two access links per record:
        //   data-clicktype="catalog" → /Record/<id> (always present)
        //   data-clicktype="pt"      → /cgi/pt?id=<id> (full text reader)
        // The "pt" link's text indicates access level: "Full view" is
        // public-domain-readable; "Limited (search-only)" / "Limited
        // (full text)" is copyright-restricted.
        const catA = art.querySelector('a[data-clicktype="catalog"]');
        const ptA  = art.querySelector('a[data-clicktype="pt"]');
        const catalog_url = catA ? catA.getAttribute('href') : '';
        const pt_url      = ptA  ? ptA.getAttribute('href')  : '';
        const access      = ptA  ? (ptA.innerText || '').trim().replace(/\\s+/g, ' ') : '';

        // HathiTrust ID — embedded in the cover div's data-hdl attribute,
        // e.g. "mdp.49015001020396". Useful as a stable identifier.
        const coverEl = art.querySelector('.cover[data-hdl]');
        const hathi_id = coverEl ? (coverEl.getAttribute('data-hdl') || '') : '';

        if (title) {
            out.push({
                title:       title.slice(0, 300),
                catalog_url: catalog_url || '',
                pt_url:      pt_url || '',
                hathi_id:    hathi_id,
                access:      access,
                published:   meta['Published'] || '',
                author:      meta['Author']    || '',
                subject:     meta['Subject']   || '',
                language:    meta['Language']  || '',
                publisher:   meta['Publisher'] || '',
            });
        }
    });
    return out;
}"""

# System prompt for HathiTrust query generation. HathiTrust uses Solr
# OCR full-text search — natural-language phrases work better than deep
# Boolean nesting. Quoted phrases for exact matches; AND/OR sparingly.
# Crucially: HathiTrust's collection skews pre-2000s monographs and
# government documents, so we steer the LLM toward period vocabulary
# and historical entities, not modern brand names.
HATHITRUST_QUERY_SYSTEM = """\
You are an expert in historical book-archive search syntax (HathiTrust
full-text OCR search). Your task is to generate ONE concise natural-
language query that matches a research gap to HathiTrust's collection.

Rules:
1. HathiTrust skews older — most full-view items are pre-1928, plus
   limited search-only access to copyrighted 20th-century books. Prefer
   PERIOD VOCABULARY: "mail order", "catalog house", "department store",
   "chain store", "direct marketing", "merchant" — not modern jargon
   like "e-commerce platform" or "omnichannel".
2. Use 2-4 key concept words/phrases. Quote multi-word phrases.
3. AND/OR are supported but use sparingly (deep Boolean degrades OCR
   matching). Prefer two quoted phrases joined by AND, or a single
   compound phrase.
4. Prefer historical/institutional entities: "Sears Roebuck",
   "Montgomery Ward", "Macy's", "Marshall Field" — over modern brands
   that don't appear in HathiTrust's older corpus.
5. Keep queries SHORT — under 120 characters.
6. Recall over precision — HathiTrust is dense, even diluted hits are
   useful for context.

Output: a SINGLE query, one line, no commentary, no numbering, no
markdown fences. Just the query.

Examples:

Research gap: "Mail-order catalogs democratized rural consumption in late-19th-century America."
Query: "mail order" "Sears Roebuck" rural

Research gap: "Department-store credit accounts predated modern consumer credit."
Query: "department store" "charge account" credit

Research gap: "Direct marketing techniques evolved from postal-era catalog houses."
Query: "direct marketing" catalog mail

Research gap: "Chinese e-commerce dominated by Alibaba and Tencent."
Query: China retail commerce trade
"""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class GapInfo:
    gap_id: str
    chapter: str
    claim_text: str


# ---------------------------------------------------------------------------
# Gap loading
# ---------------------------------------------------------------------------


def parse_gap_report(report_path: Path) -> List[Tuple[str, str, str]]:
    """Return [(gap_id, chapter, claim_text), ...] for every gap in the report.

    Mirrors the parser in ``pull_proquest_newspapers.py`` — kept as a
    local copy rather than imported because the ProQuest module's
    parser is private and the two scripts evolve independently.
    """
    text = report_path.read_text(encoding="utf-8")
    out: List[Tuple[str, str, str]] = []
    sections = re.split(r"^## Gap \d+:", text, flags=re.MULTILINE)[1:]
    for sec in sections:
        m_id = re.search(r"ID: `(AUTO-[\w-]+)`", sec)
        m_ch = re.search(r"Chapter: ([^\n]+)", sec)
        m_cl = re.search(r"```text\s*\n(.*?)\n```", sec, re.DOTALL)
        if m_id and m_cl:
            out.append((
                m_id.group(1),
                (m_ch.group(1).strip() if m_ch else ""),
                m_cl.group(1).strip(),
            ))
    return out


# ---------------------------------------------------------------------------
# Query generation
# ---------------------------------------------------------------------------


def generate_hathitrust_query(claim_text: str, llm_client: Any) -> str:
    """Use the local LLM to rewrite the gap claim into a HathiTrust-friendly
    natural-language query. Returns empty string if the LLM call fails."""
    user_msg = f"Research gap: \"{claim_text.strip()}\"\nQuery:"
    try:
        response = llm_client.complete(
            system=HATHITRUST_QUERY_SYSTEM,
            prompt=user_msg,
            temperature=0.2,
        )
    except Exception as exc:
        print(f"[warn] LLM error: {exc!s:.80}", flush=True)
        return ""
    for line in response.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        line = re.sub(r"^\s*(?:\d+[.):\s]+|Query[:\s]+)", "", line, flags=re.IGNORECASE).strip()
        if line:
            return line[:200]
    return ""


# ---------------------------------------------------------------------------
# HathiTrust search
# ---------------------------------------------------------------------------


def search_hathitrust(page: Any, query: str) -> List[Dict[str, Any]]:
    """Navigate HathiTrust's full-text search URL with *query* and extract
    the result records. Returns raw record dicts; empty list on error or
    legitimate zero-hit query."""
    url = HATHITRUST_FULLTEXT_URL.format(q=urllib.parse.quote_plus(query))
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        # HathiTrust's results render server-side; a brief wait is enough.
        page.wait_for_timeout(2500)
    except Exception as exc:
        print(f"[warn] nav timeout: {exc!s:.80}", flush=True)
        return []

    final_url = page.url.lower()
    page_title = (page.title() or "").lower()

    # Robots / rate-limit / system-overload pages — bail cleanly.
    if "are you a robot" in page_title or "captcha" in page_title:
        print("[skip] captcha or bot challenge — manual intervention needed", flush=True)
        return []
    if "service unavailable" in page_title or "503" in page_title:
        print("[skip] service unavailable", flush=True)
        return []

    # Empty-result detection — HathiTrust shows "Sorry, your search ..." or
    # similar text when zero hits. Cheap to check via title pattern; the
    # extractor will also return [] in that case.
    if "no results" in page_title or "no matches" in page_title:
        return []

    try:
        records = page.evaluate(HATHITRUST_EXTRACTOR_JS) or []
    except Exception as exc:
        print(f"[warn] extractor error: {exc!s:.80}", flush=True)
        return []
    return records


# ---------------------------------------------------------------------------
# Record persistence
# ---------------------------------------------------------------------------


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s or "").strip()
    return re.sub(r"[\s_-]+", "_", s).strip("_") or "query"


def write_records(
    records: List[Dict[str, Any]],
    *,
    gap_id: str,
    source_id: str,
    query: str,
    pull_root: Path,
) -> Path:
    """Write records as a JSON file in the existing pull_output schema.

    Output shape matches ``pull_proquest_newspapers.write_records`` so the
    article indexer's ``_ingest_seed_json`` walk picks them up without
    any indexer changes. URL field prefers ``pt_url`` (the full-text
    reader) when available since that's the actionable link; otherwise
    falls back to the catalog-record URL.
    """
    out_dir = pull_root / gap_id / source_id
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{_slugify(query)[:60]}.json"
    out_path = out_dir / fname
    rows = []
    for rec in records:
        # Prefer the full-text reader URL since it's where readable
        # content lives; fall back to the catalog record. Both are
        # absolute or absolute-path URLs from HathiTrust's own host.
        pt = rec.get("pt_url", "")
        if pt and pt.startswith("/"):
            pt = "https://babel.hathitrust.org" + pt
        cat = rec.get("catalog_url", "")
        if cat and cat.startswith("/"):
            cat = "https://catalog.hathitrust.org" + cat
        url = pt or cat
        rows.append({
            "title":         rec.get("title", "")[:300],
            "url":           url,
            "pdf_url":       "",  # full-book PDF needs page-by-page scrape; out of scope
            "abstract":      "",  # HathiTrust search results don't expose snippets reliably
            "authors":       rec.get("author", ""),
            "journal":       rec.get("publisher", ""),  # closest analog for books
            "pub_date":      rec.get("published", ""),
            "doi":           "",
            "query":         query,
            "gap_id":        gap_id,
            "quality_label": "seed",
            "quality_rank":  "20",
            "source":        f"{source_id}_hathitrust_html",
            "link_type":     "book_record",
            # HathiTrust-specific extras (preserved alongside canonical
            # fields; the indexer ignores unknown keys).
            "hathi_id":      rec.get("hathi_id", ""),
            "access":        rec.get("access", ""),
            "subject":       rec.get("subject", ""),
            "language":      rec.get("language", ""),
        })
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="Pull HathiTrust full-text book coverage for manuscript gaps.",
    )
    p.add_argument("--run-id", required=True,
                   help="Run ID (e.g. run_27f86e44394442) — must have a matching gap_report file.")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N gaps (for testing).")
    p.add_argument("--gap-ids", default=None,
                   help="Comma-separated list of specific gap IDs to process. "
                        "Useful for retrying specific gaps after a partial run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print queries; do not write JSON.")
    p.add_argument("--model", default="llama3.1:8b",
                   help="LLM model for query rewrites. Default llama3.1:8b — "
                        "follows the terse prompt format. gpt-oss:20b tends "
                        "to ask clarifying questions on this prompt.")
    p.add_argument("--data-root", default=None,
                   help="Override ORCH_DATA_ROOT (default: <repo>/data).")
    p.add_argument("--full-view-only", action="store_true",
                   help="Filter out records where access != 'Full view'. "
                        "Default keeps both Full view and Limited records — "
                        "limited entries still surface citation metadata "
                        "useful for follow-up via interlibrary loan.")
    args = p.parse_args()

    settings = OrchestratorSettings.from_env()
    data_root = Path(args.data_root) if args.data_root else (
        settings.data_root if hasattr(settings, "data_root") else PROJECT_ROOT / "data"
    )
    pull_root = Path(data_root) / "pull_outputs" / args.run_id
    if not pull_root.exists():
        sys.exit(f"pull_root not found: {pull_root}")

    # Locate the gap report
    export_root = Path(data_root) / "manuscript_exports"
    candidates = list(export_root.glob(f"*/gap_report_{args.run_id}.md"))
    if not candidates:
        sys.exit(f"no gap_report file for {args.run_id} under {export_root}")
    gap_report = candidates[0]
    print(f"gap report: {gap_report}", flush=True)

    gaps_all = parse_gap_report(gap_report)
    print(f"total gaps in report: {len(gaps_all)}", flush=True)

    # Filter — HathiTrust covers the full manuscript well, no India/China
    # narrowing. --gap-ids overrides for targeted retries.
    if args.gap_ids:
        wanted = {g.strip() for g in args.gap_ids.split(",") if g.strip()}
        gaps: List[GapInfo] = [
            GapInfo(gid, ch, cl) for (gid, ch, cl) in gaps_all if gid in wanted
        ]
        print(f"explicit gap-ids: {len(gaps)} matched out of {len(wanted)} requested", flush=True)
    else:
        gaps = [GapInfo(gid, ch, cl) for (gid, ch, cl) in gaps_all]

    if args.limit:
        gaps = gaps[:args.limit]
        print(f"limited to first {args.limit} for this run", flush=True)

    llm = make_llm_client(settings, model=args.model, timeout_seconds=120, temperature=0.2)
    source_id = "hathitrust_fulltext"

    if args.dry_run:
        page = None
    else:
        from playwright.sync_api import sync_playwright  # type: ignore
        from adapters.cdp_utils import effective_cdp_url
        cdp_url = getattr(settings, "playwright_cdp_url", "http://127.0.0.1:9222")
        pw_ctx = sync_playwright().__enter__()
        browser = pw_ctx.chromium.connect_over_cdp(effective_cdp_url(cdp_url))
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        # Restore user's tab so we don't hold focus
        try:
            others = [p for p in ctx.pages if p is not page]
            if others:
                others[0].bring_to_front()
        except Exception:
            pass

    consecutive_zero = 0
    total_records = 0
    gaps_with_results = 0
    i = 0  # ensures summary prints sensibly when gaps is empty

    try:
        for i, gap in enumerate(gaps, 1):
            tag = f"[{i}/{len(gaps)}] {gap.gap_id}"
            print(f"\n{tag}", flush=True)
            print(f"  claim: {gap.claim_text[:120]}", flush=True)

            query = generate_hathitrust_query(gap.claim_text, llm)
            if not query:
                print(f"  [skip] no query generated", flush=True)
                continue
            print(f"  query: {query}", flush=True)

            if args.dry_run:
                continue

            # Jitter to be polite — HathiTrust is non-profit-hosted.
            time.sleep(random.uniform(1.5, 3.5))
            records = search_hathitrust(page, query)

            if args.full_view_only:
                records = [r for r in records if r.get("access", "").lower().startswith("full view")]

            print(f"  → {len(records)} records", flush=True)
            if not records:
                consecutive_zero += 1
                if consecutive_zero >= 10:
                    print("[abort] 10 consecutive empty results — likely "
                          "rate-limit / bot challenge / selector breakage. Stopping.", flush=True)
                    break
                continue
            consecutive_zero = 0
            total_records += len(records)
            gaps_with_results += 1

            out_path = write_records(records, gap_id=gap.gap_id,
                                      source_id=source_id, query=query,
                                      pull_root=pull_root)
            print(f"  saved → {out_path.relative_to(PROJECT_ROOT)}", flush=True)

    finally:
        if not args.dry_run and page:
            try: page.close()
            except: pass
            try: pw_ctx.__exit__(None, None, None)
            except: pass

    print(f"\n=== summary ===", flush=True)
    print(f"  gaps processed:    {i}", flush=True)
    print(f"  gaps with results: {gaps_with_results}", flush=True)
    print(f"  total records:     {total_records}", flush=True)


if __name__ == "__main__":
    main()
