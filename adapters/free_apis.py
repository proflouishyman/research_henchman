"""Free API adapters for orchestrator pulls.

Adding a source:
1. subclass `FreeApiAdapter`
2. implement `pull()`
3. register in `app/layers/pull.py` SOURCE_REGISTRY
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from .base import PullAdapter
from .io_utils import write_json_records
from contracts import PlannedGap, SourceAvailability, SourceResult, SourceType


class FreeApiAdapter(PullAdapter):
    """Base implementation for APIs that require no credentials."""

    source_type = SourceType.FREE_API

    def is_available(self, availability: SourceAvailability) -> bool:
        return self.source_id in availability.free_apis


class WorldBankAdapter(FreeApiAdapter):
    """World Bank indicator search endpoint."""

    source_id = "world_bank"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            url = (
                "https://api.worldbank.org/v2/indicator"
                f"?format=json&per_page=12&q={urllib.parse.quote(query)}"
            )
            with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
            records = payload[1] if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list) else []
            root = write_json_records(records, run_dir, gap.gap_id, self.source_id, query)
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=len(records),
                run_dir=root,
                artifact_type="json_records",
                status="completed",
                stats={"records": len(records), "endpoint": "world_bank_indicator_search"},
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=0,
                run_dir=str(Path(run_dir) / gap.gap_id / self.source_id),
                artifact_type="json_records",
                status="failed",
                error=str(exc)[:200],
            )


class FredAdapter(FreeApiAdapter):
    """FRED public search endpoint."""

    source_id = "fred"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            url = (
                "https://api.stlouisfed.org/fred/series/search"
                f"?search_text={urllib.parse.quote(query)}&file_type=json"
            )
            with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
            rows = payload.get("seriess", []) if isinstance(payload, dict) else []
            root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
            status = "completed" if rows else "partial"
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=len(rows),
                run_dir=root,
                artifact_type="json_records",
                status=status,
                stats={"records": len(rows), "endpoint": "fred_series_search"},
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=0,
                run_dir=str(Path(run_dir) / gap.gap_id / self.source_id),
                artifact_type="json_records",
                status="failed",
                error=str(exc)[:200],
            )


class IlostatAdapter(FreeApiAdapter):
    """ILO SDMX dataflow lookup as lightweight retrieval signal."""

    source_id = "ilostat"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            # ILO endpoint does not directly expose free-text search in a stable shape,
            # so this adapter records current dataflow snapshot for manual query mapping.
            url = "https://api.ilostat.org/sdmx/rest/dataflow/ILO"
            with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
            rows = [{"query": query, "payload_excerpt": text[:3000]}]
            root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=1,
                run_dir=root,
                artifact_type="json_records",
                status="partial",
                stats={"records": 1, "note": "dataflow snapshot captured"},
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=0,
                run_dir=str(Path(run_dir) / gap.gap_id / self.source_id),
                artifact_type="json_records",
                status="failed",
                error=str(exc)[:200],
            )


class OecdAdapter(FreeApiAdapter):
    """OECD dataflow lookup adapter."""

    source_id = "oecd"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            url = "https://sdmx.oecd.org/public/rest/dataflow/OECD.SDD.STES,DSD_STES@DF_FINMARK"
            with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
            rows = [{"query": query, "payload_excerpt": text[:3000]}]
            root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=1,
                run_dir=root,
                artifact_type="json_records",
                status="partial",
                stats={"records": 1, "note": "dataflow snapshot captured"},
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=0,
                run_dir=str(Path(run_dir) / gap.gap_id / self.source_id),
                artifact_type="json_records",
                status="failed",
                error=str(exc)[:200],
            )
