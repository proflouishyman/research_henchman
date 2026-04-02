"""Layer 2: LLM reflection of GapMap into ResearchPlan."""

from __future__ import annotations

import json
import math
import re
from typing import Dict, List, Optional, Tuple

from config import OrchestratorSettings
from contracts import (
    ClaimKind,
    EvidenceNeed,
    GapMap,
    GapPriority,
    PlannedGap,
    ResearchPlan,
    SourceAvailability,
    SourceType,
)
from store import now_utc
from .analysis import _call_ollama
from .pull import rank_sources_for_claim, source_capability_catalog, source_fit_reason
from .search_policy import AccordionLadder, classify_and_build_ladder, query_quality_score

STATISTICAL_SOURCE_IDS = {"world_bank", "fred", "oecd", "bea", "census", "bls", "ilostat"}
# Seed-only sources provide discovery links without verified full-document retrieval.
# Confidence should account for this additional retrieval uncertainty.
SEED_ONLY_SOURCE_IDS = {"ebsco_api"}
QUALITATIVE_CLAIM_KINDS = {
    ClaimKind.HISTORICAL_NARRATIVE,
    ClaimKind.LEGAL_REGULATORY,
    ClaimKind.COMPANY_OPERATIONS,
    ClaimKind.BIOGRAPHICAL,
    ClaimKind.OTHER,
}

STOPWORDS = {
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
    "been",
    "were",
    "have",
    "has",
    "had",
    "your",
    "their",
    "them",
    "they",
    "could",
    "would",
    "should",
    "argument",
    "evidence",
    "claim",
}
QUERY_SPLIT_RE = re.compile(r"\s*[|;\n]+\s*")


def reflect_on_gaps(
    gap_map: GapMap,
    availability: SourceAvailability,
    run_id: str,
    settings: OrchestratorSettings,
) -> ResearchPlan:
    """Generate reflected research plan using Ollama with policy and review gates."""

    plan: Optional[ResearchPlan] = None
    fallback_reason = ""

    if settings.reflection_use_ollama:
        try:
            plan = _reflect_with_ollama(gap_map, availability, run_id, settings)
        except Exception as exc:  # noqa: BLE001 - fallback required by contract.
            fallback_reason = str(exc)[:200]

    if plan is None:
        plan = _reflect_heuristic(gap_map, availability, run_id, fallback_reason=fallback_reason)

    _apply_routing_policy(plan, availability, settings)

    initially_needs_review = [gap for gap in plan.gaps if gap.needs_review and not gap.skip]
    resolved = 0
    if initially_needs_review and settings.plan_review_use_ollama:
        before = {gap.gap_id for gap in initially_needs_review}
        _review_needs_review_gaps_with_ollama(plan, availability, settings)
        _apply_routing_policy(plan, availability, settings)
        after = {gap.gap_id for gap in plan.gaps if gap.needs_review and not gap.skip}
        resolved = len(before - after)

    _finalize_review_status(plan)
    plan.routing_method = "policy_v1"
    plan.review_required_count = sum(1 for gap in plan.gaps if gap.needs_review)
    plan.review_resolved_count = max(0, resolved)
    plan.estimated_pull_count = sum(len(gap.search_queries) for gap in plan.gaps if not gap.skip)

    return plan


def _format_source_availability(availability: SourceAvailability) -> str:
    return (
        "Free APIs: " + ", ".join(availability.free_apis or ["none"]) + "\n"
        "Keyed APIs: " + ", ".join(availability.keyed_apis or ["none"]) + "\n"
        "Playwright sources: " + ", ".join(availability.playwright_sources or ["none"]) + "\n"
        "Missing keys: "
        + (
            ", ".join(f"{src}:{key}" for src, key in sorted(availability.missing_keys.items()))
            if availability.missing_keys
            else "none"
        )
        + "\n"
        + (
            f"Playwright unavailable reason: {availability.playwright_unavailable_reason}"
            if availability.playwright_unavailable_reason
            else ""
        )
    )


