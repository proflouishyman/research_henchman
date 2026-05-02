#!/usr/bin/env python3
"""Pull ProQuest International Newsstream coverage for India/China gaps.

For each gap in the manuscript's gap report whose claim text references
India / China (or related entities like Mumbai, Shanghai, Flipkart,
Alibaba, etc.), generate a newspaper-flavored Boolean query via the local
LLM and run it against ProQuest International Newsstream through JHU's
EZproxy. Save matched articles as seed records in the existing
pull_output directory layout so the standard fetch pipeline can pick them
up later.

NOT integrated into the upstream gap-analysis pipeline — this is a
focused enrichment pass per the 2026-05-02 roadmap (option B from the
Opus consultation: probe and adapt before refactoring).

Usage:
    python3 scripts/pull_proquest_newspapers.py --run-id run_27f86e44394442
    python3 scripts/pull_proquest_newspapers.py --run-id ... --limit 5 --dry-run

Requirements:
  - CDP-attached Chrome on :9222 with a JHU-Libraries-authenticated session.
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

# JHU EZproxy URLs for ProQuest collections (discovered 2026-05-02 by walking
# https://databases.library.jhu.edu/az/databases?q=proquest).
JHU_EZPROXY_PROQUEST = {
    "international_newsstream": "https://databases.library.jhu.edu/databases/proxy/JHU07220",
    "us_newsstream":            "https://databases.library.jhu.edu/databases/proxy/JHU06250",
    "historical_newspapers":    "https://databases.library.jhu.edu/databases/proxy/JHU05070",
    "chinese_newspapers":       "https://databases.library.jhu.edu/databases/proxy/JHU06997",
}

# Keyword filters — gaps whose claim text mentions any of these are
# considered India/China-relevant. Cheap pre-filter; avoids running an
# LLM classification on every gap.
INDIA_KEYWORDS = {
    "india", "indian", "delhi", "mumbai", "bangalore", "bengaluru",
    "kolkata", "calcutta", "chennai", "hyderabad",
    "flipkart", "snapdeal", "myntra", "paytm", "ola", "swiggy", "zomato",
    "infosys", "wipro", "tcs",
}
CHINA_KEYWORDS = {
    "china", "chinese", "beijing", "shanghai", "shenzhen", "guangzhou",
    "hong kong", "taiwan", "taipei",
    "alibaba", "taobao", "wechat", "tencent", "baidu", "jd.com", "jd ",
    "didi", "xiaomi", "huawei", "byd",
    "jack ma", "tianmao", "11.11", "singles day", "singles' day",
}

# ProQuest's `.resultItem` field selectors discovered 2026-05-02 by walking
# the live DOM of an authenticated International Newsstream search results page.
PROQUEST_EXTRACTOR_JS = """() => {
    const items = document.querySelectorAll('li.resultItem');
    const out = [];
    items.forEach((item, idx) => {
        if (idx >= 50) return;  // cap per Opus advice — don't try to download the full result set
        // Title link — there are TWO `a.previewTitle` per result: the
        // first is a thumbnail anchor with empty text, the second is the
        // actual title link (id="citationDocTitleLink_*"). Match the
        // ID-prefix variant first so we don't pick the thumbnail.
        const a = item.querySelector('a[id^="citationDocTitleLink_"]')
                  || item.querySelector('a.previewTitle:not(.citationSourceTypeIconLink)');
        // Title text — preferred: `.truncatedResultsTitle` (full untruncated text);
        // fall back to the title link's innerText.
        const titleEl = item.querySelector('.truncatedResultsTitle');
        const title = titleEl ? (titleEl.innerText||'').trim()
                              : (a ? (a.innerText||'').trim() : '');
        const detail_url = a ? a.getAttribute('href') : '';
        // Source-type label (e.g. "Newspaper", "Trade Journal", "Magazine")
        const stb = item.querySelector('.source-type-label');
        const source_type = stb ? (stb.innerText||'').trim() : '';
        // Authors — `.scholUnivAuthors` gives "Nott, George; White, Kevin."
        const authorEl = item.querySelector('.scholUnivAuthors');
        const authors = authorEl ? (authorEl.innerText||'').trim() : '';
        // Publication info — `.jnlArticle` is the canonical citation block:
        //   "Grocer; Crawley (Mar 21, 2026): 30,31,32,33,..."
        // Carries newspaper name, city, date, page numbers in one string.
        const pubEl = item.querySelector('.jnlArticle');
        const pub_info = pubEl ? (pubEl.innerText||'').trim() : '';
        // Fulltext link if present — under `.contentItemLinks`
        let fulltext_url = '';
        const ftEl = item.querySelector('.contentItemLinks a[href*="fulltext"]');
        if (ftEl) fulltext_url = ftEl.getAttribute('href') || '';
        // Abstract / preview text — usually only visible after a "Quick look"
        // expansion; best-effort, often empty on the search results page.
        const ab = item.querySelector('.previewText, [class*="abstract"]');
        const abstract = ab ? (ab.innerText||'').trim().slice(0, 1500) : '';

        if (title) {
            out.push({
                title: title.slice(0, 300),
                detail_url: detail_url || '',
                fulltext_url: fulltext_url || '',
                source_type: source_type,
                authors: authors,
                pub_info: pub_info,
                abstract: abstract,
            });
        }
    });
    return out;
}"""

# System prompt for newspaper-flavored query generation. Uses period
# vocabulary, named entities, localizers — distinct from the academic
# search prompt in normalize_seed_queries.py.
NEWSPAPER_QUERY_SYSTEM = """\
You are an expert in newspaper-archive search syntax (ProQuest International
Newsstream). Your task is to generate ONE concise Boolean search query to
find historical newspaper coverage of a specific research gap.

