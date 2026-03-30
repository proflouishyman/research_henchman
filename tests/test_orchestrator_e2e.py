"""End-to-end orchestrator tests for manuscript parsing and stage chaining."""

from __future__ import annotations

import json
import sys
import textwrap
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import main as orchestrator_main
from app.store import OrchestratorStore


def _write_minimal_docx(path: Path, paragraphs: list[str]) -> None:
    """Create a minimal .docx file that the manuscript parser can read."""
    body_xml = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        for text in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_xml}</w:body>"
        "</w:document>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def _write_stage_scripts(workspace: Path) -> None:
    """Write deterministic fake pull/ingest/llm scripts for stable E2E tests."""
    scripts_dir = workspace / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    pull_script = textwrap.dedent(
        """
        import argparse
        import json
        from pathlib import Path

        parser = argparse.ArgumentParser()
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--provider", default="ebscohost")
        parser.add_argument("--mode", default="api")
        args = parser.parse_args()

        workspace = Path(args.workspace)
        run_id = "test_run_001"
        run_dir = workspace / "tmp_artifacts" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        stage_dir = workspace / "stage_markers"
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "pull.txt").write_text(f"{args.provider}|{args.mode}", encoding="utf-8")

        payload = {
            "run_id": run_id,
            "provider": args.provider,
            "run_dir": str(run_dir.relative_to(workspace)),
            "artifact_type": "external_packet",
            "status": "completed",
            "stats": {"api_calls": 3, "request_count": 3},
        }
        print(json.dumps(payload))
        """
    ).strip()

    ingest_script = textwrap.dedent(
        """
        import argparse
        from pathlib import Path

        parser = argparse.ArgumentParser()
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--run-id", required=True)
        args = parser.parse_args()

        marker = Path(args.workspace) / "stage_markers" / "ingest.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(args.run_id, encoding="utf-8")
        print("ingest completed")
        """
    ).strip()

    llm_script = textwrap.dedent(
        """
        import argparse
        from pathlib import Path

        parser = argparse.ArgumentParser()
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--model", default="")
        parser.add_argument("--gap-id", default="")
        args, _ = parser.parse_known_args()

        marker = Path(args.workspace) / "stage_markers" / "llm.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"model={args.model};gap={args.gap_id}", encoding="utf-8")
        print("llm fit completed")
        """
    ).strip()

    (scripts_dir / "fake_pull.py").write_text(pull_script + "\n", encoding="utf-8")
    (scripts_dir / "fake_ingest.py").write_text(ingest_script + "\n", encoding="utf-8")
    (scripts_dir / "fake_llm.py").write_text(llm_script + "\n", encoding="utf-8")