def _build_reflection_prompt(gap_map: GapMap, availability: SourceAvailability, settings: OrchestratorSettings) -> str:
    gaps_json = json.dumps(
        [
            {
                "gap_id": gap.gap_id,
                "chapter": gap.chapter,
                "claim_text": gap.claim_text,
                "gap_type": gap.gap_type.value,
                "priority": gap.priority.value,
                "suggested_queries": gap.suggested_queries,
            }
            for gap in gap_map.gaps
        ],
        ensure_ascii=False,
        indent=2,
    )
    sources_block = _format_source_availability(availability)
    capabilities_json = json.dumps(source_capability_catalog(settings), ensure_ascii=False, indent=2)

    return f"""You are a research strategist reviewing an evidentiary gap analysis for a manuscript.

Your job is to:
1. Review each gap and decide whether it is worth pursuing.
2. Refine suggested queries into 2-4 targeted retrievable strings.
3. Assign each gap a semantic claim_kind and evidence_need.
4. Route to sources that match claim semantics and availability.
5. Write a brief plan summary.

AVAILABLE SOURCES:
{sources_block}

SOURCE CAPABILITIES:
{capabilities_json}

For each gap return:
  gap_id
  search_queries
  source_types ("free_api", "keyed_api", "playwright")
  preferred_sources
  rationale
  claim_kind ("historical_narrative", "quantitative_macro", "quantitative_labor",
              "legal_regulatory", "company_operations", "biographical", "other")
  evidence_need ("scholarly_secondary", "primary_source", "official_statistics",
                 "legal_text", "news_archive", "mixed")
  route_confidence (0.0-1.0)
  skip
  skip_reason

Also return:
  plan_summary

Return ONLY a JSON object with keys: plan_summary, gaps (array).
No preamble, no markdown fences.

GAP ANALYSIS:
{gaps_json}
"""


def _review_prompt(plan: ResearchPlan, availability: SourceAvailability, settings: OrchestratorSettings) -> str:
    needs_review = [
        {
            "gap_id": gap.gap_id,
            "chapter": gap.chapter,
            "claim_text": gap.claim_text,
            "current_queries": gap.search_queries,
            "current_sources": gap.preferred_sources,
            "claim_kind": gap.claim_kind.value,
            "evidence_need": gap.evidence_need.value,
            "route_reason": gap.route_reason,
        }
        for gap in plan.gaps
        if gap.needs_review and not gap.skip
    ]

    return f"""You are reviewing low-confidence research-routing gaps.

Task:
- Improve only the gaps provided below.
- Produce source-specific, retrievable search strings.
- Choose sources that semantically fit each claim and are currently available.
- If no suitable source exists, keep preferred_sources empty and explain.

AVAILABLE SOURCES:
{_format_source_availability(availability)}

SOURCE CAPABILITIES:
{json.dumps(source_capability_catalog(settings), ensure_ascii=False, indent=2)}

NEEDS REVIEW GAPS:
{json.dumps(needs_review, ensure_ascii=False, indent=2)}

Return ONLY JSON object:
{{
  "gaps": [
    {{
      "gap_id": "...",
      "search_queries": ["..."],
      "preferred_sources": ["..."],
      "rationale": "...",
      "claim_kind": "...",
      "evidence_need": "...",
      "route_confidence": 0.0,
      "review_notes": "..."
    }}
  ]
}}
"""


def _reflect_with_ollama(
    gap_map: GapMap,
    availability: SourceAvailability,
    run_id: str,
    settings: OrchestratorSettings,
) -> ResearchPlan:
    prompt = _build_reflection_prompt(gap_map, availability, settings)
    response = _call_ollama(
        prompt=prompt,
        model=settings.reflection_model,
        base_url=settings.ollama_base_url,
        timeout_seconds=settings.reflection_timeout_seconds,
    )
    parsed = _parse_reflection_json(response)
    planned_gaps = _parse_planned_gaps(parsed.get("gaps", []), gap_map)
    return ResearchPlan(
        run_id=run_id,
        manuscript_path=gap_map.manuscript_path,
        plan_summary=str(parsed.get("plan_summary", "")).strip(),
        gaps=planned_gaps,
        estimated_pull_count=sum(len(gap.search_queries) for gap in planned_gaps if not gap.skip),
        reflection_model=settings.reflection_model,
        reflection_method="ollama",
        source_availability=availability,
        created_at=now_utc(),
    )


