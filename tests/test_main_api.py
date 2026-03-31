"""API integration tests for run monitor and document click-through."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import main as orchestrator_main
from app.adapters.base import PullAdapter
from app.contracts import SourceAvailability, SourceResult, SourceType
from app.layers import pull
from app.store import OrchestratorStore


class _FakeSource(PullAdapter):
    source_id = "world_bank"
    source_type = SourceType.FREE_API

    def is_available(self, availability: SourceAvailability) -> bool:
        return self.source_id in availability.free_apis

    def pull(self, gap, query, run_dir, timeout_seconds=60):
        root = Path(run_dir) / gap.gap_id / self.source_id
        root.mkdir(parents=True, exist_ok=True)
        out = root / "records.json"
        out.write_text(json.dumps([{"query": query, "value": 1}]), encoding="utf-8")
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


def test_documents_endpoint_and_file_clickthrough(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    manuscript_dir = workspace / "Manuscript"
    manuscript_dir.mkdir(parents=True, exist_ok=True)
    (manuscript_dir / "sample.txt").write_text(
        "Chapter One\nTODO source this claim.\nLikely outcomes are significant without citation.",
        encoding="utf-8",
    )

    state_dir = tmp_path / "state"
    uploads = state_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ORCH_WORKSPACE", str(workspace))
    monkeypatch.setenv("ORCH_DATA_ROOT", str(state_dir))
    monkeypatch.setenv("ORCH_GAP_ANALYSIS_USE_OLLAMA", "false")
    monkeypatch.setenv("ORCH_REFLECTION_USE_OLLAMA", "false")
    monkeypatch.setenv("ORCH_AUTO_INGEST", "true")
    monkeypatch.setenv("ORCH_AUTO_LLM_FIT", "false")
    monkeypatch.setenv("ORCH_PULL_OUTPUT_ROOT", "pull_outputs")

    monkeypatch.setattr(orchestrator_main, "store", OrchestratorStore(state_dir))
    monkeypatch.setattr(orchestrator_main, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(pull, "SOURCE_REGISTRY", {"world_bank": _FakeSource()})
    monkeypatch.setattr(pull, "check_cdp_endpoint", lambda _url: "disabled")

    def _run_inline(run_id: str) -> None:
        orchestrator_main.run_orchestration(orchestrator_main.store, orchestrator_main._settings(), run_id=run_id)

    monkeypatch.setattr(orchestrator_main, "_start_background_run", _run_inline)

    client = TestClient(orchestrator_main.app)
    create = client.post(
        "/api/orchestrator/runs",
        json={"manuscript_path": "Manuscript/sample.txt", "force": True, "pull_timeout_seconds": 30},
    )
    assert create.status_code == 200
    run_id = create.json()["run_id"]

    run_resp = client.get(f"/api/orchestrator/runs/{run_id}")
    assert run_resp.status_code == 200
    assert run_resp.json()["status"] in {"complete", "partial"}

    docs_resp = client.get(f"/api/orchestrator/runs/{run_id}/documents")
    assert docs_resp.status_code == 200
    docs = docs_resp.json()["documents"]
    assert docs, "expected at least one pulled artifact file"
    assert len({row["path"] for row in docs}) == len(docs), "expected deduped artifact paths"

    first_path = docs[0]["path"]
    file_resp = client.get("/api/orchestrator/files", params={"path": first_path})
    assert file_resp.status_code == 200
    assert file_resp.content

    # File endpoint should reject paths outside approved roots.
    blocked = client.get("/api/orchestrator/files", params={"path": "/etc/hosts"})
    assert blocked.status_code == 403
