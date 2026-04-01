"""Layer 3: source routing and pull execution."""

from __future__ import annotations

import re
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
from ..library_profiles import get_active_playwright_source_ids, get_active_university_databases
from .search_policy import AccordionLadder, get_accordion_move

QUERY_SPLIT_RE = re.compile(r"\s*[|;\n]+\s*")
QUERY_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
QUERY_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "there",
    "about",
    "without",
    "where",
    "which",
    "what",
    "were",
    "have",
    "has",
    "had",
    "company",
    "merchant",
    "chapter",
}


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


def _runtime_capability_map(settings: OrchestratorSettings | None = None) -> Dict[str, Dict[str, object]]:
    """Build runtime capability map by merging static + library profile metadata."""

    capabilities: Dict[str, Dict[str, object]] = {
        source_id: {
            "claim_kinds": list(row.get("claim_kinds", [])),
            "evidence_needs": list(row.get("evidence_needs", [])),
            "notes": str(row.get("notes", "")),
        }
        for source_id, row in SOURCE_CAPABILITIES.items()
    }

    if settings is not None:
        for db in get_active_university_databases(settings):
            source_id = str(db.get("source_id", "")).strip().lower()
            if not source_id:
                continue
            if source_id not in capabilities:
                capabilities[source_id] = {"claim_kinds": [], "evidence_needs": [], "notes": ""}

            claim_rows = db.get("claim_kinds", [])
            if isinstance(claim_rows, list):
                claim_kinds: List[ClaimKind] = []
                for item in claim_rows:
                    try:
                        claim_kinds.append(ClaimKind(str(item).strip()))
                    except Exception:
                        continue
                if claim_kinds:
                    capabilities[source_id]["claim_kinds"] = claim_kinds

            evidence_rows = db.get("evidence_needs", [])
            if isinstance(evidence_rows, list):
                evidence_needs: List[EvidenceNeed] = []
                for item in evidence_rows:
                    try:
                        evidence_needs.append(EvidenceNeed(str(item).strip()))
                    except Exception:
                        continue
                if evidence_needs:
                    capabilities[source_id]["evidence_needs"] = evidence_needs

            categories = db.get("categories", [])
            if isinstance(categories, list) and categories:
                capabilities[source_id]["notes"] = (
                    f"library profile categories: {', '.join(str(cat) for cat in categories)}"
                )

    return capabilities