def _review_needs_review_gaps_with_ollama(
    plan: ResearchPlan,
    availability: SourceAvailability,
    settings: OrchestratorSettings,
) -> None:
    """Run a second local-LLM pass only for low-confidence routing rows."""

    review_targets = [gap for gap in plan.gaps if gap.needs_review and not gap.skip]
    if not review_targets:
        return

    try:
        response = _call_ollama(
            prompt=_review_prompt(plan, availability, settings),
            model=settings.plan_review_model,
            base_url=settings.ollama_base_url,
            timeout_seconds=settings.plan_review_timeout_seconds,
        )
        payload = _parse_reflection_json(response)
    except Exception as exc:  # noqa: BLE001 - review is best-effort.
        note = f"review_unavailable:{str(exc)[:120]}"
        for gap in review_targets:
            if not gap.review_notes:
                gap.review_notes = note
        return

    rows = payload.get("gaps", [])
    if not isinstance(rows, list):
        return

    by_id = {gap.gap_id: gap for gap in review_targets}
    for row in rows:
        if not isinstance(row, dict):
            continue
        gap_id = str(row.get("gap_id", "")).strip()
        gap = by_id.get(gap_id)
        if gap is None:
            continue

        queries = row.get("search_queries", [])
        if isinstance(queries, list):
            cleaned = [str(q).strip() for q in queries if str(q).strip()]
            if cleaned:
                gap.search_queries = cleaned

        preferred = row.get("preferred_sources", [])
        if isinstance(preferred, list):
            allowed = set(availability.free_apis + availability.keyed_apis + availability.playwright_sources)
            cleaned_sources = [str(s).strip() for s in preferred if str(s).strip() in allowed]
            if cleaned_sources:
                gap.preferred_sources = cleaned_sources

        if "rationale" in row and str(row.get("rationale", "")).strip():
            gap.rationale = str(row.get("rationale", "")).strip()

        try:
            gap.claim_kind = ClaimKind(str(row.get("claim_kind", gap.claim_kind.value)).strip())
        except Exception:
            pass

        try:
            gap.evidence_need = EvidenceNeed(str(row.get("evidence_need", gap.evidence_need.value)).strip())
        except Exception:
            pass

        try:
            llm_conf = float(row.get("route_confidence", gap.route_confidence))
            gap.route_confidence = max(0.0, min(1.0, llm_conf))
        except Exception:
            pass

        gap.review_notes = str(row.get("review_notes", "")).strip() or gap.review_notes
        gap.review_method = f"ollama:{settings.plan_review_model}"


def _parse_reflection_json(response: str) -> Dict[str, object]:
    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        payload = {}

    if isinstance(payload, dict):
        return payload

    start = response.find("{")
    end = response.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise RuntimeError("reflection_response_not_json")
    payload = json.loads(response[start : end + 1])
    if not isinstance(payload, dict):
        raise RuntimeError("reflection_response_not_object")
    return payload


