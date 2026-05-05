#!/usr/bin/env python3
"""Pull Internet Archive (archive.org) book coverage for manuscript gaps.

Modeled on scripts/pull_hathitrust.py — same CLI, same output schema,
same article-indexer auto-discovery convention.

For each gap, generates an IA-friendly natural-language query via the local
LLM (llama3.1:8b), queries IA's advanced search API, and persists matched
records as JSON seed files. The article indexer (`adapters/article_index.py`)
auto-discovers new `<gap_id>/<source_id>/*.json` files on next run.

Access labeling:
  - ``access='Full view'``    — item has downloads > 0 (publicly accessible)
  - ``access='Library only'`` — item exists but downloads == 0 (access-restricted)

Output schema matches HathiTrust seeds so the indexer handles them identically.

Usage:
    python3 scripts/pull_internet_archive.py --gap-ids CP31 --run-id ia-smoke-test
    python3 scripts/pull_internet_archive.py --run-id ia-wave1
    python3 scripts/pull_internet_archive.py --run-id ... --limit 5 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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
from adapters.internet_archive import search as ia_search  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_ID = "internet_archive"

# LLM system prompt for IA queries. IA's collection spans pre-1928 public
# domain, government documents, digitized newspapers, and contemporary
# open-access materials — much broader than HathiTrust. We bias toward:
#   1. Historical period vocabulary (same as HathiTrust)
#   2. Named entities (companies, people, places)
#   3. Document type hints (e.g. "annual report", "catalog", "newspaper")
IA_QUERY_SYSTEM = """\
You are an expert in Internet Archive full-text search. Generate ONE
concise natural-language query to find books, documents, and historical
texts on the research topic provided.

Rules:
1. Internet Archive contains a wide range: pre-1928 public domain books,
   government documents, digitized newspapers, annual reports, catalogs,
   and contemporary open-access materials.
2. Use PERIOD VOCABULARY where the topic is historical:
   "mail order", "department store", "chain store", "annual report",
   "corporation", "trust", "monopoly" — not modern jargon.
3. Include specific NAMED ENTITIES: company names, founder names, place
   names — these appear in titles and improve precision.
4. Keep the query SHORT — under 100 characters. IA uses Solr full-text
   search, so 2-5 key terms work better than deep Boolean chains.
5. Recall over precision — even diluted hits provide useful context.

Output: a SINGLE query, one line, no commentary, no numbering, no markdown.
Just the query string.

Examples:

Research gap: "Mail-order catalogs democratized rural consumption in America."
Query: "Sears Roebuck" OR "mail order catalog" rural consumption

Research gap: "Department-store credit accounts predated modern consumer credit."
Query: department store credit charge account retail

Research gap: "Alibaba shaped China's retail landscape."
Query: Alibaba China e-commerce retail marketplace

