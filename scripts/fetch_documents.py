#!/usr/bin/env python3
"""Interactive CLI: fetch full document content for a completed pipeline run.

This script is a thin CLI wrapper over ``adapters/document_fetch.py``.  All
fetch logic (collection, seed extraction, PDF download, abstract saving) lives
in the library; this file handles argument parsing, run resolution, user
prompts, and human-readable output.

For each gap the pipeline analyzed, this tool:
  1. Checks which sources returned seed-only results (search URLs, no full text)
  2. Auto-launches Chrome with CDP if not already reachable (use --no-launch to disable)
  3. (Optionally) prompts you to log in to library databases via the CDP browser
  4. Navigates each search URL and extracts article records
  5. Downloads available PDFs; saves abstracts and HTML where PDFs aren't accessible
  6. Writes everything to the existing pull-output folder so the export bundle picks it up

Usage:
    python scripts/fetch_documents.py
    python scripts/fetch_documents.py --run-id run_27f86e44394442
    python scripts/fetch_documents.py --gap-id AUTO-06-G1
    python scripts/fetch_documents.py --limit 20 --dry-run
    python scripts/fetch_documents.py --cdp-url http://localhost:9222
    python scripts/fetch_documents.py --no-prompt   # non-interactive / scripted use
    python scripts/fetch_documents.py --no-launch   # print help and wait instead of auto-launching

Auto-launch behavior:
  - When Chrome is not reachable, the script spawns Chrome automatically using a
    dedicated profile at ~/.research_henchman_chrome (so it does not close your
    normal Chrome tabs or collide with your existing Chrome profile).
  - The dedicated profile persists library logins across runs — sign in once and
    subsequent fetches will find the session already live.
  - Use --no-launch to opt out and get the old "print help and wait" behavior.
  - playwright installed  (pip install playwright && playwright install chromium)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
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
# Chrome auto-launch helpers
# ---------------------------------------------------------------------------

def _launch_chrome(port: int) -> Optional[int]:
    """Spawn Chrome with CDP enabled on *port* and return its PID.

    Uses a dedicated ``~/.research_henchman_chrome`` user-data-dir so the
    process runs alongside the user's normal Chrome without touching their
    profile or closing their existing tabs.  The profile also persists library
    logins across CLI runs.

    Candidate executables (first found wins):
      macOS  — /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
      Linux  — google-chrome, then chromium

    Returns the spawned PID on success, or None when no Chrome executable is
    found (caller should print an error and exit).
    """
    user_data_dir = Path.home() / ".research_henchman_chrome"

    # Resolve the executable: try macOS path first, then Linux PATH names.
    mac_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(mac_path).exists():
        executable = mac_path
    else:
        # Try linux names via PATH
        for candidate in ("google-chrome", "chromium"):
            found = shutil.which(candidate)
            if found:
                executable = found
                break
        else:
            return None  # no Chrome found

    cmd = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach so Chrome survives script exit
    )
    return proc.pid


def _cdp_poll_until_ready(cdp_url: str, timeout_seconds: float = 15.0) -> bool:
    """Poll ``<cdp_url>/json/version`` every 0.5 s until reachable or timeout.

    Uses the same urllib + Host-header pattern as BrowserClient._playwright_cdp_ping().
    Returns True when the endpoint responds 200, False if timeout expires.
    """
    parsed = urllib.parse.urlparse(cdp_url)
    host_header = parsed.netloc or "localhost"
    version_url = f"{cdp_url.rstrip('/')}/json/version"
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(version_url, headers={"Host": host_header})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)

    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    settings = OrchestratorSettings.from_env()

    # Override CDP URL from flag (mirrors how main.py wires settings)
    if args.cdp_url:
        os.environ["ORCH_PLAYWRIGHT_CDP_URL"] = args.cdp_url

    # PDF worker / throttle tuning — flags override env vars so the user can
    # set knobs per-invocation without exporting variables. Each flag maps to
    # the matching ORCH_PDF_* variable read by adapters/document_fetch.py.
    if args.workers is not None:
        os.environ["ORCH_PDF_WORKERS"] = str(args.workers)
    if args.throttle_cooldown is not None:
        os.environ["ORCH_PDF_THROTTLE_COOLDOWN_SEC"] = str(args.throttle_cooldown)
    if args.throttle_threshold is not None:
        os.environ["ORCH_PDF_THROTTLE_THRESHOLD"] = str(args.throttle_threshold)
    if args.max_throttle_pauses is not None:
        os.environ["ORCH_PDF_MAX_THROTTLE_PAUSES"] = str(args.max_throttle_pauses)
    if args.jitter_ms is not None:
        os.environ["ORCH_PDF_JITTER_MS"] = str(args.jitter_ms)
    if args.ebsco_opid is not None:
        os.environ["ORCH_EBSCO_OPID"] = args.ebsco_opid
    if args.ebsco_db is not None:
        os.environ["ORCH_EBSCO_DB"] = args.ebsco_db

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
            if args.no_launch:
                # Legacy behavior: print help and wait for user to start Chrome.
                print(_CHROME_HELP.format(port=port))
                if not args.no_prompt:
                    input("\nPress Enter once Chrome is running... ")
                # Rebuild client after Chrome may have started
                browser_client = make_browser_client(settings)
                if not browser_client.is_available():
                    print("Still can't reach Chrome. Exiting.")
                    sys.exit(1)
            else:
                # Auto-launch Chrome with a dedicated CDP profile.
                print(f"Chrome not reachable on port {port} — launching automatically…")
                pid = _launch_chrome(port)
                if pid is None:
                    print(
                        "ERROR: No Chrome executable found. Install Google Chrome or use "
                        "--no-launch and start it manually."
                    )
                    sys.exit(1)
                print(f"  Chrome spawned (PID {pid}). Waiting for CDP…")
                if not _cdp_poll_until_ready(settings.playwright_cdp_url):
                    print("  Chrome did not become reachable within 15 s. Exiting.")
                    sys.exit(1)
                print("  CDP ready.")
                # Rebuild client now that Chrome is up.
                browser_client = make_browser_client(settings)

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
    on_blocked = None if args.no_prompt else _make_on_blocked()
    # CAPTCHA pause defaults: ON when running interactively, OFF with
    # --no-prompt (no TTY → can't take input). User can override either way.
    if args.pause_on_captcha is None:
        pause_on_captcha = not args.no_prompt
    else:
        pause_on_captcha = bool(args.pause_on_captcha)
        if pause_on_captcha and args.no_prompt:
            print("[warn] --pause-on-captcha disabled because --no-prompt was given "
                  "(input() would EOF immediately).")
            pause_on_captcha = False
    stats = run_fetch(
        items, browser_client,
        emit=_make_emit(pause_on_captcha=pause_on_captcha),
        on_blocked=on_blocked,
        pause_on_captcha=pause_on_captcha,
    )

    # ── 6. Summary ────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Abstracts saved:    {stats.abstracts_saved}")
    print(f"  Seed pages fetched: {stats.seeds_ok} / {stats.seeds_attempted}  "
          f"({stats.seeds_failed} failed)")
    print(f"  PDFs downloaded:    {stats.pdfs_ok} / {stats.pdfs_attempted}  "
          f"({stats.pdfs_failed} failed)")
    print(f"  Articles extracted: {stats.articles_extracted}")
    if stats.inline_pdfs_attempted:
        extras = []
        if stats.inline_pdfs_throttled:
            extras.append(f"{stats.inline_pdfs_throttled} throttled")
        if stats.inline_pdfs_failed:
            extras.append(f"{stats.inline_pdfs_failed} failed")
        extras_str = (", " + ", ".join(extras)) if extras else ""
        print(f"  Article PDFs saved: {stats.inline_pdfs_ok} / {stats.inline_pdfs_attempted}  "
              f"({stats.inline_pdfs_unavailable} no PDF{extras_str})")
        if stats.throttle_pauses or stats.throttle_exhausted:
            print(f"  Throttle events:    {stats.throttle_pauses} pauses, "
                  f"{stats.throttle_resumes} resumes, {stats.throttle_exhausted} exhausted")
        if stats.captcha_pauses:
            print(f"  CAPTCHA pauses:     {stats.captcha_pauses} (resumed: {stats.captcha_resumes})")
    print(f"  Total OK:           {stats.total_ok}")
    print(f"{'─'*60}\n")
    print(f"Done.  Files written under {pull_root}\n")


# ---------------------------------------------------------------------------
# emit factory
# ---------------------------------------------------------------------------

def _make_emit(*, pause_on_captcha: bool = False):
    """Return an emit callable that prints structured log lines to stdout.

    document_fetch.run_fetch calls emit(stage, status, message, meta_dict).
    This formatter prints ``[stage/status] message`` so CLI output is
    readable.

    Special handling:
      - ``throttle_paused`` / ``throttle_resumed`` / ``throttle_exhausted``
        also fire a best-effort Telegram ping per AGENTS.md §15.
      - ``captcha_paused`` (when ``pause_on_captcha`` is enabled): print a
        clear banner, ping Telegram, then BLOCK on ``input()`` until the
        user has solved the challenge in the surfaced Chrome tab and
        pressed Enter. The pool's worker thread is blocked on this call,
        so returning from input() automatically signals "resume."
    """
    def _emit(stage: str, status: str, message: str, meta: dict = None) -> None:
        print(f"[{stage}/{status}] {message}", flush=True)

        if status in ("throttle_paused", "throttle_resumed", "throttle_exhausted"):
            _try_telegram(f"[fetch_documents] {message}")
            return

        if status == "captcha_paused" and pause_on_captcha:
            meta = meta or {}
            banner = (
                f"\n┌─ PAUSED — PDF worker hit a CAPTCHA ───────────────────┐\n"
                f"│ Gap:    {meta.get('gap_id', '?')}\n"
                f"│ Title:  {meta.get('title', '')}\n"
                f"│ URL:    {meta.get('url', '')}\n"
                f"│ Hint:   {meta.get('action_required', 'Solve challenge in browser')}\n"
                f"│ ACTION: solve in the Chrome tab that just came forward, then press Enter.\n"
                f"└────────────────────────────────────────────────────────┘"
            )
            print(banner, flush=True)
            _try_telegram(
                f"[fetch_documents] PDF worker PAUSED on CAPTCHA "
                f"({meta.get('gap_id', '?')}). Solve in browser, press Enter in terminal."
            )
            try:
                input("Press Enter once you've solved the CAPTCHA (or Ctrl-C to abort)... ")
            except (EOFError, KeyboardInterrupt):
                # On EOF / Ctrl-C, fall through and let the pool resume —
                # the article will be retried once and likely fail; nothing
                # else gets stuck.
                print("[warn] CAPTCHA prompt aborted; resuming pool — this article will be skipped.",
                      flush=True)
            return

        if status == "captcha_resumed":
            _try_telegram(f"[fetch_documents] {message}")
            return

    return _emit


_RATE_LIMIT_BACKOFF_SECONDS = 60   # fixed wait for rate_limit; library retries once


def _make_on_blocked():
    """Return an on_blocked callable with reason-aware recovery.

    document_fetch.fetch_seed_page calls on_blocked(item, page_result) when a
    page is gated. The handler dispatches by ``page_result.blocked_reason``:

    - ``rate_limit``  → sleep and retry without prompting (a human can't help
      here; the server needs time). 60 s fixed; one retry from the library.
    - ``access_denied`` → return False (skip without pause). Subscription /
      authorization issues won't change between back-to-back retries.
    - ``captcha`` / ``login`` / unknown → print a banner, ping Telegram, block
      on input() until the user solves the challenge in the CDP browser, then
      return True so the library retries the URL once.

    Telegram delivery (per AGENTS.md §15) is best-effort and silent on missing
    credentials so the recovery flow works even without notifications.
    """
    def _on_blocked(item, page_result) -> bool:
        reason = page_result.blocked_reason or "blocked"
        action = page_result.action_required or ""

        # Rate-limited: sleep + auto-retry. Humans can't accelerate this.
        if reason == "rate_limit":
            delay = _RATE_LIMIT_BACKOFF_SECONDS
            msg = (
                f"\n[rate_limit] {item.gap_id}/{item.source_id}: "
                f"sleeping {delay}s before retry — {action or 'server-side cooldown'}"
            )
            print(msg, flush=True)
            _try_telegram(
                f"[fetch_documents] rate-limited at {item.gap_id}/{item.source_id}; "
                f"backing off {delay}s then retrying"
            )
            time.sleep(delay)
            return True

        # Access denied: auth/subscription problem, retry won't help. Skip
        # without pausing — the user can review _blocked.html later if needed.
        if reason == "access_denied":
            print(
                f"\n[access_denied] {item.gap_id}/{item.source_id}: "
                f"skipping (retry will not help) — {action or 'no access'}",
                flush=True,
            )
            return False

        # CAPTCHA / login / unknown: human-in-the-loop pause + retry.
        msg = (
            f"\n┌─ PAUSED — page blocked ({reason}) ────────────────────────┐\n"
            f"│ Gap:    {item.gap_id}\n"
            f"│ Source: {item.source_id}\n"
            f"│ URL:    {item.url[:90]}\n"
            f"│ Hint:   {action or 'Solve challenge in browser'}\n"
            f"│ ACTION: solve the challenge in the Chrome window, then press Enter.\n"
            f"└────────────────────────────────────────────────────────────┘"
        )
        print(msg, flush=True)
        _try_telegram(
            f"[fetch_documents] PAUSED ({reason}) for {item.gap_id}/{item.source_id}. "
            f"Solve in browser, then press Enter in terminal."
        )
        try:
            input("Press Enter once unblocked (or Ctrl-C to abort)... ")
        except (EOFError, KeyboardInterrupt):
            return False
        return True

    return _on_blocked


def _try_telegram(text: str) -> None:
    """Best-effort Telegram notification — silent on any failure."""
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.exists():
            return
        cfg = json.loads(settings_path.read_text())
        env = cfg.get("env", {})
        token = env.get("TELEGRAM_BOT_TOKEN")
        chat = env.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            return
        body = urllib.parse.urlencode({"chat_id": str(chat), "text": text}).encode()
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=body,
            ),
            timeout=5,
        )
    except Exception:
        pass


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
    p.add_argument("--no-launch", action="store_true",
                   help="Do not auto-launch Chrome; print help and wait instead (legacy behavior)")
    p.add_argument("--workers", type=int, default=None,
                   help="Number of parallel PDF-fetch workers (default 4; "
                        "1 = sequential, gentlest on EBSCO; sets ORCH_PDF_WORKERS)")
    p.add_argument("--throttle-cooldown", type=int, default=None,
                   help="Base cooldown seconds when EBSCO throttles (linear backoff: "
                        "first pause sleeps this long, second sleeps 2x, third 3x; "
                        "default 300 = 5 min; sets ORCH_PDF_THROTTLE_COOLDOWN_SEC)")
    p.add_argument("--throttle-threshold", type=int, default=None,
                   help="Consecutive page-navigation timeouts that trigger a pool pause "
                        "(default 3; sets ORCH_PDF_THROTTLE_THRESHOLD)")
    p.add_argument("--max-throttle-pauses", type=int, default=None,
                   help="Stop retrying after this many throttle pauses; remaining articles "
                        "skip with throttle_exhausted (default 3; sets ORCH_PDF_MAX_THROTTLE_PAUSES)")
    p.add_argument("--jitter-ms", type=int, default=None,
                   help="Per-task random sleep upper bound in ms — spreads request stream "
                        "(default 800; sets ORCH_PDF_JITTER_MS; pass 0 to disable)")
    p.add_argument("--ebsco-opid", default=None,
                   help="EBSCO institutional profile ID (e.g. '6hfcoc' for JHU "
                        "Libraries Academic Search Ultimate). When set, legacy "
                        "search.ebscohost.com/login.aspx URLs are rewritten to "
                        "research.ebsco.com/c/<opid>/... — bypasses cookie-priority "
                        "auto-redirects when you have multiple EBSCO institutional "
                        "profiles. Sets ORCH_EBSCO_OPID.")
    p.add_argument("--ebsco-db", default=None,
                   help="Comma-separated EBSCO database codes used together with "
                        "--ebsco-opid (default 'asn,bsu'). Sets ORCH_EBSCO_DB.")
    captcha_grp = p.add_mutually_exclusive_group()
    captcha_grp.add_argument("--pause-on-captcha", dest="pause_on_captcha", action="store_true", default=None,
                             help="When a PDF worker hits a CAPTCHA, pause the whole pool, "
                                  "surface the tab, and wait for you to solve it (default: ON when "
                                  "interactive / OFF with --no-prompt)")
    captcha_grp.add_argument("--no-pause-on-captcha", dest="pause_on_captcha", action="store_false",
                             help="Override the default and skip CAPTCHA-blocked articles silently")
    return p.parse_args()


if __name__ == "__main__":
    main()