def _parse_planned_gaps(rows: object, gap_map: GapMap) -> List[PlannedGap]:
    rows_list = rows if isinstance(rows, list) else []
    source_map = {gap.gap_id: gap for gap in gap_map.gaps}
    out: List[PlannedGap] = []

    for row in rows_list:
        if not isinstance(row, dict):
            continue
        gap_id = str(row.get("gap_id", "")).strip()
        if not gap_id or gap_id not in source_map:
            continue
        source_gap = source_map[gap_id]

        query_rows = row.get("search_queries", [])
        search_queries = [str(item).strip() for item in query_rows if str(item).strip()] if isinstance(query_rows, list) else []
        if not search_queries:
            search_queries = source_gap.suggested_queries[:]

        type_rows = row.get("source_types", [])
        source_types: List[SourceType] = []
        if isinstance(type_rows, list):
            for item in type_rows:
                try:
                    source_types.append(SourceType(str(item).strip()))
                except Exception:
                    continue

        preferred_rows = row.get("preferred_sources", [])
        preferred_sources = [str(item).strip() for item in preferred_rows if str(item).strip()] if isinstance(preferred_rows, list) else []
        query_ladder = row.get("query_ladder", {})
        if not isinstance(query_ladder, dict):
            query_ladder = {}

        try:
            claim_kind = ClaimKind(str(row.get("claim_kind", ClaimKind.OTHER.value)).strip())
        except Exception:
            claim_kind = ClaimKind.OTHER

        try:
            evidence_need = EvidenceNeed(str(row.get("evidence_need", EvidenceNeed.MIXED.value)).strip())
        except Exception:
            evidence_need = EvidenceNeed.MIXED

        try:
            route_confidence = max(0.0, min(1.0, float(row.get("route_confidence", 0.0))))
        except Exception:
            route_confidence = 0.0

        out.append(
            PlannedGap(
                gap_id=source_gap.gap_id,
                chapter=source_gap.chapter,
                claim_text=source_gap.claim_text,
                gap_type=source_gap.gap_type,
                priority=source_gap.priority,
                search_queries=search_queries,
                source_types=source_types,
                preferred_sources=preferred_sources,
                rationale=str(row.get("rationale", "")).strip(),
                skip=bool(row.get("skip", False)),
                skip_reason=str(row.get("skip_reason", "")).strip(),
                claim_kind=claim_kind,
                evidence_need=evidence_need,
                route_confidence=route_confidence,
                query_ladder=query_ladder,
            )
        )

    # Guarantee every source gap appears in plan even if LLM omits rows.
    planned_ids = {gap.gap_id for gap in out}
    for gap in gap_map.gaps:
        if gap.gap_id in planned_ids:
            continue
        out.append(
            PlannedGap(
                gap_id=gap.gap_id,
                chapter=gap.chapter,
                claim_text=gap.claim_text,
                gap_type=gap.gap_type,
                priority=gap.priority,
                search_queries=gap.suggested_queries[:],
                source_types=[],
                preferred_sources=[],
                rationale="Added by fallback because reflected row was missing.",
                skip=False,
            )
        )

    return out


def _default_preferred_sources(source_types: List[SourceType], availability: SourceAvailability) -> List[str]:
    preferred: List[str] = []
    for source_type in source_types:
        if source_type == SourceType.FREE_API:
            preferred.extend(availability.free_apis[:2])
        elif source_type == SourceType.KEYED_API:
            preferred.extend(availability.keyed_apis[:2])
        elif source_type == SourceType.PLAYWRIGHT:
            preferred.extend(availability.playwright_sources[:2])

    # Preserve order while removing duplicates.
    out: List[str] = []
    seen = set()
    for source_id in preferred:
        if source_id in seen:
            continue
        seen.add(source_id)
        out.append(source_id)
    return out


def _default_source_routing(priority: GapPriority, availability: SourceAvailability) -> List[SourceType]:
    """Fallback source order when reflection LLM is unavailable."""

    if priority == GapPriority.HIGH:
        return [SourceType.KEYED_API, SourceType.PLAYWRIGHT, SourceType.FREE_API]
    if priority == GapPriority.MEDIUM:
        return [SourceType.KEYED_API, SourceType.FREE_API]
    return [SourceType.FREE_API]


def _reflect_heuristic(
    gap_map: GapMap,
    availability: SourceAvailability,
    run_id: str,
    fallback_reason: str = "",
) -> ResearchPlan:
    """Fallback plan conversion when reflection LLM call fails."""

    planned_gaps: List[PlannedGap] = []
    for gap in gap_map.gaps:
        source_types = _default_source_routing(gap.priority, availability)
        planned_gaps.append(
            PlannedGap(
                gap_id=gap.gap_id,
                chapter=gap.chapter,
                claim_text=gap.claim_text,
                gap_type=gap.gap_type,
                priority=gap.priority,
                search_queries=gap.suggested_queries[:] or [gap.claim_text[:120]],
                source_types=source_types,
                preferred_sources=_default_preferred_sources(source_types, availability),
                rationale="Heuristic fallback - LLM reflection unavailable.",
                skip=False,
            )
        )

    summary = (
        f"Research plan for {gap_map.manuscript_path}. Found {len(gap_map.gaps)} gaps "
        f"({gap_map.explicit_count} explicit, {gap_map.implicit_count} implicit)."
    )
    if fallback_reason:
        summary += f" LLM reflection unavailable: {fallback_reason}"

    return ResearchPlan(
        run_id=run_id,
        manuscript_path=gap_map.manuscript_path,
        plan_summary=summary,
        gaps=planned_gaps,
        estimated_pull_count=sum(len(gap.search_queries) for gap in planned_gaps if not gap.skip),
        reflection_model="",
        reflection_method="heuristic_fallback",
        source_availability=availability,
        created_at=now_utc(),
    )


