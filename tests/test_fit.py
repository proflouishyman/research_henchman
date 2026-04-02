"""Layer 5 fit tests."""

from __future__ import annotations

import json

from contracts import IngestResult
from layers.fit import fit_gap


def test_fit_skips_when_backend_none(settings_factory) -> None:
    settings = settings_factory(ORCH_LLM_BACKEND="none")
    ingest_result = IngestResult(gap_id="AUTO-01-G1", run_id="run_1", ingested=True)

    out = fit_gap(ingest_result, settings, "run_1")

    assert out.skipped is True
    assert out.skip_reason == "llm_backend=none"


def test_fit_scores_and_then_skips_existing_links(settings_factory) -> None:
    settings = settings_factory(ORCH_LLM_BACKEND="ollama", ORCH_LLM_MODEL="qwen2.5:7b")
    ingest_path = settings.data_root / "ingest_index.jsonl"
    ingest_path.write_text(
        "\n".join(
            [
                json.dumps({"gap_id": "AUTO-01-G1", "doc_id": "doc_1", "query": "q1", "record": {"v": 1}, "source_id": "s"}),
                json.dumps({"gap_id": "AUTO-01-G1", "doc_id": "doc_2", "query": "q2", "record": {"v": 2}, "source_id": "s"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ingest_result = IngestResult(gap_id="AUTO-01-G1", run_id="run_1", ingested=True)

    first = fit_gap(ingest_result, settings, "run_1")
    second = fit_gap(ingest_result, settings, "run_1")

    assert first.links_scored == 2
    assert second.links_scored == 0
    assert second.links_skipped == 2
