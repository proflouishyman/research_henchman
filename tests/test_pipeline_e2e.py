"""End-to-end pipeline test with mocked source adapter."""

from __future__ import annotations

import json
from pathlib import Path

from adapters.base import PullAdapter
from contracts import RunRecord, RunStatus, SourceAvailability, SourceResult, SourceType, run_record_to_dict
from layers import pull
from pipeline import run_orchestration
from store import OrchestratorStore


class _FakeSource(PullAdapter):
    source_id = "world_bank"
    source_type = SourceType.FREE_API

    def is_available(self, availability: SourceAvailability) -> bool:
        return self.source_id in availability.free_apis

    def pull(self, gap, query, run_dir, timeout_seconds=60):
        root = Path(run_dir) / gap.gap_id / self.source_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "records.json").write_text(json.dumps([{"query": query, "value": 1}]), encoding="utf-8")
        return SourceResult(
            source_id=self.source_id,
            source_type=self.source_type,
            query=query,
            gap_id=gap.gap_id,
            document_count=1,
            run_dir=str(root),
            artifact_type="json_records",
            status="completed",
        )


def test_pipeline_runs_all_layers_with_contract_outputs(tmp_path, settings_factory, monkeypatch):
    settings = settings_factory(
        ORCH_GAP_ANALYSIS_USE_OLLAMA="false",
        ORCH_REFLECTION_USE_OLLAMA="false",
        ORCH_AUTO_INGEST="true",
        ORCH_AUTO_LLM_FIT="true",
    )

    store = OrchestratorStore(settings.data_root)
    manuscript = Path(settings.workspace) / "Manuscript" / "chapter_one.txt"
    manuscript.write_text(
        "Chapter One\nTODO cite this.\nThe argument likely suggests major effects without numbers.",
        encoding="utf-8",
    )

    run = RunRecord(
        run_id="run_test_1",
        manuscript_path="Manuscript/chapter_one.txt",
        status=RunStatus.QUEUED,
        stage_detail="Queued",
    )
    run.created_at = run.updated_at = "2026-03-31T00:00:00+00:00"
    store.upsert_run(run_record_to_dict(run))

    monkeypatch.setattr(pull, "SOURCE_REGISTRY", {"world_bank": _FakeSource()})
    monkeypatch.setattr(pull, "check_cdp_endpoint", lambda _url: "cdp disabled")

    run_orchestration(store, settings, run_id="run_test_1")

    out = store.get_run("run_test_1")
    assert out is not None
    assert out["status"] in {"complete", "partial"}
    assert out.get("gap_map")
    assert out.get("research_plan")
    assert isinstance(out.get("pull_results"), list)
    assert isinstance(out.get("ingest_results"), list)
    assert isinstance(out.get("fit_results"), list)

    events = store.list_events("run_test_1")
    stage_pairs = {(evt["stage"], evt["status"]) for evt in events}
    assert ("analyzing", "started") in stage_pairs
    assert ("planning", "completed") in stage_pairs
    assert ("pulling", "completed") in stage_pairs
    assert ("ingesting", "completed") in stage_pairs
