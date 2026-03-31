"""Layer 3 pull/router tests."""

from __future__ import annotations

from pathlib import Path

from app.adapters.base import PullAdapter
from app.contracts import (
    ClaimKind,
    EvidenceNeed,
    GapPriority,
    GapType,
    PlannedGap,
    ResearchPlan,
    SourceAvailability,
    SourceResult,
    SourceType,
)
from app.layers import pull


class _FakeAdapter(PullAdapter):
    source_id = "fake_source"
    source_type = SourceType.FREE_API

    def is_available(self, availability: SourceAvailability) -> bool:
        return self.source_id in availability.free_apis

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 60) -> SourceResult:
        root = Path(run_dir) / gap.gap_id / self.source_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "out.json").write_text("[]", encoding="utf-8")
        return SourceResult(
            source_id=self.source_id,
            source_type=self.source_type,
            query=query,
            gap_id=gap.gap_id,
            document_count=2,
            run_dir=str(root),
            artifact_type="json_records",
            status="completed",
        )


def test_build_source_availability_marks_missing_keys(settings_factory, monkeypatch) -> None:
    settings = settings_factory(BLS_API_KEY="", BEA_USER_ID="", CENSUS_API_KEY="")
    monkeypatch.setattr(pull, "check_cdp_endpoint", lambda _url: "refused")

    availability = pull.build_source_availability(settings)

    assert "world_bank" in availability.free_apis
    assert "bls" in availability.missing_keys
    assert availability.playwright_unavailable_reason


def test_build_source_availability_uses_library_profile_playwright_sources(settings_factory, monkeypatch) -> None:
    settings = settings_factory(ORCH_LIBRARY_SYSTEM="generic")
    monkeypatch.setattr(pull, "check_cdp_endpoint", lambda _url: "")

    availability = pull.build_source_availability(settings)

    assert "jstor" in availability.playwright_sources
    assert "project_muse" in availability.playwright_sources
    assert "ebscohost" in availability.playwright_sources
    assert "proquest_historical_newspapers" not in availability.playwright_sources


def test_build_source_availability_allows_extra_playwright_sources(settings_factory, monkeypatch) -> None:
    settings = settings_factory(
        ORCH_LIBRARY_SYSTEM="generic",
        ORCH_PLAYWRIGHT_EXTRA_SOURCES="statista, gale_primary_sources",
    )
    monkeypatch.setattr(pull, "check_cdp_endpoint", lambda _url: "")

    availability = pull.build_source_availability(settings)

    assert "statista" in availability.playwright_sources
    assert "gale_primary_sources" in availability.playwright_sources


def test_pull_router_aggregates_gap_results(settings_factory, monkeypatch) -> None:
    settings = settings_factory()
    availability = SourceAvailability(free_apis=["fake_source"])

    monkeypatch.setattr(pull, "SOURCE_REGISTRY", {"fake_source": _FakeAdapter()})

    plan = ResearchPlan(
        run_id="run_1",
        manuscript_path="Manuscript/ch1.docx",
        plan_summary="summary",
        source_availability=availability,
        gaps=[
            PlannedGap(
                gap_id="AUTO-01-G1",
                chapter="Chapter 1",
                claim_text="Claim",
                gap_type=GapType.IMPLICIT,
                priority=GapPriority.HIGH,
                search_queries=["q1", "q2"],
                source_types=[SourceType.FREE_API],
                preferred_sources=["fake_source"],
                rationale="r",
            )
        ],
    )

    events = []

    def _emit_event(**kwargs):
        events.append(kwargs)

    out = pull.pull_for_plan(plan, settings, _emit_event, "run_1")

    assert len(out) == 1
    assert out[0].status == "completed"
    assert out[0].total_documents == 4
    assert events


def test_rank_sources_prefers_scholarly_for_historical_claims() -> None:
    availability = SourceAvailability(
        free_apis=["world_bank"],
        keyed_apis=["ebsco_api"],
        playwright_sources=[],
    )

    ranked = pull.rank_sources_for_claim(
        ClaimKind.HISTORICAL_NARRATIVE,
        EvidenceNeed.SCHOLARLY_SECONDARY,
        availability,
        source_types=[SourceType.KEYED_API, SourceType.FREE_API],
        max_sources=3,
    )

    assert ranked
    assert ranked[0] == "ebsco_api"


def test_rank_sources_prefers_jstor_family_over_macro_for_history() -> None:
    availability = SourceAvailability(
        free_apis=["world_bank", "fred"],
        keyed_apis=[],
        playwright_sources=["jstor", "project_muse"],
    )

    ranked = pull.rank_sources_for_claim(
        ClaimKind.HISTORICAL_NARRATIVE,
        EvidenceNeed.SCHOLARLY_SECONDARY,
        availability,
        source_types=[SourceType.PLAYWRIGHT, SourceType.FREE_API],
        max_sources=4,
    )

    assert ranked
    assert ranked[0] in {"jstor", "project_muse"}
    assert "world_bank" not in ranked[:2]