def _configure_workspace_env(workspace: Path, monkeypatch) -> None:
    """Populate orchestrator env so run preflight and stage scripts are executable."""
    pull_cmd = f'{sys.executable} "{workspace / "scripts" / "fake_pull.py"}" --workspace "{{workspace}}" --provider "{{provider}}" --mode "{{mode}}"'
    env_values = {
        "ORCH_WORKSPACE": str(workspace),
        "ORCH_PULL_PROVIDER": "ebscohost",
        "ORCH_PULL_MODE": "api",
        "ORCH_AUTO_INGEST": "true",
        "ORCH_AUTO_LLM_FIT": "true",
        "ORCH_API_PULL_COMMAND": pull_cmd,
        "ORCH_INGEST_EBSCO_SCRIPT": "scripts/fake_ingest.py",
        "ORCH_INGEST_EXTERNAL_SCRIPT": "scripts/fake_ingest.py",
        "ORCH_LLM_SCRIPT": "scripts/fake_llm.py",
        "ORCH_LLM_BACKEND": "ollama",
        "ORCH_LLM_MODEL": "qwen2.5:32b",
        "ORCH_OLLAMA_BASE_URL": "http://127.0.0.1:11434",
        "ORCH_GAP_ANALYSIS_USE_OLLAMA": "false",
    }
    for key, value in env_values.items():
        monkeypatch.setenv(key, value)

    env_lines = [f"{key}={value}" for key, value in env_values.items()]
    (workspace / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")


def _sync_background_runner(monkeypatch, run_inline: bool = True) -> None:
    """Control background execution style for deterministic API tests."""
    if not run_inline:
        monkeypatch.setattr(orchestrator_main, "_start_background_run", lambda run_id: None)
        return

    def _run_inline(run_id: str) -> None:
        orchestrator_main.run_orchestration(orchestrator_main.store, orchestrator_main._settings(), run_id=run_id)

    monkeypatch.setattr(orchestrator_main, "_start_background_run", _run_inline)


def _setup_test_client(tmp_path: Path, monkeypatch, run_inline: bool = True) -> tuple[TestClient, Path]:
    """Build isolated workspace/store paths and a patched FastAPI test client."""
    workspace = tmp_path / "workspace"
    (workspace / "Manuscript").mkdir(parents=True, exist_ok=True)
    _write_stage_scripts(workspace)
    _configure_workspace_env(workspace, monkeypatch)

    test_data_dir = tmp_path / "orchestrator_state"
    uploads_dir = test_data_dir / "uploads"
    gap_dir = test_data_dir / "gap_maps"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    gap_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(orchestrator_main, "store", OrchestratorStore(test_data_dir))
    monkeypatch.setattr(orchestrator_main, "UPLOAD_DIR", uploads_dir)
    monkeypatch.setattr(orchestrator_main, "GAP_MAP_DIR", gap_dir)
    _sync_background_runner(monkeypatch, run_inline=run_inline)
    return TestClient(orchestrator_main.app), workspace


def test_manuscript_gap_layout_reads_docx_and_generates_map(tmp_path, monkeypatch) -> None:
    """Gap endpoint should parse manuscript text and emit non-placeholder gap analysis."""
    client, workspace = _setup_test_client(tmp_path, monkeypatch)
    manuscript_rel = "Manuscript/chapter_one.docx"
    manuscript_path = workspace / manuscript_rel
    _write_minimal_docx(
        manuscript_path,
        [
            "Chapter One: Merchant",
            "John McDonogh sold his cargo quickly in New Orleans.",
            "The manuscript needs stronger citations on this claim.",
        ],
    )

    response = client.get(
        "/api/orchestrator/gaps/layout",
        params={"manuscript_path": manuscript_rel, "refresh": "true"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["chapter_count"] >= 1
    assert payload["gap_count"] >= 1
    assert payload["generated"] is True
    assert payload["reason"] in {
        "generated_missing_map",
        "regenerated_placeholder_map",
        "regenerated_missing_metadata",
        "regenerated_analysis_upgrade",
    }
    extraction = payload.get("extraction", {})
    assert extraction.get("status") == "ok"
    assert int(extraction.get("char_count", 0)) > 0
    assert extraction.get("analysis_method") in {"heuristic", "ollama"}
    assert "Manuscript Read Status" not in json.dumps(payload)


def test_run_pipeline_executes_pull_ingest_and_llm_with_event_metadata(tmp_path, monkeypatch) -> None:
    """Run endpoint should execute all stages and expose detailed progress events."""
    client, workspace = _setup_test_client(tmp_path, monkeypatch)
    manuscript_rel = "Manuscript/chapter_one.docx"
    _write_minimal_docx(
        workspace / manuscript_rel,
        [
            "Chapter One: Merchant",
            "Claim about business acumen without citation.",
            "Claim about smuggling without citation.",
        ],
    )

    intent_resp = client.post(
        "/api/orchestrator/intents",
        json={
            "input_mode": "manuscript",
            "manuscript_path": manuscript_rel,
            "search_plan_path": "",
            "gap_ids": ["AUTO-01-G1"],
            "max_queries": 25,
            "notes": "test-run",
        },
    )
    assert intent_resp.status_code == 200
    intent_id = intent_resp.json()["intent_id"]

    run_resp = client.post(
        "/api/orchestrator/runs",
        json={
            "intent_id": intent_id,
            "pull_mode": "api",
            "pull_provider": "ebscohost",
            "artifact_type": "external_packet",
            "gap_id": "AUTO-01-G1",
            "force": False,
        },
    )
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run_id"]

    run_state = client.get(f"/api/orchestrator/runs/{run_id}")
    assert run_state.status_code == 200
    run_payload = run_state.json()
    assert run_payload["status"] == "completed"
    assert run_payload["stage"] == "completed"

    result = run_payload.get("result", {})
    assert result.get("artifact", {}).get("run_id") == "test_run_001"
    assert result.get("ingest", {}).get("run_id") == "test_run_001"
    assert result.get("llm_fit", {}).get("llm_model") == "qwen2.5:32b"

    events_resp = client.get(f"/api/orchestrator/runs/{run_id}/events")
    assert events_resp.status_code == 200
    events = events_resp.json().get("events", [])
    assert events, "expected run events to be emitted"

    stage_statuses = {(evt.get("stage"), evt.get("status")) for evt in events}
    expected = {
        ("queued", "queued"),
        ("validating_config", "started"),
        ("validating_config", "completed"),
        ("planning", "started"),
        ("planning", "completed"),
        ("pulling", "started"),
        ("pulling", "completed"),
        ("ingesting", "started"),
        ("ingesting", "completed"),
        ("llm_processing", "started"),
        ("llm_processing", "completed"),
    }
    assert expected.issubset(stage_statuses)

    pulling_completed = [evt for evt in events if evt.get("stage") == "pulling" and evt.get("status") == "completed"]
    assert pulling_completed
    pull_meta = pulling_completed[-1].get("meta", {})
    assert pull_meta.get("run_dir")
    assert pull_meta.get("artifact_type") == "external_packet"
    assert int(pull_meta.get("stats", {}).get("api_calls", 0)) == 3

    # Marker files prove the downstream stage scripts executed in order.
    stage_dir = workspace / "stage_markers"
    assert (stage_dir / "pull.txt").exists()
    assert (stage_dir / "ingest.txt").read_text(encoding="utf-8").strip() == "test_run_001"
    llm_text = (stage_dir / "llm.txt").read_text(encoding="utf-8")
    assert "model=qwen2.5:32b" in llm_text
    assert "gap=AUTO-01-G1" in llm_text


def test_create_run_reuses_existing_active_run(tmp_path, monkeypatch) -> None:
    """Run creation should reuse active run when force is false."""
    client, _ = _setup_test_client(tmp_path, monkeypatch, run_inline=False)

    first = client.post(
        "/api/orchestrator/runs",
        json={
            "intent_id": "",
            "pull_mode": "api",
            "pull_provider": "ebscohost",
            "artifact_type": "external_packet",
            "gap_id": "",
            "force": False,
        },
    )
    assert first.status_code == 200
    first_run = first.json()
    assert first_run["status"] == "queued"
    assert first_run["reused_active_run"] is False

    second = client.post(
        "/api/orchestrator/runs",
        json={
            "intent_id": "",
            "pull_mode": "api",
            "pull_provider": "ebscohost",
            "artifact_type": "external_packet",
            "gap_id": "",
            "force": False,
        },
    )
    assert second.status_code == 200
    second_run = second.json()
    assert second_run["run_id"] == first_run["run_id"]
    assert second_run["reused_active_run"] is True

    runs = client.get("/api/orchestrator/runs").json()["runs"]
    assert len(runs) == 1


def test_stale_run_watchdog_marks_orphan_before_new_run(tmp_path, monkeypatch) -> None:
    """Stale active run should be marked failed and not block a new run."""
    client, _ = _setup_test_client(tmp_path, monkeypatch, run_inline=False)
    stale_run_id = "run_stale_123"
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    orchestrator_main.store.upsert_run(
        {
            "run_id": stale_run_id,
            "status": "ingesting",
            "stage": "ingesting",
            "payload": {},
            "created_at": stale_ts,
            "updated_at": stale_ts,
            "result": {},
            "error": None,
        }
    )

    created = client.post(
        "/api/orchestrator/runs",
        json={
            "intent_id": "",
            "pull_mode": "api",
            "pull_provider": "ebscohost",
            "artifact_type": "external_packet",
            "gap_id": "",
            "force": False,
        },
    )
    assert created.status_code == 200
    new_run = created.json()
    assert new_run["run_id"] != stale_run_id
    assert new_run["reused_active_run"] is False

    stale = client.get(f"/api/orchestrator/runs/{stale_run_id}")
    assert stale.status_code == 200
    stale_payload = stale.json()
    assert stale_payload["status"] == "failed"
    assert "stale_run_watchdog" in str(stale_payload.get("error", ""))

    stale_events = client.get(f"/api/orchestrator/runs/{stale_run_id}/events").json().get("events", [])
    assert any(evt.get("meta", {}).get("reason") == "stale_run_watchdog" for evt in stale_events)


def test_ingest_stage_skips_when_artifact_run_already_ingested(tmp_path, monkeypatch) -> None:
    """Pipeline should skip ingest stage when ingest registry already contains artifact run_id."""
    client, workspace = _setup_test_client(tmp_path, monkeypatch, run_inline=True)
    ingest_registry = workspace / "codex" / "evidence_hub" / "data" / "ingest_runs.json"
    ingest_registry.parent.mkdir(parents=True, exist_ok=True)
    ingest_registry.write_text(
        json.dumps(
            {
                "test_run_001": {
                    "run_id": "test_run_001",
                    "provider": "ebscohost",
                    "run_dir": str(workspace / "tmp_artifacts" / "test_run_001"),
                    "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "manifest_rows": 10,
                    "result_rows": 10,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    run_resp = client.post(
        "/api/orchestrator/runs",
        json={
            "intent_id": "",
            "pull_mode": "api",
            "pull_provider": "ebscohost",
            "artifact_type": "external_packet",
            "gap_id": "",
            "force": False,
        },
    )
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run_id"]

    run_state = client.get(f"/api/orchestrator/runs/{run_id}")
    assert run_state.status_code == 200
    run_payload = run_state.json()
    assert run_payload["status"] == "completed"

    events = client.get(f"/api/orchestrator/runs/{run_id}/events").json().get("events", [])
    ingest_events = [evt for evt in events if evt.get("stage") == "ingesting"]
    assert any(evt.get("status") == "skipped" for evt in ingest_events)
    skip_evt = next(evt for evt in ingest_events if evt.get("status") == "skipped")
    assert skip_evt.get("meta", {}).get("reason") == "already_ingested"

    # Ingest script marker is absent because stage was skipped.
    assert not (workspace / "stage_markers" / "ingest.txt").exists()


def test_strategy_preview_returns_summary_sources_queries_and_checklist(tmp_path, monkeypatch) -> None:
    """Strategy preview endpoint should expose inspectable run plan details for UI rendering."""
    client, workspace = _setup_test_client(tmp_path, monkeypatch, run_inline=False)
    manuscript_rel = "Manuscript/strategy_input.docx"
    _write_minimal_docx(
        workspace / manuscript_rel,
        [
            "Chapter One: Merchant",
            "Claim about French and Spanish language mastery lacks citation.",
            "Claim about smuggling prevalence lacks primary-source grounding.",
        ],
    )

    # Ensure gap layout exists so strategy query extraction can map to generated gaps.
    layout = client.get("/api/orchestrator/gaps/layout", params={"manuscript_path": manuscript_rel, "refresh": "true"})
    assert layout.status_code == 200
    assert layout.json().get("gap_count", 0) >= 1

    preview = client.post(
        "/api/orchestrator/strategy/preview",
        json={
            "manuscript_path": manuscript_rel,
            "pull_mode": "auto",
            "pull_provider": "ebscohost",
            "strategy_mode": "automatic",
            "narrow_question": "",
            "target_gap_id": "",
        },
    )
    assert preview.status_code == 200
    out = preview.json()
    assert out.get("summary")
    assert out.get("summary_method") in {"fallback", "ollama"}
    assert isinstance(out.get("sources"), list) and len(out["sources"]) >= 1
    assert isinstance(out.get("queries"), list) and len(out["queries"]) >= 1
    assert isinstance(out.get("checklist"), list) and len(out["checklist"]) >= 4
