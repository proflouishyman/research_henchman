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
                claim_text="Claim without source",
                gap_type=GapType.IMPLICIT,
                priority=GapPriority.HIGH,
                suggested_queries=["query one"],
            )
        ],
        explicit_count=0,
        implicit_count=1,
    )


def test_reflection_prompt_includes_source_availability() -> None:
    prompt = reflection._build_reflection_prompt(
        _gap_map(),
        SourceAvailability(
            free_apis=["world_bank"],
            keyed_apis=["bls"],
            playwright_sources=[],
            missing_keys={"census": "CENSUS_API_KEY"},
            playwright_unavailable_reason="cdp down",
        ),
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
