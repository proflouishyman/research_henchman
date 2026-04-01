"""Layer 2 reflection tests."""

from __future__ import annotations

from app.contracts import Gap, GapMap, GapPriority, GapType, SourceAvailability
from app.layers import reflection


def _gap_map() -> GapMap:
    return GapMap(
        manuscript_path="Manuscript/ch1.docx",
        manuscript_fingerprint="abc123",
        gaps=[
            Gap(
                gap_id="AUTO-01-G1",
                chapter="Chapter 1",
                claim_text="US unemployment rate reached 6% in 1991 but no citation is provided.",
                gap_type=GapType.IMPLICIT,
                priority=GapPriority.HIGH,
                suggested_queries=["us unemployment rate 1991 bls"],
            )
        ],
        explicit_count=0,
        implicit_count=1,
    )


def test_reflection_prompt_includes_source_availability(settings_factory) -> None:
    settings = settings_factory()
    prompt = reflection._build_reflection_prompt(
        _gap_map(),
        SourceAvailability(
            free_apis=["world_bank"],
            keyed_apis=["bls"],
            playwright_sources=[],
            missing_keys={"census": "CENSUS_API_KEY"},
            playwright_unavailable_reason="cdp down",
        ),
        settings,
    )

    assert "AVAILABLE SOURCES" in prompt
    assert "world_bank" in prompt
    assert "bls" in prompt
    assert "CENSUS_API_KEY" in prompt
    assert "cdp down" in prompt


def test_reflection_fallback_returns_plan(settings_factory, monkeypatch) -> None:
    settings = settings_factory(ORCH_REFLECTION_USE_OLLAMA="true")
    availability = SourceAvailability(free_apis=["world_bank"])

    def _boom(*_args, **_kwargs):
        raise RuntimeError("reflection failed")

    monkeypatch.setattr(reflection, "_reflect_with_ollama", _boom)
    plan = reflection.reflect_on_gaps(_gap_map(), availability, "run_1", settings)

    assert plan.reflection_method == "heuristic_fallback"
    assert plan.gaps
    assert plan.gaps[0].search_queries
    assert not plan.gaps[0].skip


def test_reflection_routes_historical_claims_to_scholarly_sources(settings_factory) -> None:
    settings = settings_factory(
        ORCH_REFLECTION_USE_OLLAMA="false",
        ORCH_PLAN_REVIEW_USE_OLLAMA="false",
    )
    gap_map = GapMap(
        manuscript_path="Manuscript/ch1.docx",
        manuscript_fingerprint="xyz",
        gaps=[
            Gap(
                gap_id="AUTO-01-G1",
                chapter="Chapter One: Merchant",
                claim_text="The manuscript claims NetMarket transformed historical commerce without citations.",
                gap_type=GapType.IMPLICIT,
                priority=GapPriority.HIGH,
                suggested_queries=["NetMarket historical commerce scholarly source"],
            )
        ],
    )
    availability = SourceAvailability(
        free_apis=["world_bank"],
        keyed_apis=["ebsco_api"],
    )

    plan = reflection.reflect_on_gaps(gap_map, availability, "run_h", settings)
    gap = plan.gaps[0]

    assert "ebsco_api" in gap.preferred_sources
    assert gap.evidence_need.value in {"scholarly_secondary", "news_archive", "primary_source"}
    assert gap.needs_review is False
    assert gap.route_confidence < 0.95


def test_reflection_marks_low_confidence_historical_meta_claim_for_review(settings_factory) -> None:
    settings = settings_factory(
        ORCH_REFLECTION_USE_OLLAMA="false",
        ORCH_PLAN_REVIEW_USE_OLLAMA="false",
    )
    gap_map = GapMap(
        manuscript_path="Manuscript/ch1.docx",
        manuscript_fingerprint="xyz",
        gaps=[
            Gap(
                gap_id="AUTO-01-G1",
                chapter="Chapter One: Merchant",
                claim_text="Argument is highly compressed; split claims and tie each to evidence.",
                gap_type=GapType.IMPLICIT,
                priority=GapPriority.LOW,
                suggested_queries=["Argument is highly compressed; split claims and tie each to evidence."],
            )
        ],
    )
    availability = SourceAvailability(free_apis=["world_bank"])

    plan = reflection.reflect_on_gaps(gap_map, availability, "run_low", settings)
    gap = plan.gaps[0]

    assert gap.needs_review is True
    assert gap.skip is True
    assert "No suitable source" in gap.skip_reason


def test_reflection_splits_compound_queries_for_pull_backoff(settings_factory) -> None:
    settings = settings_factory(
        ORCH_REFLECTION_USE_OLLAMA="false",
        ORCH_PLAN_REVIEW_USE_OLLAMA="false",
    )
    gap_map = GapMap(
        manuscript_path="Manuscript/ch1.docx",
        manuscript_fingerprint="xyz",
        gaps=[
            Gap(
                gap_id="AUTO-01-G1",
                chapter="Chapter One: Merchant",
                claim_text="John McDonogh served as a supercargo for Taylor Merchant Company in New Orleans.",
                gap_type=GapType.IMPLICIT,
                priority=GapPriority.HIGH,
                suggested_queries=[
                    "john mcdonogh supercargo great britain new orleans 1800 | taylor merchant company overseas assignments early 19th century"
                ],
            )
        ],
    )
    availability = SourceAvailability(
        free_apis=["world_bank"],
        keyed_apis=["ebsco_api"],
    )

    plan = reflection.reflect_on_gaps(gap_map, availability, "run_compound", settings)
    gap = plan.gaps[0]

    assert gap.search_queries
    assert any("|" not in query for query in gap.search_queries)
    assert any("john mcdonogh supercargo" in query for query in gap.search_queries)