def _evidence_source_types(evidence_need: EvidenceNeed) -> List[SourceType]:
    if evidence_need in {
        EvidenceNeed.SCHOLARLY_SECONDARY,
        EvidenceNeed.PRIMARY_SOURCE,
        EvidenceNeed.NEWS_ARCHIVE,
        EvidenceNeed.LEGAL_TEXT,
    }:
        return [SourceType.KEYED_API, SourceType.PLAYWRIGHT]
    if evidence_need == EvidenceNeed.OFFICIAL_STATISTICS:
        return [SourceType.KEYED_API, SourceType.FREE_API]
    return [SourceType.KEYED_API, SourceType.FREE_API, SourceType.PLAYWRIGHT]


def _claim_routing_profile(gap: PlannedGap, settings: OrchestratorSettings) -> Tuple[ClaimKind, EvidenceNeed, float]:
    """Classify claim and build accordion ladder, caching by claim hash."""

    ck_str, en_str, conf, ladder = classify_and_build_ladder(
        chapter=gap.chapter,
        claim_text=gap.claim_text,
        use_llm=settings.reflection_use_ollama,
        model=settings.reflection_model,
        base_url=settings.ollama_base_url,
        timeout_seconds=min(settings.reflection_timeout_seconds, 25),
        existing_queries=gap.search_queries[:] if gap.search_queries else None,
        cache_dir=(settings.data_root / "search_policy_cache"),
    )
    gap.query_ladder = ladder.to_dict()
    try:
        claim_kind = ClaimKind(ck_str)
    except ValueError:
        claim_kind = ClaimKind.OTHER
    try:
        evidence_need = EvidenceNeed(en_str)
    except ValueError:
        evidence_need = EvidenceNeed.MIXED
    return (claim_kind, evidence_need, conf)


def _extract_keywords(text: str, limit: int = 6) -> List[str]:
    tokens = re.findall(r"[a-zA-Z][a-zA-Z\-']{2,}", text.lower())
    out: List[str] = []
    seen = set()
    for tok in tokens:
        if tok in STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def _explode_queries(candidates: List[str]) -> List[str]:
    """Split combined query blobs into concrete retrievable query strings."""

    out: List[str] = []
    seen = set()
    for raw in candidates:
        if not str(raw).strip():
            continue
        parts = [p.strip() for p in QUERY_SPLIT_RE.split(str(raw)) if p.strip()]
        if not parts:
            parts = [str(raw).strip()]
        for part in parts:
            key = part.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(part)
    return out


def _fallback_queries(gap: PlannedGap, evidence_need: EvidenceNeed) -> List[str]:
    kws = _extract_keywords(f"{gap.chapter} {gap.claim_text}")
    core = " ".join(kws[:4]) if kws else gap.chapter or "manuscript claim"

    if evidence_need == EvidenceNeed.OFFICIAL_STATISTICS:
        return [f"{core} statistics time series", f"{core} official data"]
    if evidence_need in {EvidenceNeed.SCHOLARLY_SECONDARY, EvidenceNeed.PRIMARY_SOURCE}:
        return [f"{core} scholarly article", f"{core} primary source archive"]
    if evidence_need == EvidenceNeed.LEGAL_TEXT:
        return [f"{core} legal text regulation", f"{core} statute policy archive"]
    if evidence_need == EvidenceNeed.NEWS_ARCHIVE:
        return [f"{core} historical newspaper archive", f"{core} periodical source"]
    return [f"{core} historical evidence", f"{core} supporting source"]


