"""Layer 3: source routing and pull execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Dict, List

from ..adapters.base import PullAdapter
from ..adapters.free_apis import FredAdapter, IlostatAdapter, OecdAdapter, WorldBankAdapter
from ..adapters.keyed_apis import BeaAdapter, BlsAdapter, CensusAdapter, EbscoApiAdapter
from ..adapters.playwright_adapters import (
    AmericasHistoricalNewsPlaywrightAdapter,
    EbscohostPlaywrightAdapter,
    GalePrimarySourcesPlaywrightAdapter,
    JstorPlaywrightAdapter,
    ProjectMusePlaywrightAdapter,
    ProquestHistoricalNewsPlaywrightAdapter,
    StatistaPlaywrightAdapter,
    check_cdp_endpoint,
)
from ..config import OrchestratorSettings
from ..contracts import ClaimKind, EvidenceNeed, GapPullResult, PlannedGap, ResearchPlan, SourceAvailability, SourceResult, SourceType


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
    "jstor": JstorPlaywrightAdapter(),
    "project_muse": ProjectMusePlaywrightAdapter(),
    "proquest_historical_newspapers": ProquestHistoricalNewsPlaywrightAdapter(),
    "americas_historical_newspapers": AmericasHistoricalNewsPlaywrightAdapter(),
    "gale_primary_sources": GalePrimarySourcesPlaywrightAdapter(),
    "statista": StatistaPlaywrightAdapter(),
}

# Capabilities are the semantic routing contract:
# each source declares what claim kinds and evidence needs it can satisfy.
SOURCE_CAPABILITIES: Dict[str, Dict[str, object]] = {
    "world_bank": {
        "claim_kinds": [ClaimKind.QUANTITATIVE_MACRO],
        "evidence_needs": [EvidenceNeed.OFFICIAL_STATISTICS],
        "notes": "macro development indicators",
    },
    "fred": {
        "claim_kinds": [ClaimKind.QUANTITATIVE_MACRO, ClaimKind.QUANTITATIVE_LABOR],
        "evidence_needs": [EvidenceNeed.OFFICIAL_STATISTICS],
        "notes": "time-series macro and labor indicators",
    },
    "ilostat": {
        "claim_kinds": [ClaimKind.QUANTITATIVE_LABOR],
        "evidence_needs": [EvidenceNeed.OFFICIAL_STATISTICS],
        "notes": "labor market statistical series",
    },
    "oecd": {
        "claim_kinds": [ClaimKind.QUANTITATIVE_MACRO, ClaimKind.QUANTITATIVE_LABOR],
        "evidence_needs": [EvidenceNeed.OFFICIAL_STATISTICS],
        "notes": "cross-national statistical datasets",
    },
    "bls": {
        "claim_kinds": [ClaimKind.QUANTITATIVE_LABOR],
        "evidence_needs": [EvidenceNeed.OFFICIAL_STATISTICS],
        "notes": "US labor statistics",
    },
    "bea": {
        "claim_kinds": [ClaimKind.QUANTITATIVE_MACRO],
        "evidence_needs": [EvidenceNeed.OFFICIAL_STATISTICS],
        "notes": "US macroeconomic statistics",
    },
    "census": {
        "claim_kinds": [ClaimKind.QUANTITATIVE_MACRO, ClaimKind.QUANTITATIVE_LABOR],
        "evidence_needs": [EvidenceNeed.OFFICIAL_STATISTICS],
        "notes": "US survey and time-series statistics",
    },
    "ebsco_api": {
        "claim_kinds": [
            ClaimKind.HISTORICAL_NARRATIVE,
            ClaimKind.LEGAL_REGULATORY,
            ClaimKind.COMPANY_OPERATIONS,
            ClaimKind.BIOGRAPHICAL,
            ClaimKind.OTHER,
        ],
        "evidence_needs": [
            EvidenceNeed.SCHOLARLY_SECONDARY,
            EvidenceNeed.PRIMARY_SOURCE,
            EvidenceNeed.NEWS_ARCHIVE,
            EvidenceNeed.LEGAL_TEXT,
            EvidenceNeed.MIXED,
        ],
        "notes": "scholarly and archival discovery via keyed API",
    },
    "ebscohost": {
        "claim_kinds": [
            ClaimKind.HISTORICAL_NARRATIVE,
            ClaimKind.LEGAL_REGULATORY,
            ClaimKind.COMPANY_OPERATIONS,
            ClaimKind.BIOGRAPHICAL,
            ClaimKind.OTHER,
        ],
        "evidence_needs": [
            EvidenceNeed.SCHOLARLY_SECONDARY,
            EvidenceNeed.PRIMARY_SOURCE,
            EvidenceNeed.NEWS_ARCHIVE,
            EvidenceNeed.LEGAL_TEXT,
            EvidenceNeed.MIXED,
        ],
        "notes": "authenticated university database access",
    },
    "jstor": {
        "claim_kinds": [
            ClaimKind.HISTORICAL_NARRATIVE,
            ClaimKind.COMPANY_OPERATIONS,
            ClaimKind.BIOGRAPHICAL,
            ClaimKind.LEGAL_REGULATORY,
            ClaimKind.OTHER,
        ],
        "evidence_needs": [
            EvidenceNeed.SCHOLARLY_SECONDARY,
            EvidenceNeed.PRIMARY_SOURCE,
            EvidenceNeed.NEWS_ARCHIVE,
            EvidenceNeed.MIXED,
        ],
        "notes": "JHU humanities recommendation for peer-reviewed scholarship",
    },
    "project_muse": {
        "claim_kinds": [
            ClaimKind.HISTORICAL_NARRATIVE,
            ClaimKind.COMPANY_OPERATIONS,
            ClaimKind.BIOGRAPHICAL,
            ClaimKind.OTHER,
        ],
        "evidence_needs": [
            EvidenceNeed.SCHOLARLY_SECONDARY,
            EvidenceNeed.PRIMARY_SOURCE,
            EvidenceNeed.MIXED,
        ],
        "notes": "JHU humanities recommendation for history and cultural scholarship",
    },
    "proquest_historical_newspapers": {
        "claim_kinds": [
            ClaimKind.HISTORICAL_NARRATIVE,
            ClaimKind.COMPANY_OPERATIONS,
            ClaimKind.LEGAL_REGULATORY,
            ClaimKind.BIOGRAPHICAL,
            ClaimKind.OTHER,
        ],
        "evidence_needs": [
            EvidenceNeed.NEWS_ARCHIVE,
            EvidenceNeed.PRIMARY_SOURCE,
            EvidenceNeed.MIXED,
        ],
        "notes": "historical newspaper archives",
    },
    "americas_historical_newspapers": {
        "claim_kinds": [
            ClaimKind.HISTORICAL_NARRATIVE,
            ClaimKind.COMPANY_OPERATIONS,
            ClaimKind.BIOGRAPHICAL,
            ClaimKind.OTHER,
        ],
        "evidence_needs": [
            EvidenceNeed.NEWS_ARCHIVE,
            EvidenceNeed.PRIMARY_SOURCE,
            EvidenceNeed.MIXED,
        ],
        "notes": "early American newspaper and periodical archives",
    },
    "gale_primary_sources": {
        "claim_kinds": [
            ClaimKind.HISTORICAL_NARRATIVE,
            ClaimKind.LEGAL_REGULATORY,
            ClaimKind.COMPANY_OPERATIONS,
            ClaimKind.BIOGRAPHICAL,
            ClaimKind.OTHER,
        ],
        "evidence_needs": [
            EvidenceNeed.PRIMARY_SOURCE,
            EvidenceNeed.NEWS_ARCHIVE,
            EvidenceNeed.LEGAL_TEXT,
            EvidenceNeed.MIXED,
        ],
        "notes": "digitized primary-source collections and periodicals",
    },
    "statista": {
        "claim_kinds": [ClaimKind.QUANTITATIVE_MACRO, ClaimKind.COMPANY_OPERATIONS],
        "evidence_needs": [EvidenceNeed.OFFICIAL_STATISTICS, EvidenceNeed.NEWS_ARCHIVE],
        "notes": "commercial compiled statistics",
    },
}


def source_capability_catalog() -> Dict[str, Dict[str, object]]:
    """Return JSON-safe capability rows for API/LLM consumers."""

    catalog: Dict[str, Dict[str, object]] = {}
    for source_id, row in SOURCE_CAPABILITIES.items():
        claim_kinds = [str(kind.value) for kind in row.get("claim_kinds", [])]
        evidence_needs = [str(kind.value) for kind in row.get("evidence_needs", [])]
        catalog[source_id] = {
            "claim_kinds": claim_kinds,
            "evidence_needs": evidence_needs,
            "notes": str(row.get("notes", "")),
        }
    return catalog


def rank_sources_for_claim(
    claim_kind: ClaimKind,
    evidence_need: EvidenceNeed,
    availability: SourceAvailability,
    source_types: List[SourceType] | None = None,
    max_sources: int = 4,
) -> List[str]:
    """Rank currently-available sources by semantic fit for one claim."""

    available_by_type: Dict[SourceType, List[str]] = {
        SourceType.FREE_API: list(availability.free_apis),
        SourceType.KEYED_API: list(availability.keyed_apis),
        SourceType.PLAYWRIGHT: list(availability.playwright_sources),
    }
    type_allow = set(source_types or [SourceType.FREE_API, SourceType.KEYED_API, SourceType.PLAYWRIGHT])

    candidates: List[str] = []
    for src_type, ids in available_by_type.items():
        if src_type in type_allow:
            candidates.extend(ids)

    scored: List[tuple[float, str]] = []
    for source_id in candidates:
        adapter = SOURCE_REGISTRY.get(source_id)
        if adapter is None:
            continue
        caps = SOURCE_CAPABILITIES.get(source_id, {})
        cap_claims: List[ClaimKind] = list(caps.get("claim_kinds", []))  # type: ignore[arg-type]
        cap_evidence: List[EvidenceNeed] = list(caps.get("evidence_needs", []))  # type: ignore[arg-type]

        score = 0.0
        if claim_kind in cap_claims:
            score += 3.0
        elif claim_kind == ClaimKind.OTHER:
            score += 1.0
        else:
            score -= 1.5

        if evidence_need in cap_evidence:
            score += 3.0
        elif evidence_need == EvidenceNeed.MIXED:
            score += 1.0
        else:
            score -= 1.5

        if adapter.source_type == SourceType.PLAYWRIGHT and evidence_need in {
            EvidenceNeed.SCHOLARLY_SECONDARY,
            EvidenceNeed.PRIMARY_SOURCE,
            EvidenceNeed.NEWS_ARCHIVE,
            EvidenceNeed.LEGAL_TEXT,
        }:
            score += 1.0

        if adapter.source_type == SourceType.FREE_API and evidence_need in {
            EvidenceNeed.SCHOLARLY_SECONDARY,
            EvidenceNeed.PRIMARY_SOURCE,
            EvidenceNeed.NEWS_ARCHIVE,
            EvidenceNeed.LEGAL_TEXT,
        }:
            score -= 2.0

        scored.append((score, source_id))

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    preferred = [source_id for score, source_id in scored if score >= 1.5]
    if preferred:
        return preferred[:max_sources]
    return []


def source_fit_reason(claim_kind: ClaimKind, evidence_need: EvidenceNeed, preferred_sources: List[str]) -> str:
    """Generate concise explanation shown in plan cards for source routing."""

    if not preferred_sources:
        return f"No strong source match for {claim_kind.value}/{evidence_need.value}."
    return (
        f"Routed {claim_kind.value} claim needing {evidence_need.value} evidence to "
        f"{', '.join(preferred_sources)}."
    )


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
        results.append(
            _pull_gap(
                planned_gap,
                plan.source_availability,
                settings,
                run_id,
                emit_event=emit_event,
            )
        )

    return results


def _source_ids_for_gap(gap: PlannedGap, availability: SourceAvailability) -> List[str]:
    """Resolve source IDs for a planned gap using preferred list then type routing."""

    if gap.preferred_sources:
        return gap.preferred_sources

    ranked = rank_sources_for_claim(gap.claim_kind, gap.evidence_need, availability, source_types=gap.source_types)
    if ranked:
        return ranked

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
    emit_event: Callable[..., None] | None = None,
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
            if emit_event:
                emit_event(
                    stage="pulling",
                    status="progress",
                    message=f"[{gap.gap_id}] querying {source_id}: {query[:160]}",
                    meta={"gap_id": gap.gap_id, "source_id": source_id, "query": query},
                )
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
            if emit_event:
                emit_event(
                    stage="pulling",
                    status="progress",
                    message=f"[{gap.gap_id}] {source_id} -> {result.status} ({result.document_count} docs)",
                    meta={
                        "gap_id": gap.gap_id,
                        "source_id": source_id,
                        "query": query,
                        "result_status": result.status,
                        "document_count": result.document_count,
                        "artifact_type": result.artifact_type,
                    },
                )

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
