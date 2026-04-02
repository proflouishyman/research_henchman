"""API integration tests for run monitor and document click-through."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import main as orchestrator_main
from contracts import (
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
from store import OrchestratorStore


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
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setenv("ORCH_PULL_OUTPUT_ROOT", "pull_outputs")
    monkeypatch.setenv("ORCH_LIBRARY_SYSTEM", "generic")

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
    payload = docs_resp.json()
    docs = payload["documents"]
    packets = payload.get("packets", [])
    assert isinstance(packets, list)
    assert packets, "expected packet rows for document browser"
    assert "linked_documents" in packets[0]
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
    payload = catalog.json()
    universities = payload.get("university_databases", [])
    assert universities
    assert payload.get("library_system") == "generic"
    assert any(row.get("source_id") == "jstor" for row in universities)
    assert not any(row.get("source_id") == "proquest_historical_newspapers" for row in universities)
    assert all(isinstance(row.get("categories", []), list) for row in universities)


def test_documents_endpoint_extracts_linked_docs_from_json_packet(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    uploads = state_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    artifact_root = workspace / "pull_outputs" / "run_test_links" / "AUTO-01-G1" / "ebsco_api"
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_file = artifact_root / "packet.json"
    artifact_file.write_text(
        json.dumps([{"title": "JSTOR record", "url": "https://example.org/paper.pdf"}]),
        encoding="utf-8",
    )

    monkeypatch.setenv("ORCH_WORKSPACE", str(workspace))
    monkeypatch.setenv("ORCH_DATA_ROOT", str(state_dir))
    monkeypatch.setenv("ORCH_PULL_OUTPUT_ROOT", "pull_outputs")

    store = OrchestratorStore(state_dir)
    monkeypatch.setattr(orchestrator_main, "store", store)
    monkeypatch.setattr(orchestrator_main, "UPLOAD_DIR", uploads)

    rec = RunRecord(
        run_id="run_test_links",
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
                source_types=[SourceType.KEYED_API],
                preferred_sources=["ebsco_api"],
            ),
            results=[
                SourceResult(
                    source_id="ebsco_api",
                    source_type=SourceType.KEYED_API,
                    query="example query",
                    gap_id="AUTO-01-G1",
                    document_count=1,
                    run_dir=str(artifact_root),
                    artifact_type="json_records",
                    status="completed",
                )
            ],
            total_documents=1,
            sources_attempted=["ebsco_api"],
            sources_succeeded=["ebsco_api"],
            sources_failed=[],
            status="completed",
        )
    ]
    store.upsert_run(run_record_to_dict(rec))
    client = TestClient(orchestrator_main.app)

    docs_resp = client.get("/api/orchestrator/runs/run_test_links/documents")
    assert docs_resp.status_code == 200
    payload = docs_resp.json()
    docs = payload.get("documents", [])
    assert docs
    assert any(row.get("url") == "https://example.org/paper.pdf" for row in docs)
    assert any("quality_label" in row for row in docs)
    assert any("quality_rank" in row for row in docs)


def test_documents_endpoint_preserves_quality_metadata_and_order(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    uploads = state_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    artifact_root = workspace / "pull_outputs" / "run_test_quality" / "AUTO-01-G1" / "ebsco_api"
    artifact_root.mkdir(parents=True, exist_ok=True)
    (artifact_root / "matched_source.pdf").write_text("pdf", encoding="utf-8")
    artifact_file = artifact_root / "packet.json"
    artifact_file.write_text(
        json.dumps(
            [
                {
                    "title": "ebsco search results",
                    "url": "https://search.ebscohost.com/login.aspx?direct=true&bquery=test",
                    "quality_label": "seed",
                    "quality_rank": 20,
                },
                {
                    "title": "Matched Source",
                    "path": "matched_source.pdf",
                    "quality_label": "high",
                    "quality_rank": 100,
                },
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ORCH_WORKSPACE", str(workspace))
    monkeypatch.setenv("ORCH_DATA_ROOT", str(state_dir))
    monkeypatch.setenv("ORCH_PULL_OUTPUT_ROOT", "pull_outputs")

    store = OrchestratorStore(state_dir)
    monkeypatch.setattr(orchestrator_main, "store", store)
    monkeypatch.setattr(orchestrator_main, "UPLOAD_DIR", uploads)

    rec = RunRecord(
        run_id="run_test_quality",
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
                source_types=[SourceType.KEYED_API],
                preferred_sources=["ebsco_api"],
            ),
            results=[
                SourceResult(
                    source_id="ebsco_api",
                    source_type=SourceType.KEYED_API,
                    query="example query",
                    gap_id="AUTO-01-G1",
                    document_count=2,
                    run_dir=str(artifact_root),
                    artifact_type="json_records",
                    status="completed",
                )
            ],
            total_documents=2,
            sources_attempted=["ebsco_api"],
            sources_succeeded=["ebsco_api"],
            sources_failed=[],
            status="completed",
        )
    ]
    store.upsert_run(run_record_to_dict(rec))
    client = TestClient(orchestrator_main.app)

    docs_resp = client.get("/api/orchestrator/runs/run_test_quality/documents")
    assert docs_resp.status_code == 200
    docs = docs_resp.json().get("documents", [])
    assert docs
    assert docs[0].get("quality_label") == "high"
    assert "matched_source.pdf" in str(docs[0].get("path", ""))


def test_library_profiles_endpoint_lists_available_systems(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    uploads = state_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ORCH_WORKSPACE", str(workspace))
    monkeypatch.setenv("ORCH_DATA_ROOT", str(state_dir))
    monkeypatch.setenv("ORCH_LIBRARY_SYSTEM", "generic")

    store = OrchestratorStore(state_dir)
    monkeypatch.setattr(orchestrator_main, "store", store)
    monkeypatch.setattr(orchestrator_main, "UPLOAD_DIR", uploads)

    client = TestClient(orchestrator_main.app)
    resp = client.get("/api/orchestrator/library/profiles")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("library_system") == "generic"
    systems = payload.get("systems", [])
    assert any(row.get("key") == "generic" for row in systems)
    assert any(row.get("key") == "harvard" for row in systems)
    assert any(row.get("key") == "yale" for row in systems)
    assert any(row.get("key") == "stanford" for row in systems)
    assert any(row.get("key") == "nypl" for row in systems)
    assert all("database_count" in row for row in systems)


def test_connections_save_supports_blank_value_updates(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    uploads = state_dir / "uploads"
    workspace.mkdir(parents=True, exist_ok=True)
    uploads.mkdir(parents=True, exist_ok=True)

    env_path = workspace / ".env"
    env_path.write_text("FRED_API_KEY=abc123\n", encoding="utf-8")

    monkeypatch.setenv("ORCH_WORKSPACE", str(workspace))
    monkeypatch.setenv("ORCH_DATA_ROOT", str(state_dir))
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    store = OrchestratorStore(state_dir)
    monkeypatch.setattr(orchestrator_main, "store", store)
    monkeypatch.setattr(orchestrator_main, "UPLOAD_DIR", uploads)

    client = TestClient(orchestrator_main.app)
    save_resp = client.post("/api/orchestrator/connections/save", json={"updates": {"FRED_API_KEY": ""}})
    assert save_resp.status_code == 200

    raw = env_path.read_text(encoding="utf-8")
    assert "FRED_API_KEY=\n" in raw

    values_resp = client.get("/api/orchestrator/connections/values", params={"mask_secrets": "false"})
    assert values_resp.status_code == 200
    rows = values_resp.json().get("values", [])
    fred = next((row for row in rows if row.get("key") == "FRED_API_KEY"), None)
    assert fred is not None
    assert fred.get("raw_value") == ""
