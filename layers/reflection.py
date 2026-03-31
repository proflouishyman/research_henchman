"""Layer 2: LLM reflection of GapMap into ResearchPlan."""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..config import OrchestratorSettings
from ..contracts import (
    GapMap,
    PlannedGap,
    ResearchPlan,
    SourceAvailability,
    SourceType,
)
from ..store import now_utc
from .analysis import _call_ollama


def reflect_on_gaps(
    gap_map: GapMap,
    availability: SourceAvailability,
    run_id: str,
    settings: OrchestratorSettings,
) -> ResearchPlan:
    """Generate reflected research plan using Ollama with heuristic fallback."""

    plan: Optional[ResearchPlan] = None
    fallback_reason = ""

    if settings.reflection_use_ollama:
        try:
            plan = _reflect_with_ollama(gap_map, availability, run_id, settings)
        except Exception as exc:  # noqa: BLE001 - fallback required by contract.
            fallback_reason = str(exc)[:200]

    if plan is None:
        plan = _reflect_heuristic(gap_map, availability, run_id, fallback_reason=fallback_reason)
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
        + (f"Playwright unavailable reason: {availability.playwright_unavailable_reason}" if availability.playwright_unavailable_reason else "")
    )


def _build_reflection_prompt(gap_map: GapMap, availability: SourceAvailability) -> str:
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

    return f"""You are a research strategist reviewing an evidentiary gap analysis for a manuscript.

Your job is to:
1. Review each gap and decide whether it is worth pursuing.
2. Refine the suggested search queries into the 2-4 most targeted, retrievable strings.
3. Recommend which source types to use for each gap based on what is AVAILABLE.
4. Write a brief plan summary explaining what the manuscript needs and why.

AVAILABLE SOURCES:
{sources_block}

If a source type is not listed as available, do NOT recommend it.
If a gap cannot be filled with available sources, set skip=true and explain why.

For each gap, return:
  gap_id            — unchanged from input
  search_queries    — final list of 2-4 refined search strings
  source_types      — list of: \"free_api\", \"keyed_api\", \"playwright\" (in preferred order)
  preferred_sources — list of specific source IDs (e.g. \"world_bank\", \"ebscohost\")
  rationale         — 1 sentence: why this gap matters to the manuscript's argument
  skip              — true if the gap should not be pursued
  skip_reason       — explanation when skip is true

Also return:
  plan_summary      — 1 paragraph overview of the research plan

Return ONLY a JSON object with keys: plan_summary, gaps (array).
No preamble, no markdown fences.

GAP ANALYSIS:
{gaps_json}
"""


def _reflect_with_ollama(
    gap_map: GapMap,
    availability: SourceAvailability,
    run_id: str,
    settings: OrchestratorSettings,
) -> ResearchPlan:
    prompt = _build_reflection_prompt(gap_map, availability)
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


def _default_source_routing(priority: object, availability: SourceAvailability) -> List[SourceType]:
    """Fallback source order when reflection LLM is unavailable."""

    types: List[SourceType] = []
    if availability.free_apis:
        types.append(SourceType.FREE_API)
    if availability.keyed_apis and str(priority) in {"GapPriority.HIGH", "GapPriority.MEDIUM", "high", "medium"}:
        types.append(SourceType.KEYED_API)
    if availability.playwright_sources and str(priority) in {"GapPriority.HIGH", "high"}:
        types.append(SourceType.PLAYWRIGHT)
    return types or [SourceType.FREE_API]


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
                rationale="Heuristic fallback — LLM reflection unavailable.",
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