Research gap: "Mercado Libre dominated Latin American e-commerce."
Query: "Mercado Libre" Latin America e-commerce retail
"""


# ---------------------------------------------------------------------------
# Gap loading from gap_tree SQLite
# ---------------------------------------------------------------------------


def load_gaps_from_db(
    db_path: Path,
    *,
    gap_ids: Optional[List[str]] = None,
    tier: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return gap_tree rows to pull IA sources for.

    Skips editorial_todo rows (no pullable content). When *gap_ids* is
    provided, returns only those gaps (ignoring tier). When *tier* is
    provided, restricts to that tier. When neither is given, returns all
    pullable gaps.
    """
    import sqlite3
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if gap_ids:
            placeholders = ",".join("?" for _ in gap_ids)
            rows = conn.execute(
                f"SELECT * FROM gap_tree WHERE gap_id IN ({placeholders})",
                gap_ids,
            ).fetchall()
        elif tier is not None:
            rows = conn.execute(
                "SELECT * FROM gap_tree WHERE tier = ?", (tier,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM gap_tree").fetchall()
        return [
            dict(r) for r in rows
            if (r["gap_type"] or "") != "editorial_todo"
        ]
    except Exception:
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Query generation
# ---------------------------------------------------------------------------


def generate_ia_query(claim_text: str, llm_client: Any) -> str:
    """Rewrite the gap claim into an IA-friendly natural-language query."""
    user_msg = f'Research gap: "{claim_text.strip()}"\nQuery:'
    try:
        response = llm_client.complete(
            system=IA_QUERY_SYSTEM,
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
# Record persistence
# ---------------------------------------------------------------------------


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s or "").strip()
    return re.sub(r"[\s_-]+", "_", s).strip("_") or "query"


def write_records(
    records: List[Dict[str, Any]],
    *,
    gap_id: str,
    query: str,
    pull_root: Path,
) -> Path:
    """Write IA search results as a JSON seed file in the pull_output schema.

    Output shape matches HathiTrust seeds (same _ingest_seed_json walk).
    Fields: title, url (IA detail page), pdf_url, abstract (description),
    authors (creator), journal, pub_date (date), doi='', query, gap_id,
    quality_label='seed', source='internet_archive', link_type='book_record',
    access, subject, language.
    """
    out_dir = pull_root / gap_id / SOURCE_ID
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{_slugify(query)[:60]}.json"
    out_path = out_dir / fname

    rows = []
    for rec in records:
        identifier = rec.get("identifier", "")
        title = (rec.get("title") or "").strip()[:300]
        if not title:
            continue

        url = f"https://archive.org/details/{identifier}" if identifier else ""
        authors = (rec.get("creator") or "").strip()
        abstract = (rec.get("description") or "").strip()[:1000]
        pub_date = (rec.get("date") or "").strip()

        # Access label: public items have downloads > 0.
        downloads = int(rec.get("downloads") or 0)
        access = "Full view" if downloads > 0 else "Library only"

        rows.append({
            "title":         title,
            "url":           url,
            "pdf_url":       "",  # resolved on demand via download_url()
            "abstract":      abstract,
            "authors":       authors,
            "journal":       "",  # IA books don't have a journal field
            "pub_date":      pub_date,
            "doi":           "",
            "query":         query,
            "gap_id":        gap_id,
            "quality_label": "seed",
            "quality_rank":  "20",
            "source":        SOURCE_ID,
            "link_type":     "book_record",
            # Availability tagging (Phase 1 schema)
            "access":        access,
            "hathi_id":      "",    # not applicable for IA
            "subject":       "",    # IA format list used instead
            "language":      "",
            # IA-specific extras (preserved; indexer ignores unknown keys)
            "ia_identifier": identifier,
            "ia_downloads":  downloads,
        })

    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="Pull Internet Archive book coverage for manuscript gaps.",
    )
    p.add_argument("--run-id", required=True,
                   help="Run ID for output dir (e.g. ia-smoke-test).")
    p.add_argument("--gap-ids", default=None,
                   help="Comma-separated gap IDs to pull. Overrides --tier.")
    p.add_argument("--tier", type=int, default=None,
                   help="Only pull gaps at this tier.")
    p.add_argument("--limit", type=int, default=None,
                   help="Process only the first N gaps (for testing).")
    p.add_argument("--ia-limit", type=int, default=50,
                   help="Max IA search results per query (default 50).")
    p.add_argument("--model", default="llama3.1:8b",
                   help="LLM model for query rewrites.")
    p.add_argument("--data-root", default=None,
                   help="Override ORCH_DATA_ROOT.")
    p.add_argument("--db", default=None,
                   help="SQLite DB path (default: <data-root>/article_index.sqlite).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print queries; don't write JSON.")
    args = p.parse_args()

    settings = OrchestratorSettings.from_env()
    data_root = Path(args.data_root) if args.data_root else (
        settings.data_root if hasattr(settings, "data_root") else PROJECT_ROOT / "data"
    )
    db_path = Path(args.db) if args.db else data_root / "article_index.sqlite"
    pull_root = data_root / "pull_outputs" / args.run_id
    if not args.dry_run:
        pull_root.mkdir(parents=True, exist_ok=True)

    # Load gap list
    gap_ids: Optional[List[str]] = None
    if args.gap_ids:
        gap_ids = [g.strip() for g in args.gap_ids.split(",") if g.strip()]

    gaps = load_gaps_from_db(db_path, gap_ids=gap_ids, tier=args.tier)

    if not gaps:
        print("No gaps found — check --gap-ids / --tier / --db path.", flush=True)
        sys.exit(0)

    if args.limit:
        gaps = gaps[:args.limit]

    print(f"DB:        {db_path}", flush=True)
    print(f"Run ID:    {args.run_id}", flush=True)
    print(f"Pull root: {pull_root}", flush=True)
    print(f"Gaps:      {len(gaps)}", flush=True)
    print(f"IA limit:  {args.ia_limit} per query", flush=True)

    llm = make_llm_client(settings, model=args.model, timeout_seconds=120, temperature=0.2)

    total_records = 0
    gaps_with_results = 0
    consecutive_zero = 0

    for i, gap in enumerate(gaps, 1):
        gap_id    = gap.get("gap_id", "")
        claim     = (gap.get("claim_text") or "").strip()
        print(f"\n[{i}/{len(gaps)}] {gap_id}", flush=True)
        if claim:
            print(f"  claim: {claim[:120]}", flush=True)
        else:
            print(f"  [skip] no claim_text", flush=True)
            continue

        query = generate_ia_query(claim, llm)
        if not query:
            print(f"  [skip] no query generated", flush=True)
            continue
        print(f"  query: {query}", flush=True)

        if args.dry_run:
            continue

        # Small politeness jitter on top of the adapter's built-in throttle.
        time.sleep(0.3)

        records = ia_search(query, limit=args.ia_limit)
        print(f"  -> {len(records)} IA records", flush=True)

        if not records:
            consecutive_zero += 1
            if consecutive_zero >= 10:
                print("[abort] 10 consecutive empty results — IA may be rate-limiting.", flush=True)
                break
            continue
        consecutive_zero = 0

        out_path = write_records(
            records,
            gap_id=gap_id,
            query=query,
            pull_root=pull_root,
        )
        total_records += len(records)
        gaps_with_results += 1
        print(f"  saved -> {out_path.relative_to(PROJECT_ROOT)}", flush=True)

    print(f"\n=== summary ===", flush=True)
    print(f"  gaps processed:    {i}", flush=True)
    print(f"  gaps with results: {gaps_with_results}", flush=True)
    print(f"  total records:     {total_records}", flush=True)


if __name__ == "__main__":
    main()