def _clean_queries(
    gap: PlannedGap,
    evidence_need: EvidenceNeed,
    settings: OrchestratorSettings,
) -> Tuple[List[str], float]:
    """Normalize planning-time query list from stored query ladder."""

    ladder_dict = getattr(gap, "query_ladder", {}) or {}
    if isinstance(ladder_dict, dict) and ladder_dict:
        ladder = AccordionLadder.from_dict(ladder_dict)
        ordered = ladder.queries_for_rung("constrained", synonym_cap=settings.pull_synonym_cap)
        if not ordered:
            ordered = ladder.queries_for_rung("contextual", synonym_cap=settings.pull_synonym_cap)
    else:
        ordered = []

    if not ordered:
        candidates = _explode_queries(gap.search_queries[:] if gap.search_queries else [])
        ordered = candidates if candidates else _fallback_queries(gap, evidence_need)

    # Ensure one broad backup query appears in plan if constrained terms are over-tight.
    fallback_candidates = _fallback_queries(gap, evidence_need)
    seen = {q.strip().lower() for q in ordered}
    for fallback in fallback_candidates:
        key = fallback.strip().lower()
        if not key or key in seen:
            continue
        ordered.append(fallback)
        seen.add(key)
        if len(ordered) >= max(4, settings.pull_synonym_cap):
            break

    scores = [query_quality_score(q) for q in ordered]
    avg = (sum(scores) / len(scores)) if scores else 0.0
    return (ordered[: max(4, settings.pull_synonym_cap)], avg)


def _calibrated_route_confidence(
    claim_conf: float,
    query_conf: float,
    has_sources: bool,
    preferred_sources: List[str],
    claim_kind: ClaimKind,
    evidence_need: EvidenceNeed,
) -> float:
    """Convert raw policy signals into a calibrated [0,1] route confidence.

    Non-obvious logic:
    - Uses logistic blending to avoid hard-clipping to 1.0.
    - Rewards evidence/source diversity when semantically appropriate.
    - Applies hard penalties for qualitative claims routed only to stats APIs.
    """

    centered_claim = (max(0.0, min(1.0, claim_conf)) - 0.5) * 1.8
    centered_query = (max(0.0, min(1.0, query_conf)) - 0.5) * 1.4
    source_term = 0.75 if has_sources else -1.1

    source_diversity = len(set(preferred_sources))
    diversity_bonus = 0.0
    if source_diversity >= 2:
        diversity_bonus += 0.18
    if source_diversity >= 3:
        diversity_bonus += 0.10

    seed_penalty = 0.0
    if preferred_sources and all(source_id in SEED_ONLY_SOURCE_IDS for source_id in preferred_sources):
        seed_penalty = 0.28
    elif preferred_sources and any(source_id in SEED_ONLY_SOURCE_IDS for source_id in preferred_sources):
        seed_penalty = 0.12

    only_stat_sources = bool(preferred_sources) and all(source_id in STATISTICAL_SOURCE_IDS for source_id in preferred_sources)
    mismatch_penalty = 0.0
    if evidence_need in {
        EvidenceNeed.SCHOLARLY_SECONDARY,
        EvidenceNeed.PRIMARY_SOURCE,
        EvidenceNeed.NEWS_ARCHIVE,
        EvidenceNeed.LEGAL_TEXT,
    } and any(source_id in STATISTICAL_SOURCE_IDS for source_id in preferred_sources):
        mismatch_penalty += 0.55
    if claim_kind in QUALITATIVE_CLAIM_KINDS and only_stat_sources:
        mismatch_penalty += 0.65

    z = centered_claim + centered_query + source_term + diversity_bonus - mismatch_penalty - seed_penalty
    confidence = 1.0 / (1.0 + math.exp(-z))
    return max(0.0, min(1.0, confidence))


