#!/usr/bin/env python3
"""Interactive CLI: fetch full document content for a completed pipeline run.

This script is a thin CLI wrapper over ``adapters/document_fetch.py``.  All
fetch logic (collection, seed extraction, PDF download, abstract saving) lives
in the library; this file handles argument parsing, run resolution, user
prompts, and human-readable output.

For each gap the pipeline analyzed, this tool:
  1. Checks which sources returned seed-only results (search URLs, no full text)
  2. (Optionally) prompts you to log in to library databases via the CDP browser
  3. Navigates each search URL and extracts article records
  4. Downloads available PDFs; saves abstracts and HTML where PDFs aren't accessible
  5. Writes everything to the existing pull-output folder so the export bundle picks it up

Usage:
    python scripts/fetch_documents.py
    python scripts/fetch_documents.py --run-id run_27f86e44394442
    python scripts/fetch_documents.py --gap-id AUTO-06-G1
    python scripts/fetch_documents.py --limit 20 --dry-run
    python scripts/fetch_documents.py --cdp-url http://localhost:9222
    python scripts/fetch_documents.py --no-prompt   # non-interactive / scripted use

Requirements:
  - Chrome launched with --remote-debugging-port=9222 (or whatever --cdp-url points to)
  - playwright installed  (pip install playwright && playwright install chromium)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Bootstrap: add project root to path
# ---------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env before importing project modules so credentials are available.
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, v = _line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from config import OrchestratorSettings  # noqa: E402
from adapters.browser_client import make_browser_client  # noqa: E402
from adapters.document_fetch import collect_fetch_items, preview_counts, run_fetch  # noqa: E402

# ---------------------------------------------------------------------------
# Login URLs opened in the browser so the user can authenticate before fetch
# ---------------------------------------------------------------------------

LOGIN_URLS = {
    "ebsco_api":   "https://search.ebscohost.com/login.aspx",
    "ebscohost":   "https://search.ebscohost.com/login.aspx",
    "jstor":       "https://www.jstor.org/",
    "project_muse":"https://muse.jhu.edu/",
    "proquest_historical_newspapers": "https://www.proquest.com/",
    "gale_primary_sources": "https://link.gale.com/",
}

# ---------------------------------------------------------------------------
# Chrome launch guidance (printed when CDP is not yet reachable)
# ---------------------------------------------------------------------------

_CHROME_HELP = """\
┌─ Chrome not reachable ─────────────────────────────────────┐
│ Start Chrome with remote debugging before continuing:        │
│                                                              │
│   /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
│     --remote-debugging-port={port}                          │
│                                                              │
│ Or on Linux / other paths:                                   │
│   google-chrome --remote-debugging-port={port}              │
└──────────────────────────────────────────────────────────────┘"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    settings = OrchestratorSettings.from_env()

    # Override CDP URL from flag (mirrors how main.py wires settings)
    if args.cdp_url:
        os.environ["ORCH_PLAYWRIGHT_CDP_URL"] = args.cdp_url

    # Re-read settings so the overridden CDP URL is picked up.
    settings = OrchestratorSettings.from_env()

    # ── 1. Resolve run directory ──────────────────────────────────────────
    run_id, pull_root = _resolve_run(args.run_id, settings)
    print(f"\n{'='*60}")
    print(f"  Run:  {run_id}")
    print(f"  Pull: {pull_root}")
    print(f"{'='*60}\n")

    # ── 2. Collect items via library ──────────────────────────────────────
    items = collect_fetch_items(pull_root, gap_filter=args.gap_id, limit=args.limit)
    if not items:
        print("No fetchable items found in pull outputs.")
        return

    seeds    = [i for i in items if i.fetch_type == "seed"]
    pdfs     = [i for i in items if i.fetch_type == "pdf"]
    abstracts = [i for i in items if i.fetch_type == "abstract"]

    print(f"Found {len(items)} items across {len({i.gap_id for i in items})} gaps:")
    print(f"  Seed URLs (need login): {len(seeds)}")
    print(f"  PDF downloads:          {len(pdfs)}")
    print(f"  Abstracts (no PDF):     {len(abstracts)}\n")

    if args.dry_run:
        print("[DRY RUN] — no files will be written.")
        for item in items[:20]:
            print(f"  [{item.fetch_type:8}] {item.gap_id:15} {item.source_id:12} {item.url[:70]}")
        return

    # ── 3. Build browser client from settings (like main.py does) ─────────
    browser_client = make_browser_client(settings)

    # ── 4. Sign-in gate: CDP availability + login prompt ──────────────────
    if seeds or pdfs:
        if not browser_client.is_available():
            port = _port_from_url(settings.playwright_cdp_url)
            print(_CHROME_HELP.format(port=port))
            if not args.no_prompt:
                input("\nPress Enter once Chrome is running... ")
            # Rebuild client after Chrome may have started
            browser_client = make_browser_client(settings)
            if not browser_client.is_available():
                print("Still can't reach Chrome. Exiting.")
                sys.exit(1)

        # Open sign-in tabs for sources that need login (seed sources)
        needed_sources = {i.source_id for i in seeds}
        login_targets  = {s: u for s, u in LOGIN_URLS.items() if s in needed_sources}

        if login_targets:
            print("\n┌─ Login required ───────────────────────────────────────────┐")
            print("│ Opening sign-in pages in Chrome. Log in to each:            │")
            for src, url in login_targets.items():
                print(f"│   {src:20}  {url}  │")
            print("└──────────────────────────────────────────────────────────────┘\n")
            browser_client.open_tabs(list(login_targets.values()))
            if not args.no_prompt:
                input("Press Enter once you are logged in to all databases... ")

    # ── 5. Run the full fetch via library ─────────────────────────────────
    stats = run_fetch(items, browser_client, emit=_make_emit())

    # ── 6. Summary ────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Abstracts saved:   {stats.abstracts_saved}")
    print(f"  Seed pages fetched:{stats.seeds_ok} / {stats.seeds_attempted}  "
          f"({stats.seeds_failed} failed)")
    print(f"  PDFs downloaded:   {stats.pdfs_ok} / {stats.pdfs_attempted}  "
          f"({stats.pdfs_failed} failed)")
    print(f"  Articles extracted:{stats.articles_extracted}")
    print(f"  Total OK:          {stats.total_ok}")
    print(f"{'─'*60}\n")
    print(f"Done.  Files written under {pull_root}\n")


