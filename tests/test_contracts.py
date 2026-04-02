"""Contract serialization tests for dataclass pipeline boundaries."""

from __future__ import annotations

from contracts import (
    Gap,
    GapMap,
    GapPriority,
    GapType,
    PlannedGap,
    ResearchPlan,
    RunRecord,
    RunStatus,
    SourceAvailability,
    SourceType,
    run_record_from_dict,
    run_record_to_dict,
)


def test_run_record_round_trip_serialization() -> None:
    record = RunRecord(
        run_id="run_123",
        manuscript_path="Manuscript/ch1.docx",
        status=RunStatus.PLANNING,
        gap_map=GapMap(
            manuscript_path="Manuscript/ch1.docx",
            manuscript_fingerprint="abc123",
            gaps=[
                Gap(
                    gap_id="AUTO-01-G1",
                    chapter="Chapter 1",
                    claim_text="Claim needs source",
                    gap_type=GapType.EXPLICIT,
                    priority=GapPriority.HIGH,
                    suggested_queries=["query 1"],
                )
            ],
            explicit_count=1,
            implicit_count=0,
        ),
        research_plan=ResearchPlan(
            run_id="run_123",
            manuscript_path="Manuscript/ch1.docx",
            plan_summary="Summary",
            gaps=[
                PlannedGap(
                    gap_id="AUTO-01-G1",
                    chapter="Chapter 1",
                    claim_text="Claim needs source",
                    gap_type=GapType.EXPLICIT,
                    priority=GapPriority.HIGH,
                    search_queries=["query 1"],
                    source_types=[SourceType.FREE_API],
                    preferred_sources=["world_bank"],
                    rationale="Core claim",
                )
            ],
            estimated_pull_count=1,
            reflection_method="heuristic_fallback",
            source_availability=SourceAvailability(free_apis=["world_bank"]),
        ),
    )

    payload = run_record_to_dict(record)
    rebuilt = run_record_from_dict(payload)

    assert rebuilt.run_id == "run_123"
    assert rebuilt.status == RunStatus.PLANNING
    assert rebuilt.gap_map is not None
    assert rebuilt.gap_map.gaps[0].gap_type == GapType.EXPLICIT
    assert rebuilt.research_plan is not None
    assert rebuilt.research_plan.gaps[0].source_types[0] == SourceType.FREE_API