def source_capability_catalog(settings: OrchestratorSettings | None = None) -> Dict[str, Dict[str, object]]:
    """Return JSON-safe capability rows for API/LLM consumers."""

    catalog: Dict[str, Dict[str, object]] = {}
    for source_id, row in _runtime_capability_map(settings).items():
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
    settings: OrchestratorSettings | None = None,
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

    capabilities = _runtime_capability_map(settings)

    scored: List[tuple[float, str]] = []
    for source_id in candidates:
        adapter = SOURCE_REGISTRY.get(source_id)
        if adapter is None:
            continue
        caps = capabilities.get(source_id, {})
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
            has_credentials = bool(getattr(adapter, "has_credentials", lambda: False)())
            if has_credentials:
                keyed_apis.append(source_id)
            else:
                hint = str(getattr(adapter, "credential_hint", lambda: getattr(adapter, "env_key", ""))()).strip()
                missing_keys[source_id] = hint

    playwright_sources: List[str] = []
    playwright_unavailable_reason = ""
    cdp_error = check_cdp_endpoint(settings.playwright_cdp_url)
    if not cdp_error:
        preferred_playwright_ids = set(get_active_playwright_source_ids(settings))
        playwright_sources = [
            source_id
            for source_id, adapter in SOURCE_REGISTRY.items()
            if adapter.source_type == SourceType.PLAYWRIGHT and (not preferred_playwright_ids or source_id in preferred_playwright_ids)
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


def _source_ids_for_gap(gap: PlannedGap, availability: SourceAvailability, settings: OrchestratorSettings) -> List[str]:
    """Resolve source IDs for a planned gap using preferred list then type routing."""

    if gap.preferred_sources:
        return gap.preferred_sources

    ranked = rank_sources_for_claim(
        gap.claim_kind,
        gap.evidence_need,
        availability,
        source_types=gap.source_types,
        settings=settings,
    )
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


def _query_attempt_chain(gap: PlannedGap, query: str, max_attempts: int = 3) -> List[str]:
    """Build a progressive query chain from specific to broader fallback terms."""

    base = str(query or "").strip() or gap.claim_text[:120]
    parts = [p.strip() for p in QUERY_SPLIT_RE.split(base) if p.strip()] or [base]

    out: List[str] = []
    seen = set()

    def _push(candidate: str) -> None:
        key = candidate.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(candidate.strip())

    for part in parts:
        _push(part)

    # Query-length backoff for very narrow strings.
    seed_tokens = [t.lower() for t in QUERY_TOKEN_RE.findall(base)]
    compact = [tok for tok in seed_tokens if tok not in QUERY_STOPWORDS]
    if len(compact) >= 5:
        _push(" ".join(compact[:5]))

    # Historical claims should always include at least one broad person/topic query.
    if gap.claim_kind in {
        ClaimKind.HISTORICAL_NARRATIVE,
        ClaimKind.BIOGRAPHICAL,
        ClaimKind.COMPANY_OPERATIONS,
        ClaimKind.LEGAL_REGULATORY,
    }:
        claim_tokens = [t.lower() for t in QUERY_TOKEN_RE.findall(f"{gap.chapter} {gap.claim_text}")]
        claim_compact = [tok for tok in claim_tokens if tok not in QUERY_STOPWORDS]
        if len(claim_compact) >= 2:
            _push(" ".join(claim_compact[:2]))
        if len(claim_compact) >= 4:
            _push(" ".join(claim_compact[:4]))

    return out[:max_attempts]


def _noise_threshold_for_adapter(adapter: PullAdapter, settings: OrchestratorSettings) -> int:
    """Return source-family threshold for deciding when result sets are too noisy."""

    if adapter.source_type == SourceType.PLAYWRIGHT:
        return max(1, settings.pull_noise_threshold_playwright)
    if adapter.source_type == SourceType.KEYED_API:
        return max(1, settings.pull_noise_threshold_keyed_api)
    if adapter.source_type == SourceType.FREE_API:
        return max(1, settings.pull_noise_threshold_free_api)
    return max(1, settings.pull_noise_threshold)


def _execute_with_accordion(
    adapter: PullAdapter,
    gap: PlannedGap,
    run_dir: str,
    timeout_seconds: int,
    emit_fn: Callable[..., None] | None,
    settings: OrchestratorSettings,
) -> List[SourceResult]:
    """Execute one gap/source with ladder traversal and bounded retries.

    Assumptions:
    - `gap.query_ladder` stores a serialized AccordionLadder from reflection.
    - We cap attempts per gap/source for predictable runtime behavior.
    - If all ladder paths fail, we run one final entity-only retry before review.
    """

    ladder_dict = getattr(gap, "query_ladder", {}) or {}
    if isinstance(ladder_dict, dict) and ladder_dict:
        ladder = AccordionLadder.from_dict(ladder_dict)
    else:
        # Contract-preserving fallback when older records lack query_ladder.
        seed_query = (gap.search_queries[0] if gap.search_queries else gap.claim_text[:120]).strip()
        ladder = AccordionLadder(
            constrained=seed_query,
            contextual=seed_query,
            broad=seed_query,
            fallback=seed_query,
            primary_term=seed_query.split()[0] if seed_query else "claim",
            claim_kind=gap.claim_kind.value,
            evidence_need=gap.evidence_need.value,
            generation_method="legacy_fallback",
        )

    max_attempts = max(1, settings.pull_max_query_attempts)
    synonym_cap = max(1, settings.pull_synonym_cap)
    noise_threshold = _noise_threshold_for_adapter(adapter, settings)
    attempted_queries: set[str] = set()
    source_results: List[SourceResult] = []
    attempts_used = 0

    current_rung = "constrained"
    current_synonym_idx = 0
    current_queries = ladder.queries_for_rung(current_rung, synonym_cap=synonym_cap)
    if not current_queries:
        current_rung = "contextual"
        current_queries = ladder.queries_for_rung(current_rung, synonym_cap=synonym_cap)
    if not current_queries:
        return source_results

    current_query = current_queries[0]

    while attempts_used < max_attempts and current_query:
        query_key = current_query.strip().lower()
        if not query_key or query_key in attempted_queries:
            break
        attempted_queries.add(query_key)
        attempts_used += 1

        if emit_fn:
            emit_fn(
                stage="pulling",
                status="progress",
                message=f"[{gap.gap_id}] querying {adapter.source_id} ({attempts_used}/{max_attempts}): {current_query[:160]}",
                meta={
                    "gap_id": gap.gap_id,
                    "source_id": adapter.source_id,
                    "query": current_query,
                    "attempt_index": attempts_used,
                    "attempt_total": max_attempts,
                    "rung": current_rung,
                    "synonym_idx": current_synonym_idx,
                },
            )

        result = adapter.pull(
            gap=gap,
            query=current_query,
            run_dir=run_dir,
            timeout_seconds=timeout_seconds,
        )
        source_results.append(result)
        doc_count = int(result.document_count or 0)

        move = get_accordion_move(
            ladder,
            current_rung,
            current_synonym_idx,
            doc_count,
            noise_threshold=noise_threshold,
            synonym_cap=synonym_cap,
        )

        if emit_fn:
            emit_fn(
                stage="pulling",
                status=move.action,
                message=f"[{gap.gap_id}] {adapter.source_id}: {move.reason}",
                meta={
                    "gap_id": gap.gap_id,
                    "source_id": adapter.source_id,
                    "query": current_query,
                    "doc_count": doc_count,
                    "result_status": result.status,
                    "rung": current_rung,
                    "synonym_idx": current_synonym_idx,
                    "action": move.action,
                    "attempt_index": attempts_used,
                    "attempt_total": max_attempts,
                },
            )

        if move.action == "accept":
            break

        # Structural adapter errors should not consume additional ladder hops.
        if result.status == "failed":
            break

        if move.action == "exhausted":
            primary = (ladder.primary_term or "").strip()
            primary_key = primary.lower()
            if primary and primary_key not in attempted_queries and attempts_used < max_attempts:
                current_query = primary
                current_rung = "broad"
                current_synonym_idx = 0
                if emit_fn:
                    emit_fn(
                        stage="pulling",
                        status="progress",
                        message=f"[{gap.gap_id}] final entity-only retry: {primary}",
                        meta={
                            "gap_id": gap.gap_id,
                            "source_id": adapter.source_id,
                            "query": primary,
                            "action": "entity_retry",
                            "attempt_index": attempts_used + 1,
                            "attempt_total": max_attempts,
                        },
                    )
                continue

            gap.needs_review = True
            note = "Accordion exhausted all rungs for this source."
            if note not in (gap.review_notes or ""):
                gap.review_notes = f"{gap.review_notes} {note}".strip()
            break

        if move.action in {"lateral", "widen", "tighten"}:
            current_rung = move.rung
            current_synonym_idx = move.synonym_idx
            current_query = move.next_queries[0] if move.next_queries else ""
            continue

        break

    return source_results


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

    source_ids = _source_ids_for_gap(gap, availability, settings)

    for source_id in source_ids:
        adapter = SOURCE_REGISTRY.get(source_id)
        if adapter is None or not adapter.is_available(availability):
            continue
        attempted.append(source_id)
        has_ladder = isinstance(getattr(gap, "query_ladder", {}), dict) and bool(getattr(gap, "query_ladder", {}))

        if has_ladder:
            results_for_source = _execute_with_accordion(
                adapter=adapter,
                gap=gap,
                run_dir=run_dir,
                timeout_seconds=settings.pull_timeout_seconds,
                emit_fn=emit_event,
                settings=settings,
            )
        else:
            results_for_source = []
            for query in (gap.search_queries or [gap.claim_text[:120]]):
                attempts = _query_attempt_chain(gap, query, max_attempts=settings.pull_max_query_attempts)
                for attempt_index, attempt_query in enumerate(attempts, start=1):
                    if emit_event:
                        emit_event(
                            stage="pulling",
                            status="progress",
                            message=f"[{gap.gap_id}] querying {source_id} ({attempt_index}/{len(attempts)}): {attempt_query[:160]}",
                            meta={
                                "gap_id": gap.gap_id,
                                "source_id": source_id,
                                "query": attempt_query,
                                "attempt_index": attempt_index,
                                "attempt_total": len(attempts),
                            },
                        )
                    result = adapter.pull(
                        gap=gap,
                        query=attempt_query,
                        run_dir=run_dir,
                        timeout_seconds=settings.pull_timeout_seconds,
                    )
                    results_for_source.append(result)
                    if emit_event:
                        emit_event(
                            stage="pulling",
                            status="progress",
                            message=f"[{gap.gap_id}] {source_id} -> {result.status} ({result.document_count} docs)",
                            meta={
                                "gap_id": gap.gap_id,
                                "source_id": source_id,
                                "query": attempt_query,
                                "result_status": result.status,
                                "document_count": result.document_count,
                                "artifact_type": result.artifact_type,
                                "attempt_index": attempt_index,
                                "attempt_total": len(attempts),
                            },
                        )
                    if result.document_count > 0 or result.status == "failed":
                        break

        source_results.extend(results_for_source)
        if any(r.status in {"completed", "partial"} for r in results_for_source):
            succeeded.append(source_id)
        if any(r.status == "failed" for r in results_for_source):
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
