"""Layer 4 ingest tests."""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts import GapPullResult, IngestResult, PlannedGap, SourceResult, SourceType
from app.layers.ingest import ingest_gap_result


def test_ingest_skips_unresolvable_gap(settings_factory) -> None:
    settings = settings_factory()
    pull_result = GapPullResult(gap_id="AUTO-01-G1", status="unresolvable")

    out = ingest_gap_result(pull_result, settings, "run_1")

    assert out.skipped is True
    assert out.ingested is False


def test_ingest_json_records_indexes_documents(settings_factory) -> None:
    settings = settings_factory()
    root = Path(settings.pull_output_root) / "run_1" / "AUTO-01-G1" / "world_bank"
    root.mkdir(parents=True, exist_ok=True)
    (root / "records.json").write_text(json.dumps([{"a": 1}, {"a": 2}]), encoding="utf-8")

    source = SourceResult(
        source_id="world_bank",
        source_type=SourceType.FREE_API,
        query="q",
        gap_id="AUTO-01-G1",
        document_count=2,
        run_dir=str(root),
        artifact_type="json_records",
        status="completed",
    )
    pull_result = GapPullResult(gap_id="AUTO-01-G1", planned_gap=PlannedGap(gap_id="AUTO-01-G1"), results=[source])

    out = ingest_gap_result(pull_result, settings, "run_1")

    assert out.ingested is True
    assert out.documents_upserted == 2

    index_path = settings.data_root / "ingest_index.jsonl"
    assert index_path.exists()