# ---------------------------------------------------------------------------
# emit factory
# ---------------------------------------------------------------------------

def _make_emit():
    """Return an emit callable that prints structured log lines to stdout.

    document_fetch.run_fetch calls emit(stage, status, message, meta_dict).
    This formatter prints ``[stage/status] message`` so CLI output is readable.
    """
    def _emit(stage: str, status: str, message: str, meta: dict = None) -> None:
        print(f"[{stage}/{status}] {message}")

    return _emit


# ---------------------------------------------------------------------------
# Run resolution
# ---------------------------------------------------------------------------

def _resolve_run(run_id_arg: Optional[str], settings: OrchestratorSettings) -> Tuple[str, Path]:
    """Resolve a run ID and its pull-output directory.

    Precedence:
    1. Explicit --run-id flag
    2. Most-recent complete/partial run from local API (http://localhost:8876)
    3. Most-recently-modified directory under pull_outputs/
    """
    pull_root_base = settings.data_root / "pull_outputs"

    if run_id_arg:
        p = pull_root_base / run_id_arg
        if not p.exists():
            print(f"Run directory not found: {p}")
            sys.exit(1)
        return run_id_arg, p

    # Try local API for the most recent completed run
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

    # Fall back to the most-recently-modified directory on disk
    try:
        dirs = sorted(pull_root_base.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
        dirs = [d for d in dirs if d.is_dir()]
        if dirs:
            return dirs[0].name, dirs[0]
    except (FileNotFoundError, PermissionError):
        pass

    print(f"No run directories found under {pull_root_base}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _port_from_url(cdp_url: str) -> int:
    """Extract port number from a CDP URL, defaulting to 9222."""
    try:
        return urllib.parse.urlparse(cdp_url).port or 9222
    except Exception:
        return 9222


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch full document content for a completed pipeline run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--run-id",    help="Run ID to fetch documents for (default: most recent)")
    p.add_argument("--gap-id",    help="Fetch documents for a single gap only")
    p.add_argument("--limit",     type=int, help="Max number of items to fetch")
    p.add_argument("--cdp-url",   default=None, help="CDP endpoint URL (default: from .env)")
    p.add_argument("--dry-run",   action="store_true",
                   help="Print what would be fetched; write nothing")
    p.add_argument("--no-prompt", action="store_true",
                   help="Skip all interactive input() prompts (non-interactive / scripted use)")
    return p.parse_args()


if __name__ == "__main__":
    main()
