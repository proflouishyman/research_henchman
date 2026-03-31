"""Shared adapter artifact writing helpers."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List


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
