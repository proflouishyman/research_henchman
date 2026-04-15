"""Playwright-backed source adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from .base import PullAdapter
from .cdp_utils import effective_cdp_url
from .document_links import build_link_rows
from .io_utils import era_years_from_gap, write_json_records
from .seed_url_fetch import blocked_reason_hint, resolve_seed_rows
from contracts import PlannedGap, SourceAvailability, SourceResult, SourceType


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

    def _link_seed_result(self, gap: PlannedGap, query: str, run_dir: str, note: str) -> SourceResult:
        """Emit actionable click-through links for browser-backed sources.

        This preserves user momentum until source-specific browser scraping is
        fully implemented by returning provider search URLs plus local corpus
        matches when available.
        """

        try:
            era_start, era_end = era_years_from_gap(gap)
            rows = build_link_rows(self.source_id, query, gap.gap_id, limit_local=4, era_start=era_start, era_end=era_end)
            source_root = Path(run_dir) / gap.gap_id / self.source_id
            source_root.mkdir(parents=True, exist_ok=True)
            resolved_rows, resolved_stats = resolve_seed_rows(
                rows=rows,
                source_root=source_root,
                source_id=self.source_id,
                query=query,
                gap_id=gap.gap_id,
            )
            rows.extend(resolved_rows)
            blocked_files = int(resolved_stats.get("blocked_files", 0))
            captcha_blocks = int(resolved_stats.get("captcha_blocks", 0))
            login_blocks = int(resolved_stats.get("login_blocks", 0))
            challenge_blocks = int(resolved_stats.get("challenge_blocks", 0))
            for row in rows:
                row_note = note
                blocked_reason = str(row.get("blocked_reason", "")).strip().lower()
                if blocked_reason:
                    hint = blocked_reason_hint(blocked_reason)
                    row_note = f"{row_note} User action required: {hint}" if hint else row_note
                row["note"] = row_note
                row["source_id"] = self.source_id
            root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
            pulled_docs = sum(
                1
                for row in rows
                if (
                    str(row.get("quality_label", "")).lower() in {"high", "medium"}
                    and not str(row.get("blocked_reason", "")).strip()
                )
            )
            status = "completed" if pulled_docs > 0 else ("partial" if rows else "failed")
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=len(rows),
                run_dir=root,
                artifact_type="json_records",
                status=status,
                stats={
                    "records": len(rows),
                    "pulled_docs": pulled_docs,
                    "seed_only": pulled_docs <= 0,
                    "resolved_files": int(resolved_stats.get("resolved_files", 0)),
                    "blocked_files": blocked_files,
                    "captcha_blocks": captcha_blocks,
                    "login_blocks": login_blocks,
                    "challenge_blocks": challenge_blocks,
                    "action_required": blocked_files > 0,
                    "link_mode": "provider_search+local_corpus+resolved_fetch",
                },
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


class EbscohostPlaywrightAdapter(PlaywrightAdapter):
    """EBSCOhost browser adapter placeholder implementation."""

    source_id = "ebscohost"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        return self._link_seed_result(
            gap,
            query,
            run_dir,
            note="Playwright execution delegated to authenticated EBSCOhost workflow; seeded click-through links provided.",
        )


class StatistaPlaywrightAdapter(PlaywrightAdapter):
    """Statista browser adapter placeholder implementation."""

    source_id = "statista"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        return self._link_seed_result(
            gap,
            query,
            run_dir,
            note="Statista Playwright retrieval pending source-specific workflow; seeded click-through links provided.",
        )


class JstorPlaywrightAdapter(PlaywrightAdapter):
    """JSTOR browser adapter placeholder implementation."""

    source_id = "jstor"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        return self._link_seed_result(
            gap,
            query,
            run_dir,
            note="JSTOR Playwright retrieval pending source-specific workflow; seeded click-through links provided.",
        )


class ProjectMusePlaywrightAdapter(PlaywrightAdapter):
    """Project MUSE browser adapter placeholder implementation."""

    source_id = "project_muse"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        return self._link_seed_result(
            gap,
            query,
            run_dir,
            note="Project MUSE Playwright retrieval pending source-specific workflow; seeded click-through links provided.",
        )


class ProquestHistoricalNewsPlaywrightAdapter(PlaywrightAdapter):
    """ProQuest Historical Newspapers browser adapter placeholder implementation."""

    source_id = "proquest_historical_newspapers"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        return self._link_seed_result(
            gap,
            query,
            run_dir,
            note="ProQuest Historical Newspapers retrieval pending source-specific workflow; seeded click-through links provided.",
        )


class AmericasHistoricalNewsPlaywrightAdapter(PlaywrightAdapter):
    """America's Historical Newspapers browser adapter placeholder implementation."""

    source_id = "americas_historical_newspapers"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        return self._link_seed_result(
            gap,
            query,
            run_dir,
            note="America's Historical Newspapers retrieval pending source-specific workflow; seeded click-through links provided.",
        )


class GalePrimarySourcesPlaywrightAdapter(PlaywrightAdapter):
    """Gale Primary Sources browser adapter placeholder implementation."""

    source_id = "gale_primary_sources"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        return self._link_seed_result(
            gap,
            query,
            run_dir,
            note="Gale Primary Sources retrieval pending source-specific workflow; seeded click-through links provided.",
        )


def check_cdp_endpoint(cdp_url: str, timeout_seconds: int = 5) -> str:
    """Return empty string when CDP endpoint is reachable, else error reason."""

    probe_url = f"{effective_cdp_url(cdp_url).rstrip('/')}/json"
    try:
        with urllib.request.urlopen(probe_url, timeout=max(1, timeout_seconds)) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if isinstance(payload, list):
            return ""
        return "unexpected CDP response payload"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore").strip()
        if detail:
            return f"HTTP {exc.code}: {detail[:120]}"
        return f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return str(exc.reason)[:160]
    except Exception as exc:  # noqa: BLE001 - return reason string instead of raising.
        return str(exc)[:160]