Rules:
1. Prefer PERIOD VOCABULARY of newspapers in the relevant era — "online shopping",
   "Internet shopping", "World Wide Web", "Web site" (1990s/2000s spell it
   as two words), "dot-com". Newspapers don't use academic jargon.
2. Prefer NAMED ENTITIES over abstractions: "Jeff Bezos" not "platform founder",
   "Flipkart" not "Indian e-commerce platform".
3. LOCALIZE for the target region (India: include Indian / Mumbai / Bangalore;
   China: Chinese / Shanghai / Hong Kong).
4. Quote multi-word phrases with double-quotes.
5. Use truncation operators (retail*) instead of long synonym lists.
6. Keep the query SHORT — ProQuest's newspaper search degrades on deep nesting.
   Two or three concept groups joined by AND. Maximum 200 chars.
7. Prefer recall over precision — we want results, not zero hits.

Output: a SINGLE Boolean query, one line, no commentary, no numbering, no
markdown fences. Just the query.

Examples:

Research gap: "China became the world's largest retail market in 2019, driven by e-commerce."
Region: China
Query: ("China" OR "Chinese") AND ("e-commerce" OR "online shopping" OR "Alibaba") AND (retail OR market OR consumer)

Research gap: "Indian e-commerce dominated by Flipkart, Amazon, Snapdeal."
Region: India
Query: ("Flipkart" OR "Snapdeal" OR "Amazon India") AND ("e-commerce" OR "online retail" OR "online shopping")

