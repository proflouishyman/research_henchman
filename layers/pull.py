"""Layer 3: source routing and pull execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Dict, List

from ..adapters.base import PullAdapter
from ..adapters.free_apis import FredAdapter, IlostatAdapter, OecdAdapter, WorldBankAdapter
from ..adapters.keyed_apis import BeaAdapter, BlsAdapter, CensusAdapter, EbscoApiAdapter
from ..adapters.playwright_adapters import EbscohostPlaywrightAdapter, StatistaPlaywrightAdapter, check_cdp_endpoint
from ..config import OrchestratorSettings
from ..contracts import GapPullResult, PlannedGap, ResearchPlan, SourceAvailability, SourceResult, SourceType


SOURCE_REGISTRY: Dict[str, PullAdapter] = {
    "world_bank": WorldBankAdapter(),
    "fred": FredAdapter(),
    "ilostat": IlostatAdapter(),
    "oecd": OecdAdapter(),
    "bls": BlsAdapter(),
    "bea": BeaAdapter(),
    "census": CensusAdapter(),
    "ebsco_api": EbscoApiAdapter(),
    "ebscohost": EbscohostPlaywrightAdapter(),
    "statista": StatistaPlaywrightAdapter(),
}


def build_source_availability(settings: OrchestratorSettings) -> SourceAvailability:
    """Build one availability snapshot before pull planning/execution."""

    free_apis: List[str] = []
    keyed_apis: List[str] = []
    missing_keys: Dict[str, str] = {}

    for source_id, adapter in SOURCE_REGISTRY.items():
        if adapter.source_type == SourceType.FREE_API:
            free_apis.append(source_id)
            continue

        if adapter.source_type == SourceType.KEYED_API:
            env_key = str(getattr(adapter, "env_key", "")).strip()
            if env_key and os.environ.get(env_key, "").strip():
                keyed_apis.append(source_id)
            else:
                missing_keys[source_id] = env_key

    playwright_sources: List[str] = []
    playwright_unavailable_reason = ""
    cdp_error = check_cdp_endpoint(settings.playwright_cdp_url)
    if not cdp_error:
        playwright_sources = [
            source_id
            for source_id, adapter in SOURCE_REGISTRY.items()
            if adapter.source_type == SourceType.PLAYWRIGHT
        ]
    else:
        playwright_unavailable_reason = (
            f"CDP connection failed at {settings.playwright_cdp_url}: {cdp_error}. "
            "Re-authenticate browser session in Settings."
        )

    return SourceAvailability(
        free_apis=sorted(free_apis),
        keyed_apis=sorted(keyed_apis),
        playwright_sources=sorted(playwright_sources),
        missing_keys=missing_keys,
        playwright_unavailable_reason=playwright_unavailable_reason,
    )


def pull_for_plan(
    plan: ResearchPlan,
    settings: OrchestratorSettings,
    emit_event: Callable[..., None],
    run_id: str,
) -> List[GapPullResult]:
    """Execute pulls for all non-skipped gaps and aggregate typed results."""

    results: List[GapPullResult] = []
    active_gaps = [gap for gap in plan.gaps if not gap.skip]

    for index, planned_gap in enumerate(active_gaps, start=1):
        emit_event(
            stage="pulling",
            status="progress",
            message=f"Pulling gap {index}/{len(active_gaps)}: {planned_gap.gap_id}",
            meta={"gap_id": planned_gap.gap_id, "queries": planned_gap.search_queries},
        )
        results.append(_pull_gap(planned_gap, plan.source_availability, settings, run_id))

    return results


def _source_ids_for_gap(gap: PlannedGap, availability: SourceAvailability) -> List[str]:
    """Resolve source IDs for a planned gap using preferred list then type routing."""

    if gap.preferred_sources:
        return gap.preferred_sources

    out: List[str] = []
    for source_type in gap.source_types:
        if source_type == SourceType.FREE_API:
            out.extend(availability.free_apis)
        elif source_type == SourceType.KEYED_API:
            out.extend(availability.keyed_apis)
        elif source_type == SourceType.PLAYWRIGHT:
            out.extend(availability.playwright_sources)

    seen = set()
    unique: List[str] = []
    for source_id in out:
        if source_id in seen:
            continue
        seen.add(source_id)
        unique.append(source_id)
    return unique


def _pull_gap(
    gap: PlannedGap,
    availability: SourceAvailability,
    settings: OrchestratorSettings,
    run_id: str,
) -> GapPullResult:
    """Pull all queries for one gap across selected adapters."""

    run_dir = str(Path(settings.pull_output_root) / run_id)
    source_results: List[SourceResult] = []
    attempted: List[str] = []
    succeeded: List[str] = []
    failed: List[str] = []

    source_ids = _source_ids_for_gap(gap, availability)

    for source_id in source_ids:
        adapter = SOURCE_REGISTRY.get(source_id)
        if adapter is None or not adapter.is_available(availability):
            continue

        for query in (gap.search_queries or [gap.claim_text[:120]]):
            attempted.append(source_id)
            result = adapter.pull(
                gap=gap,
                query=query,
                run_dir=run_dir,
                timeout_seconds=settings.pull_timeout_seconds,
            )
            source_results.append(result)
            if result.status in {"completed", "partial"}:
                succeeded.append(source_id)
            else:
                failed.append(source_id)

    total_docs = sum(result.document_count for result in source_results)
    status = "unresolvable" if total_docs == 0 else ("partial" if failed else "completed")

    return GapPullResult(
        gap_id=gap.gap_id,
        planned_gap=gap,
        results=source_results,
        total_documents=total_docs,
        sources_attempted=sorted(set(attempted)),
        sources_succeeded=sorted(set(succeeded)),
        sources_failed=sorted(set(failed)),
        status=status,
    )
