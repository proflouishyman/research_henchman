"""Dispatcher: take a gap_tree node, plan queries, run each via the right puller.

Resume-safe via ``gap_tree.status``:
  - ``pending``        — node has not been touched yet
  - ``queries_built``  — at least one source ran but not all (errors)
  - ``pulled``         — all queries dispatched + JSON files on disk

Per-(query, source) routing:

  ebsco_api      → adapters.keyed_apis.EbscoApiAdapter().pull(...) — same
                   path the legacy gap-pull pipeline uses; writes
                   ``<gap>/ebsco_api/<query>.json`` and (when API hits)
                   article rows the indexer ingests directly via the
                   _ingest_seed_json walk.
  hathitrust_fulltext        → scripts.pull_hathitrust.search_hathitrust(page, q)
  proquest_us_newsstream     → scripts.pull_proquest_newspapers.search_proquest
  proquest_international_newsstream → same as above with intl proxy URL
  sec_edgar_10k  → adapters.sec_edgar.lookup_cik / list_filings /
                   fetch_filing_text. Filings serialized as seed JSON
                   in the existing pull_outputs schema.

The browser ``page`` object is opened ONCE by the caller and passed in
so we don't pay Playwright launch cost per query. Per-gap pulls are
sequential (HathiTrust + ProQuest are politeness-throttled anyway).

Public surface:
  - ``pull_gap(conn, node, *, run_id, llm, pull_root, page=None,
               sec_user_agent=DEFAULT_UA) -> Dict``

Returned dict shape:
  {"records_pulled": int, "sources_used": List[str],
   "queries_run": int, "errors": List[str]}
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Project root must be on sys.path before relative imports of scripts/* —
# layers/ files don't trigger the bootstrap most CLI scripts do, so be
# defensive here.
_LAYERS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _LAYERS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from adapters import sec_edgar  # noqa: E402
from adapters.gap_tree import (  # noqa: E402
    list_nodes,
)
from layers.gap_query_planner import (  # noqa: E402
    SRC_EBSCO,
    SRC_HATHI,
    SRC_IA,
    SRC_PQ_INTL,
    SRC_PQ_US,
    SRC_SEC_10K,
    plan_queries,
)


# ---------------------------------------------------------------------------
# Helpers — mirror the slugify used by other puller scripts so output paths
# stay consistent with the pre-existing pull_outputs convention.
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^\w\s-]")


def _slugify(s: str) -> str:
    s = _SLUG_RE.sub("", s or "").strip()
    return re.sub(r"[\s_-]+", "_", s).strip("_") or "query"


# ---------------------------------------------------------------------------
# gap_tree status updates
# ---------------------------------------------------------------------------


def _set_status(
    conn: sqlite3.Connection, gap_id: str, status: str
) -> None:
    conn.execute(
        "UPDATE gap_tree SET status = ? WHERE gap_id = ?",
        (status, gap_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# SEC EDGAR shim — produce a seed JSON file shaped like the rest of the
# pullers' outputs so the article indexer auto-ingests it.
# ---------------------------------------------------------------------------


def _pull_sec_edgar(
    *,
    entity: str,
    gap_id: str,
    pull_root: Path,
    user_agent: str,
    forms: Tuple[str, ...] = ("10-K", "10-Q", "S-1", "DEF 14A"),
    limit: int = 8,
    fetch_text: bool = False,
) -> Tuple[int, Optional[Path], List[str]]:
    """Look up *entity*, list filings, write a seed JSON file.

    Returns (records_written, output_path, errors).

    *fetch_text* is False by default — we keep the indexed metadata light.
    Callers (e.g. dossier generators) can re-fetch the filing text on
    demand using ``adapters.sec_edgar.fetch_filing_text``.
    """
    errors: List[str] = []
    cik = sec_edgar.lookup_cik(entity, user_agent=user_agent)
    if not cik:
        # Not a hard error — many manuscript entities (defunct companies,
        # privately-held firms, non-US firms) have no SEC CIK. Caller
        # treats this as "0 records, no error".
        return 0, None, errors

    try:
        filings = sec_edgar.list_filings(
            cik, forms, limit=limit, user_agent=user_agent,
        )
    except Exception as exc:
        errors.append(f"sec_edgar list_filings({cik}): {exc!s:.150}")
        return 0, None, errors

    if not filings:
        return 0, None, errors

    rows: List[Dict[str, Any]] = []
    for f in filings:
        title = f"{entity} — {f['form']} — {f['filing_date']}"
        rows.append({
            "title":         title[:300],
            "url":           f.get("primary_doc_url", ""),
            "pdf_url":       "",
            "abstract":      "",  # filings are long; text-fetch is on demand
            "authors":       "",
            "journal":       "SEC EDGAR",
            "pub_date":      f.get("filing_date", ""),
            "doi":           "",
            "query":         entity,
            "gap_id":        gap_id,
            "quality_label": "seed",
            "quality_rank":  "20",
            "source":        "sec_edgar",
            "link_type":     "regulatory_filing",
            "form":          f.get("form", ""),
            "accession_number": f.get("accession_number", ""),
            "report_date":   f.get("report_date", ""),
            "primary_document": f.get("primary_document", ""),
            "cik":           cik,
        })

    out_dir = pull_root / gap_id / SRC_SEC_10K
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_slugify(entity)[:60]}.json"
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return len(rows), out_path, errors


# ---------------------------------------------------------------------------
# EBSCO shim — uses the existing keyed adapter so the EIT / EDS / seed
# fallback chain works unchanged.
# ---------------------------------------------------------------------------


def _pull_ebsco(
    *,
    query: str,
    gap_id: str,
    claim_text: str,
    pull_root: Path,
    timeout_seconds: int = 60,
) -> Tuple[int, Optional[Path], List[str]]:
    """Run EbscoApiAdapter.pull(...) with a synthetic PlannedGap.

    The adapter writes its own JSON file under <run_dir>/<gap_id>/ebsco_api/
    via ``write_json_records`` — we simply return the directory it used so
    the caller can verify presence.
    """
    errors: List[str] = []
    try:
        from adapters.keyed_apis import EbscoApiAdapter
        from contracts import GapPriority, PlannedGap
    except Exception as exc:
        errors.append(f"ebsco import: {exc!s:.150}")
        return 0, None, errors

    pg = PlannedGap(
        gap_id=gap_id,
        chapter="",
        claim_text=claim_text,
        priority=GapPriority.MEDIUM,
        search_queries=[query],
    )

    adapter = EbscoApiAdapter()
    try:
        result = adapter.pull(pg, query, str(pull_root), timeout_seconds=timeout_seconds)
    except Exception as exc:
        errors.append(f"ebsco pull: {exc!s:.150}")
        return 0, None, errors

    out_dir = pull_root / gap_id / SRC_EBSCO
    return int(result.document_count or 0), (out_dir if out_dir.exists() else None), errors


# ---------------------------------------------------------------------------
# HathiTrust shim — directly reuses scripts.pull_hathitrust helpers.
# ---------------------------------------------------------------------------


def _pull_hathitrust(
    *,
    page: Any,
    query: str,
    gap_id: str,
    pull_root: Path,
) -> Tuple[int, Optional[Path], List[str]]:
    errors: List[str] = []
    if page is None:
        errors.append("hathitrust: no browser page")
        return 0, None, errors
    try:
        from scripts.pull_hathitrust import search_hathitrust, write_records
    except Exception as exc:
        errors.append(f"hathitrust import: {exc!s:.150}")
        return 0, None, errors

    # Polite jitter — HathiTrust is non-profit hosted.
    time.sleep(random.uniform(1.5, 3.5))
    try:
        records = search_hathitrust(page, query) or []
    except Exception as exc:
        errors.append(f"hathitrust search: {exc!s:.150}")
        return 0, None, errors

    if not records:
        return 0, None, errors

    out_path = write_records(
        records,
        gap_id=gap_id,
        source_id=SRC_HATHI,
        query=query,
        pull_root=pull_root,
    )
    return len(records), out_path, errors


# ---------------------------------------------------------------------------
# ProQuest shim — same wrapping as HathiTrust.
# ---------------------------------------------------------------------------

# Mirror of scripts.pull_proquest_newspapers.JHU_EZPROXY_PROQUEST so we
# don't add an import-cycle risk if the script's constants change.
_JHU_EZPROXY = {
    SRC_PQ_US:    "https://databases.library.jhu.edu/databases/proxy/JHU06250",
    SRC_PQ_INTL:  "https://databases.library.jhu.edu/databases/proxy/JHU07220",
}


def _pull_proquest(
    *,
    page: Any,
    query: str,
    gap_id: str,
    source_id: str,
    pull_root: Path,
) -> Tuple[int, Optional[Path], List[str]]:
    errors: List[str] = []
    if page is None:
        errors.append(f"{source_id}: no browser page")
        return 0, None, errors
    if source_id not in _JHU_EZPROXY:
        errors.append(f"{source_id}: not a known ProQuest collection")
        return 0, None, errors

    try:
        from scripts.pull_proquest_newspapers import (
            search_proquest, write_records,
        )
    except Exception as exc:
        errors.append(f"proquest import: {exc!s:.150}")
        return 0, None, errors

    proxy_url = _JHU_EZPROXY[source_id]
    time.sleep(random.uniform(2.0, 4.0))
    try:
        records = search_proquest(page, proxy_url, query) or []
    except Exception as exc:
        errors.append(f"{source_id} search: {exc!s:.150}")
        return 0, None, errors

    if not records:
        return 0, None, errors

    out_path = write_records(
        records,
        gap_id=gap_id,
        source_id=source_id,
        query=query,
        pull_root=pull_root,
    )
    return len(records), out_path, errors


# ---------------------------------------------------------------------------
# Internet Archive shim — uses scripts.pull_internet_archive helpers.
# ---------------------------------------------------------------------------


def _pull_internet_archive(
    *,
    query: str,
    gap_id: str,
    pull_root: Path,
    limit: int = 50,
) -> Tuple[int, Optional[Path], List[str]]:
    """Search IA and write seed JSON. Mirrors the HathiTrust shim pattern."""
    errors: List[str] = []
    try:
        from scripts.pull_internet_archive import write_records
        from adapters.internet_archive import search as ia_search
    except Exception as exc:
        errors.append(f"internet_archive import: {exc!s:.150}")
        return 0, None, errors

    try:
        records = ia_search(query, limit=limit) or []
    except Exception as exc:
        errors.append(f"internet_archive search: {exc!s:.150}")
        return 0, None, errors

    if not records:
        return 0, None, errors

    out_path = write_records(
        records,
        gap_id=gap_id,
        query=query,
        pull_root=pull_root,
    )
    return len(records), out_path, errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def pull_gap(
    conn: sqlite3.Connection,
    node: Dict[str, Any],
    *,
    run_id: str,
    llm: Any,
    pull_root: Path,
    page: Any = None,
    sec_user_agent: str = sec_edgar.DEFAULT_USER_AGENT,
    log: Any = None,
    sources_filter: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Pull every (query, source) plan for *node* into *pull_root*.

    *node* must carry: gap_id, gap_type, tier, claim_text. *page* is an
    open Playwright page (or None when only EBSCO + SEC sources will run).
    *log* is an optional callable for verbose progress (defaults to print).
    *sources_filter* restricts which source_ids are dispatched (Phase 4
    ``--sources`` CLI flag); None means all sources.

    Resume semantics: skips entirely if the node's status is already
    'pulled'. Sets status to 'pulled' on full success, 'queries_built'
    when at least one source errored.
    """
    say = log if callable(log) else print

    gap_id = node["gap_id"]
    status = (node.get("status") or "pending")

    if status == "pulled":
        return {"records_pulled": 0, "sources_used": [], "queries_run": 0,
                "errors": [], "skipped": True, "reason": "already_pulled"}

    plans = plan_queries(dict(node), llm)  # dict() copes with sqlite Row
    if not plans:
        return {"records_pulled": 0, "sources_used": [], "queries_run": 0,
                "errors": [], "skipped": True, "reason": "no_plan"}

    # Run pull dir prep
    pull_root = Path(pull_root)
    pull_root.mkdir(parents=True, exist_ok=True)

    records_pulled = 0
    sources_used: List[str] = []
    errors: List[str] = []

    for query, source in plans:
        # Phase 4: honour --sources filter if provided.
        if sources_filter and source not in sources_filter:
            say(f"  [{gap_id}] skip {source} (not in sources_filter)")
            continue
        say(f"  [{gap_id}] → {source}: {query[:80]}")
        if source == SRC_SEC_10K:
            n, _, errs = _pull_sec_edgar(
                entity=query,  # planner passes the entity name as query
                gap_id=gap_id,
                pull_root=pull_root,
                user_agent=sec_user_agent,
            )
        elif source == SRC_EBSCO:
            n, _, errs = _pull_ebsco(
                query=query,
                gap_id=gap_id,
                claim_text=node.get("claim_text") or "",
                pull_root=pull_root,
            )
        elif source == SRC_HATHI:
            n, _, errs = _pull_hathitrust(
                page=page, query=query, gap_id=gap_id, pull_root=pull_root,
            )
        elif source in (SRC_PQ_US, SRC_PQ_INTL):
            n, _, errs = _pull_proquest(
                page=page, query=query, gap_id=gap_id,
                source_id=source, pull_root=pull_root,
            )
        elif source == SRC_IA:
            n, _, errs = _pull_internet_archive(
                query=query, gap_id=gap_id, pull_root=pull_root,
            )
        else:
            n, errs = 0, [f"unknown_source: {source}"]

        if n:
            records_pulled += n
            if source not in sources_used:
                sources_used.append(source)
            say(f"    ↳ {n} records")
        if errs:
            errors.extend(errs)
            say(f"    ↳ errors: {errs}")

    final_status = "pulled" if not errors else "queries_built"
    _set_status(conn, gap_id, final_status)

    return {
        "records_pulled": records_pulled,
        "sources_used":   sources_used,
        "queries_run":    len(plans),
        "errors":         errors,
        "status":         final_status,
    }


# ---------------------------------------------------------------------------
# Convenience: list pending gaps for the CLI
# ---------------------------------------------------------------------------


def fetch_pullable_nodes(
    conn: sqlite3.Connection,
    *,
    tier: Optional[int] = None,
    gap_ids: Optional[List[str]] = None,
    include_pulled: bool = False,
) -> List[sqlite3.Row]:
    """Return gap_tree rows that are pullable (gap_type ≠ editorial_todo)
    and optionally filter by tier and explicit gap_ids."""
    rows: List[sqlite3.Row] = []
    if gap_ids:
        # Don't filter by tier when caller supplied an explicit list.
        for gid in gap_ids:
            cur = conn.execute(
                "SELECT * FROM gap_tree WHERE gap_id = ?", (gid,),
            ).fetchall()
            rows.extend(cur)
    else:
        rows = list_nodes(conn, tier=tier)

    out: List[sqlite3.Row] = []
    for r in rows:
        if (r["gap_type"] or "") == "editorial_todo":
            continue
        if not include_pulled and (r["status"] or "") == "pulled":
            continue
        out.append(r)
    return out