Research gap: "Singles' Day shopping festival in China became the world's largest sales event."
Region: China
Query: ("Singles' Day" OR "Singles Day" OR "11.11") AND ("Alibaba" OR "Tmall" OR "China") AND (sales OR shopping)
"""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class GapInfo:
    gap_id: str
    chapter: str
    claim_text: str
    region: str  # "india" | "china" | "both"


# ---------------------------------------------------------------------------
# Gap loading + classification
# ---------------------------------------------------------------------------


_GAP_HEADER_RE = re.compile(
    r"^## Gap \d+:.*?\n.*?ID: `(?P<gap_id>AUTO-[\w-]+)`\s+Chapter: (?P<chapter>.+?)\n",
    re.MULTILINE | re.DOTALL,
)


def parse_gap_report(report_path: Path) -> List[Tuple[str, str, str]]:
    """Return [(gap_id, chapter, claim_text), ...] for every gap in the report.

    Claims live in the ```text fenced block immediately after each gap header.
    """
    text = report_path.read_text(encoding="utf-8")
    out: List[Tuple[str, str, str]] = []
    # Split on gap-section boundary
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


def classify_region(claim_text: str) -> Optional[str]:
    """Return 'india' | 'china' | 'both' | None.

    Pure keyword check — fast, no LLM needed. False positives are tolerable
    (we'd just run a query that returns 0 results); false negatives mean
    a relevant gap gets skipped (acceptable tradeoff vs. running an LLM
    on 185 gaps).
    """
    lower = claim_text.lower()
    has_india = any(kw in lower for kw in INDIA_KEYWORDS)
    has_china = any(kw in lower for kw in CHINA_KEYWORDS)
    if has_india and has_china:
        return "both"
    if has_india:
        return "india"
    if has_china:
        return "china"
    return None


# ---------------------------------------------------------------------------
# Newspaper query generation
# ---------------------------------------------------------------------------


def generate_newspaper_query(claim_text: str, region: str, llm_client: Any) -> str:
    """Use the local LLM to rewrite the gap claim into a newspaper-flavored
    Boolean query targeted at the given region. Returns a single query
    string — empty string if the LLM call fails (caller should skip)."""
    user_msg = f"Research gap: \"{claim_text.strip()}\"\nRegion: {region}\nQuery:"
    try:
        response = llm_client.complete(
            system=NEWSPAPER_QUERY_SYSTEM,
            prompt=user_msg,
            temperature=0.2,
        )
    except Exception as exc:
        print(f"[warn] LLM error: {exc!s:.80}", flush=True)
        return ""
    # Take the first non-empty line as the query.
    for line in response.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        # Strip leading numbering / "Query:" prefix
        line = re.sub(r"^\s*(?:\d+[.):\s]+|Query[:\s]+)", "", line, flags=re.IGNORECASE).strip()
        if line:
            return line[:300]
    return ""


# ---------------------------------------------------------------------------
# ProQuest search (one query × one collection)
# ---------------------------------------------------------------------------


def search_proquest(page: Any, ezproxy_url: str, query: str) -> List[Dict[str, Any]]:
    """Drive the page through ProQuest: navigate via EZproxy, fill search
    box, submit via Enter, extract result records. Returns raw record dicts
    (may be empty)."""
    try:
        page.goto(ezproxy_url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
    except Exception as exc:
        print(f"[warn] proxy nav timeout: {exc!s:.80}", flush=True)
        return []

    # Detect "session expired" / "captcha" / "not subscribed" — Opus advised
    # to skip cleanly when these appear.
    final_url = page.url.lower()
    if "sessionexpired" in final_url or "/login" in final_url:
        print("[skip] session expired / login required — re-auth needed", flush=True)
        return []
    page_title = (page.title() or "").lower()
    if "captcha" in page_title or "not authorized" in page_title:
        print("[skip] captcha or not authorized", flush=True)
        return []

    # ProQuest collections vary in UI: International Newsstream lands on
    # Basic Search (`#searchTerm` textarea); US Newsstream lands on
    # Advanced Search but has a "Basic Search" link to navigate to;
    # Historical Newspapers lands on Advanced Search directly with no
    # basic-search route. Detect which form is present and dispatch.
    ui_state = page.evaluate("""() => ({
        has_basic: !!document.querySelector('#searchTerm'),
        has_advanced: !!document.querySelector('#queryTermField'),
        has_basic_link: !!Array.from(document.querySelectorAll('a'))
            .find(a => /^Basic Search$/i.test((a.innerText||'').trim())),
    })""")

    # Prefer basic search when available (simpler URL patterns, often
    # broader recall). Hop to it via the "Basic Search" link if needed.
    if not ui_state["has_basic"] and ui_state["has_basic_link"]:
        try:
            basic_href = page.evaluate("""() => {
                const a = Array.from(document.querySelectorAll('a'))
                  .find(a => /^Basic Search$/i.test((a.innerText||'').trim()));
                return a ? a.getAttribute('href') : null;
            }""")
            if basic_href:
                full = basic_href if basic_href.startswith("http") else f"https://www.proquest.com{basic_href}"
                page.goto(full, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                ui_state = page.evaluate("""() => ({
                    has_basic: !!document.querySelector('#searchTerm'),
                    has_advanced: !!document.querySelector('#queryTermField'),
                })""")
        except Exception:
            pass  # fall through to whatever's available now

    # Dispatch — basic preferred, advanced as fallback.
    try:
        if ui_state.get("has_basic"):
            page.fill("#searchTerm", query)
            page.wait_for_timeout(400)
            page.press("#searchTerm", "Enter")
        elif ui_state.get("has_advanced"):
            # Advanced Search has a hidden Search button (cookie banner often
            # covers it). Fill the visible #queryTermField and submit the
            # form by id directly — works regardless of overlay state.
            page.fill("#queryTermField", query)
            page.wait_for_timeout(400)
            page.evaluate("""() => {
                const f = document.getElementById('searchForm');
                if (f) f.submit();
            }""")
        else:
            print("[skip] no usable search form on this collection page", flush=True)
            return []
        page.wait_for_timeout(8000)
    except Exception as exc:
        print(f"[warn] search submit failed: {exc!s:.80}", flush=True)
        return []

    final_url = page.url
    if "sessionexpired" in final_url.lower():
        print("[skip] session expired post-search", flush=True)
        return []
    if "no results" in (page.title() or "").lower():
        return []  # legitimate 0 hits — return empty

    try:
        records = page.evaluate(PROQUEST_EXTRACTOR_JS) or []
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


_DATE_PAREN_RE = re.compile(r"\(([^)]+)\)")


def _parse_pub_info(pub_info: str) -> Tuple[str, str]:
    """Best-effort split of ProQuest's citation block into (journal, date).

    ProQuest's `.jnlArticle` text typically looks like:
      "Grocer; Crawley (Mar 21, 2026): 30,31,32..."
      "Financial Express; New Delhi. 23 Oct 2014."
      "Times of India; New Delhi (Aug 5, 2009): 1."
    The journal name + city is the part before the first '(', and the
    date sits inside parens. For the older `Newspaper; City. Date.` form,
    fall back to splitting on the last ". ".
    """
    if not pub_info:
        return "", ""
    # Primary form: anything before the first paren-date is journal+city.
    m = _DATE_PAREN_RE.search(pub_info)
    if m:
        date = m.group(1).strip()
        head = pub_info[:m.start()].strip().rstrip(",;:").strip()
        return head, date
    # Fallback: dot-separated form.
    parts = pub_info.rsplit(". ", 1)
    if len(parts) == 2:
        head, tail = parts
        return head.strip().rstrip(",;").strip(), tail.rstrip(".").strip()
    return pub_info.strip(), ""


def write_records(
    records: List[Dict[str, Any]],
    *,
    gap_id: str,
    source_id: str,
    query: str,
    pull_root: Path,
) -> Path:
    """Write records as a JSON file in the existing pull_output schema."""
    out_dir = pull_root / gap_id / source_id
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{_slugify(query)[:60]}.json"
    out_path = out_dir / fname
    rows = []
    for rec in records:
        journal, date = _parse_pub_info(rec.get("pub_info", ""))
        url = rec.get("detail_url") or rec.get("fulltext_url") or ""
        rows.append({
            "title":         rec.get("title", "")[:300],
            "url":           url,
            "pdf_url":       "",
            "abstract":      rec.get("abstract", "")[:2000],
            "authors":       rec.get("authors", ""),
            "journal":       journal,
            "pub_date":      date,
            "doi":           "",
            "query":         query,
            "gap_id":        gap_id,
            "quality_label": "seed",
            "quality_rank":  "20",
            "source":        f"{source_id}_proquest_html",
            "link_type":     "newspaper_record",
            "source_type":   rec.get("source_type", ""),
        })
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="Pull ProQuest International Newsstream coverage for India/China gaps.",
    )
    p.add_argument("--run-id", required=True,
                   help="Run ID (e.g. run_27f86e44394442) — must have a matching gap_report file.")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N relevant gaps (for testing).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print queries and per-gap result counts; do not write JSON.")
    p.add_argument("--model", default="llama3.1:8b",
                   help="LLM model for query rewrites. Default llama3.1:8b — "
                        "follows the terse newspaper-query prompt; gpt-oss:20b "
                        "tends to ask clarifying questions on this prompt format.")
    p.add_argument("--collection", default="international_newsstream",
                   choices=list(JHU_EZPROXY_PROQUEST.keys()),
                   help="Which ProQuest collection to query (default international_newsstream).")
    p.add_argument("--data-root", default=None,
                   help="Override ORCH_DATA_ROOT (default: <repo>/data).")
    args = p.parse_args()

    settings = OrchestratorSettings.from_env()
    data_root = Path(args.data_root) if args.data_root else (settings.data_root if hasattr(settings, "data_root") else PROJECT_ROOT / "data")
    pull_root = Path(data_root) / "pull_outputs" / args.run_id
    if not pull_root.exists():
        sys.exit(f"pull_root not found: {pull_root}")

    # Locate the gap report — match the manuscript_exports layout.
    export_root = Path(data_root) / "manuscript_exports"
    candidates = list(export_root.glob(f"*/gap_report_{args.run_id}.md"))
    if not candidates:
        sys.exit(f"no gap_report file for {args.run_id} under {export_root}")
    gap_report = candidates[0]
    print(f"gap report: {gap_report}", flush=True)

    # Parse gaps
    gaps_all = parse_gap_report(gap_report)
    print(f"total gaps in report: {len(gaps_all)}", flush=True)

    # Filter to relevant gaps
    relevant: List[GapInfo] = []
    for gap_id, chapter, claim in gaps_all:
        region = classify_region(claim)
        if region:
            relevant.append(GapInfo(gap_id, chapter, claim, region))
    print(f"India/China-relevant: {len(relevant)} gaps", flush=True)

    if args.limit:
        relevant = relevant[:args.limit]
        print(f"limited to first {args.limit} for this run", flush=True)

    # LLM client
    llm = make_llm_client(settings, model=args.model, timeout_seconds=120, temperature=0.2)
    source_id = f"proquest_{args.collection}"
    proxy_url = JHU_EZPROXY_PROQUEST[args.collection]

    # Browser
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
        # Bring user's tab back to front so we don't hold focus
        try:
            others = [p for p in ctx.pages if p is not page]
            if others:
                others[0].bring_to_front()
        except Exception:
            pass

    consecutive_session_fails = 0
    total_records = 0
    gaps_with_results = 0

    try:
        for i, gap in enumerate(relevant, 1):
            tag = f"[{i}/{len(relevant)}] {gap.gap_id} ({gap.region})"
            print(f"\n{tag}", flush=True)
            print(f"  claim: {gap.claim_text[:120]}", flush=True)

            # Generate query
            query = generate_newspaper_query(gap.claim_text, gap.region, llm)
            if not query:
                print(f"  [skip] no query generated", flush=True)
                continue
            print(f"  query: {query}", flush=True)

            if args.dry_run:
                continue

            # Run the search (with jitter to avoid bot detection)
            time.sleep(random.uniform(2.0, 5.0))
            records = search_proquest(page, proxy_url, query)
            print(f"  → {len(records)} records", flush=True)
            if not records:
                # Track consecutive empty/error for early-bail per Opus
                consecutive_session_fails += 1
                if consecutive_session_fails >= 5:
                    print("[abort] 5 consecutive empty/error results — likely "
                          "session lost or selector breakage. Stopping.", flush=True)
                    break
                continue
            consecutive_session_fails = 0
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
    print(f"  gaps processed:    {min(len(relevant), i if relevant else 0)}", flush=True)
    print(f"  gaps with results: {gaps_with_results}", flush=True)
    print(f"  total records:     {total_records}", flush=True)


if __name__ == "__main__":
    main()
