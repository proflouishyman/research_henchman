"""Playwright-backed source adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from .base import PullAdapter
from .io_utils import write_json_records
from ..contracts import PlannedGap, SourceAvailability, SourceResult, SourceType


class PlaywrightAdapter(PullAdapter):
    """Base class for browser-session adapters."""

    source_type = SourceType.PLAYWRIGHT

    def is_available(self, availability: SourceAvailability) -> bool:
        return self.source_id in availability.playwright_sources

    def validate(self, availability: SourceAvailability) -> str:
        if availability.playwright_unavailable_reason:
            return f"Browser session unavailable: {availability.playwright_unavailable_reason}"
        if self.source_id not in availability.playwright_sources:
            return f"{self.source_id}: not in active browser session list"
        return ""


class EbscohostPlaywrightAdapter(PlaywrightAdapter):
    """EBSCOhost browser adapter placeholder implementation."""

    source_id = "ebscohost"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        try:
            rows = [
                {
                    "query": query,
                    "note": "Playwright execution delegated to authenticated browser workflow",
                    "source_id": self.source_id,
                    "gap_id": gap.gap_id,
                }
            ]
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
                stats={"placeholder": True, "records": 1},
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


class StatistaPlaywrightAdapter(PlaywrightAdapter):
    """Statista browser adapter placeholder implementation."""

    source_id = "statista"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        try:
            rows = [
                {
                    "query": query,
                    "note": "Statista Playwright retrieval pending source-specific ticket",
                    "source_id": self.source_id,
                    "gap_id": gap.gap_id,
                }
            ]
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
                stats={"placeholder": True, "records": 1},
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


def check_cdp_endpoint(cdp_url: str, timeout_seconds: int = 5) -> str:
    """Return empty string when CDP endpoint is reachable, else error reason."""

    probe_url = f"{cdp_url.rstrip('/')}/json"
    try:
        with urllib.request.urlopen(probe_url, timeout=max(1, timeout_seconds)) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if isinstance(payload, list):
            return ""
        return "unexpected CDP response payload"
    except urllib.error.URLError as exc:
        return str(exc.reason)[:160]
    except Exception as exc:  # noqa: BLE001 - return reason string instead of raising.
        return str(exc)[:160]
