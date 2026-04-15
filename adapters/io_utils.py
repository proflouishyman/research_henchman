"""Shared adapter artifact writing helpers."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def era_years_from_gap(gap: Any) -> Tuple[Optional[int], Optional[int]]:
    """Extract (era_start, era_end) from a PlannedGap's query_ladder synonym ring.

    Returns (None, None) when the gap has no ladder or the ring has no era bounds.
    Adapters use these values to apply date-range facets to provider search URLs
    and API calls without changing the query string itself.
    """

    ladder_dict: Dict[str, Any] = getattr(gap, "query_ladder", {}) or {}
    ring: Dict[str, Any] = ladder_dict.get("synonym_ring", {}) or {}
    era_start = ring.get("era_start")
    era_end = ring.get("era_end")
    try:
        start = int(era_start) if era_start is not None else None
    except (TypeError, ValueError):
        start = None
    try:
        end = int(era_end) if era_end is not None else None
    except (TypeError, ValueError):
        end = None
    return (start, end)


def safe_query_token(query: str) -> str:
    """Create filesystem-safe token from query text."""

    token = re.sub(r"[^A-Za-z0-9._-]+", "_", query.strip())[:60]
    return token or "query"


def write_json_records(records: List[Dict[str, Any]], run_dir: str, gap_id: str, source_id: str, query: str) -> str:
    """Write JSON records and return adapter artifact directory path."""

    root = Path(run_dir) / gap_id / source_id
    root.mkdir(parents=True, exist_ok=True)
    out_path = root / f"{safe_query_token(query)}.json"
    out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(root)


def write_csv_rows(rows: List[Dict[str, Any]], run_dir: str, gap_id: str, source_id: str, query: str) -> str:
    """Write CSV rows and return adapter artifact directory path."""

    root = Path(run_dir) / gap_id / source_id
    root.mkdir(parents=True, exist_ok=True)
    out_path = root / f"{safe_query_token(query)}.csv"
    fieldnames = sorted({key for row in rows for key in row.keys()}) or ["value"]
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return str(root)
