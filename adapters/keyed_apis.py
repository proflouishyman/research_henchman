"""Keyed API adapters for orchestrator pulls."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from .base import PullAdapter
from .document_links import build_link_rows
from .io_utils import write_json_records
from .seed_url_fetch import blocked_reason_hint, resolve_seed_rows
from contracts import PlannedGap, SourceAvailability, SourceResult, SourceType


class KeyedApiAdapter(PullAdapter):
    """Base class for APIs that require env-provided credentials."""

    source_type = SourceType.KEYED_API
    env_key: str = ""
    env_aliases: List[str] = []
    # Optional OR-of-AND groups for credential shape support.
    # Example: [["API_KEY"], ["USERNAME", "PASSWORD"]]
    credential_sets: List[List[str]] = []

    def is_available(self, availability: SourceAvailability) -> bool:
        return self.source_id in availability.keyed_apis

    def validate(self, availability: SourceAvailability) -> str:
        if self.source_id not in availability.keyed_apis:
            missing = availability.missing_keys.get(self.source_id, self.env_key)
            return f"{self.source_id}: missing env key {missing}"
        return ""

    def credential_hint(self) -> str:
        """Return human-readable credential requirement string."""

        if self.credential_sets:
            groups = ["+".join(group) for group in self.credential_sets if group]
            return " OR ".join(groups)
        keys = [self.env_key, *self.env_aliases]
        keys = [key for key in keys if key]
        return " | ".join(keys) if keys else self.env_key

    def has_credentials(self) -> bool:
        """Check whether this adapter has any valid credential form."""

        if self.credential_sets:
            for group in self.credential_sets:
                if group and all(os.environ.get(key, "").strip() for key in group):
                    return True
            return False

        for key in [self.env_key, *self.env_aliases]:
            if key and os.environ.get(key, "").strip():
                return True
        return False

    @property
    def api_key(self) -> str:
        for key in [self.env_key, *self.env_aliases]:
            val = os.environ.get(key, "").strip()
            if val:
                return val
        return ""


class BlsAdapter(KeyedApiAdapter):
    """BLS public data API v2."""

    source_id = "bls"
    env_key = "BLS_API_KEY"
    env_aliases = ["BLS_REGISTRATION_KEY"]

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            payload = {
                "seriesid": ["CUUR0000SA0"],
                "startyear": "2019",
                "endyear": "2024",
                "registrationkey": self.api_key,
            }
            req = urllib.request.Request(
                "https://api.bls.gov/publicAPI/v2/timeseries/data/",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="ignore"))
            rows: List[Dict[str, Any]] = []
            for series in body.get("Results", {}).get("series", []):
                for point in series.get("data", [])[:12]:
                    rows.append(point)
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
                stats={"records": len(rows), "endpoint": "bls_timeseries"},
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


class BeaAdapter(KeyedApiAdapter):
    """BEA API dataset metadata lookup."""

    source_id = "bea"
    env_key = "BEA_USER_ID"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            params = urllib.parse.urlencode(
                {
                    "UserID": self.api_key,
                    "method": "GETDATASETLIST",
                    "ResultFormat": "JSON",
                }
            )
            url = f"https://apps.bea.gov/api/data/?{params}"
            with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="ignore"))
            rows = body.get("BEAAPI", {}).get("Results", {}).get("Dataset", [])
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
                stats={"records": len(rows), "endpoint": "bea_dataset_list"},
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


class CensusAdapter(KeyedApiAdapter):
    """Census API basic variable lookup endpoint."""

    source_id = "census"
    env_key = "CENSUS_API_KEY"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            params = urllib.parse.urlencode({"key": self.api_key})
            url = f"https://api.census.gov/data/timeseries/eits/mrts/variables.json?{params}"
            with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="ignore"))
            variables = body.get("variables", {}) if isinstance(body, dict) else {}
            rows = [{"name": k, **v} for k, v in list(variables.items())[:50] if isinstance(v, dict)]
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
                stats={"records": len(rows), "endpoint": "census_mrts_variables"},
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


class EbscoApiAdapter(KeyedApiAdapter):
    """EBSCOhost API adapter.

    Current behavior emits provider click-through search URLs plus best-effort
    local corpus matches so results remain actionable while API-specific
    translation evolves.
    """

    source_id = "ebsco_api"
    env_key = "EBSCO_API_KEY"
    # EBSCO deployments vary: API key or profile credentials.
    credential_sets = [
        ["EBSCO_API_KEY"],
        ["EBSCO_PROF", "EBSCO_PWD"],
        ["EBSCO_PROFILE_ID", "EBSCO_PROFILE_PASSWORD"],
    ]

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        try:
            rows = build_link_rows(self.source_id, query, gap.gap_id, limit_local=6)
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
                blocked_reason = str(row.get("blocked_reason", "")).strip().lower()
                if not blocked_reason:
                    continue
                hint = blocked_reason_hint(blocked_reason)
                row_note = str(row.get("note", "")).strip()
                action_note = f"User action required: {hint}" if hint else "User action required."
                row["note"] = f"{row_note} {action_note}".strip()
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