def _apply_routing_policy(plan: ResearchPlan, availability: SourceAvailability, settings: OrchestratorSettings) -> None:
    """Assign claim/evidence types and enforce capability-based source routing."""

    for gap in plan.gaps:
        claim_kind, evidence_need, claim_conf = _claim_routing_profile(gap, settings)

        # Policy typing is authoritative to prevent semantic drift from
        # low-quality LLM rows (e.g., historical claims routed as macro stats).
        gap.claim_kind = claim_kind
        gap.evidence_need = evidence_need

        gap.source_types = _evidence_source_types(gap.evidence_need)
        cleaned_queries, query_conf = _clean_queries(gap, gap.evidence_need, settings)
        gap.search_queries = cleaned_queries

        preferred = rank_sources_for_claim(
            gap.claim_kind,
            gap.evidence_need,
            availability,
            source_types=gap.source_types,
            max_sources=3,
            settings=settings,
        )
        if not preferred and gap.claim_kind in QUALITATIVE_CLAIM_KINDS:
            preferred = rank_sources_for_claim(
                gap.claim_kind,
                EvidenceNeed.MIXED,
                availability,
                source_types=[SourceType.KEYED_API, SourceType.PLAYWRIGHT, SourceType.FREE_API],
                max_sources=3,
                settings=settings,
            )
        # Policy is authoritative: always replace legacy/LLM-preferred sources
        # with capability-ranked sources for this claim/evidence type.
        gap.preferred_sources = preferred

        has_sources = bool(gap.preferred_sources)
        only_stat_sources = has_sources and all(src in STATISTICAL_SOURCE_IDS for src in gap.preferred_sources)
        conf = _calibrated_route_confidence(
            claim_conf=claim_conf,
            query_conf=query_conf,
            has_sources=has_sources,
            preferred_sources=gap.preferred_sources,
            claim_kind=gap.claim_kind,
            evidence_need=gap.evidence_need,
        )

        gap.route_confidence = conf
        gap.route_reason = source_fit_reason(gap.claim_kind, gap.evidence_need, gap.preferred_sources)
        if not has_sources:
            missing = []
            if availability.playwright_unavailable_reason:
                missing.append(availability.playwright_unavailable_reason)
            if availability.missing_keys:
                missing.append(
                    "missing API keys: "
                    + ", ".join(
                        f"{source_id}:{env_key}" for source_id, env_key in sorted(availability.missing_keys.items())
                    )
                )
            if missing:
                gap.route_reason = f"{gap.route_reason} {' | '.join(missing)}"

        needs_review = (conf < settings.routing_min_confidence) or (not has_sources) or (
            gap.claim_kind in QUALITATIVE_CLAIM_KINDS and only_stat_sources
        )
        gap.needs_review = needs_review

        if needs_review and not gap.review_notes:
            gap.review_notes = "Low-confidence route; local review recommended."


def _finalize_review_status(plan: ResearchPlan) -> None:
    """Prevent bad pulls when route remains low confidence after review pass."""

    for gap in plan.gaps:
        if gap.skip:
            continue
        only_stat_sources = bool(gap.preferred_sources) and all(src in STATISTICAL_SOURCE_IDS for src in gap.preferred_sources)
        if gap.needs_review and gap.claim_kind in QUALITATIVE_CLAIM_KINDS and only_stat_sources:
            gap.skip = True
            gap.skip_reason = "Claim requires scholarly/archival sources; only statistical APIs are available."
            continue
        if gap.needs_review and not gap.preferred_sources:
            gap.skip = True
            gap.skip_reason = "No suitable source available for claim type; review required."
            continue

        if gap.needs_review and gap.preferred_sources:
            # Keep executable but make uncertainty explicit in rationale for UI transparency.
            prefix = "Needs review before publication use. "
            if not gap.rationale.startswith(prefix):
                gap.rationale = f"{prefix}{gap.rationale}".strip()

    active = sum(1 for gap in plan.gaps if not gap.skip)
    review_count = sum(1 for gap in plan.gaps if gap.needs_review)
    if review_count:
        plan.plan_summary = (
            f"{plan.plan_summary} Routing policy flagged {review_count} low-confidence gaps. "
            f"{active} gaps remain executable."
        ).strip()
