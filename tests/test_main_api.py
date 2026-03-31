"""API integration tests for run monitor and document click-through."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import main as orchestrator_main
from app.contracts import (
    GapPriority,
    GapPullResult,
    GapType,
    PlannedGap,
    RunRecord,
    RunStatus,
    SourceResult,
    SourceType,
    run_record_to_dict,
)
from app.store import OrchestratorStore


def test_documents_endpoint_and_file_clickthrough(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    uploads = state_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    artifact_root = workspace / "pull_outputs" / "run_test_api" / "AUTO-01-G1" / "world_bank"
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_file = artifact_root / "records.json"
    artifact_file.write_text(json.dumps([{"query": "example", "value": 1}]), encoding="utf-8")

    monkeypatch.setenv("ORCH_WORKSPACE", str(workspace))
    monkeypatch.setenv("ORCH_DATA_ROOT", str(state_dir))
    monkeypatch.setenv("ORCH_PULL_OUTPUT_ROOT", "pull_outputs")

    store = OrchestratorStore(state_dir)
    monkeypatch.setattr(orchestrator_main, "store", store)
    monkeypatch.setattr(orchestrator_main, "UPLOAD_DIR", uploads)

    rec = RunRecord(
        run_id="run_test_api",
        manuscript_path="Manuscript/sample.txt",
        status=RunStatus.COMPLETE,
        stage_detail="Run complete",
    )
    rec.pull_results = [
        GapPullResult(
            gap_id="AUTO-01-G1",
            planned_gap=PlannedGap(
                gap_id="AUTO-01-G1",
                chapter="Chapter One",
                claim_text="Claim",
                gap_type=GapType.IMPLICIT,
                priority=GapPriority.MEDIUM,
                search_queries=["example query"],
                source_types=[SourceType.FREE_API],
                preferred_sources=["world_bank"],
            ),
            results=[
                SourceResult(
                    source_id="world_bank",
                    source_type=SourceType.FREE_API,
                    query="example query",
                    gap_id="AUTO-01-G1",
                    document_count=1,
                    run_dir=str(artifact_root),
                    artifact_type="json_records",
                    status="completed",
                )
            ],
            total_documents=1,
            sources_attempted=["world_bank"],
            sources_succeeded=["world_bank"],
            sources_failed=[],
            status="completed",
        )
    ]

    store.upsert_run(run_record_to_dict(rec))

    client = TestClient(orchestrator_main.app)

    docs_resp = client.get("/api/orchestrator/runs/run_test_api/documents")
    assert docs_resp.status_code == 200
    docs = docs_resp.json()["documents"]
    assert docs, "expected at least one pulled artifact file"
    assert len({row["path"] for row in docs}) == len(docs), "expected deduped artifact paths"

    first_path = docs[0]["path"]
    file_resp = client.get("/api/orchestrator/files", params={"path": first_path})
    assert file_resp.status_code == 200
    assert file_resp.content

    blocked = client.get("/api/orchestrator/files", params={"path": "/etc/hosts"})
    assert blocked.status_code == 403

    catalog = client.get("/api/orchestrator/sources/catalog")
    assert catalog.status_code == 200
    universities = catalog.json().get("university_databases", [])
    assert universities
    assert any(row.get("source_id") == "jstor" for row in universities)
