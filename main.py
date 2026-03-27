"""FastAPI entrypoint for interactive orchestration app."""

from __future__ import annotations

import csv
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import OrchestratorSettings, load_runtime_env, required_connection_fields, write_env_updates
from .contracts import ConnectionSaveInput, ConnectionSchemaResponse, IntentCreateInput, RetryInput, RunCreateInput
from .pipeline import emit_event, run_orchestration
from .store import OrchestratorStore, now_utc


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"

store = OrchestratorStore(DATA_DIR)
app = FastAPI(title="Interactive Research Orchestrator", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _settings() -> OrchestratorSettings:
    """Reload .env-backed settings so UI config updates apply immediately."""
    workspace = Path(os.getenv("ORCH_WORKSPACE", str(BASE_DIR.parents[0]))).resolve()
    load_runtime_env(workspace)
    return OrchestratorSettings.from_env()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _search_plan_preview(workspace: Path, path_value: str, max_rows: int = 5) -> Dict[str, Any]:
    """Read a small preview of search plan CSV for UI confirmation."""
    if not path_value:
        return {"path": "", "exists": False, "rows": [], "row_count": 0}
    path = Path(path_value)
    if not path.is_absolute():
        path = (workspace / path).resolve()
    if not path.exists():
        return {"path": str(path), "exists": False, "rows": [], "row_count": 0}

    rows: List[Dict[str, str]] = []
    row_count = 0
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                row_count += 1
                if len(rows) < max_rows:
                    rows.append(dict(row))
    except Exception:
        return {"path": str(path), "exists": True, "rows": [], "row_count": 0, "error": "failed_to_read_csv"}
    return {"path": str(path), "exists": True, "rows": rows, "row_count": row_count}


@app.get("/api/orchestrator/health")
def api_health() -> Dict[str, Any]:
    settings = _settings()
    return {
        "status": "ok",
        "workspace": str(settings.workspace),
        "auto_ingest": settings.auto_ingest,
        "auto_llm_fit": settings.auto_llm_fit,
        "pull_mode_default": settings.pull_mode,
        "pull_provider_default": settings.pull_provider,
    }


@app.post("/api/orchestrator/intents")
def api_create_intent(inp: IntentCreateInput) -> Dict[str, Any]:
    settings = _settings()
    intent_id = _new_id("intent")
    rec = {
        "intent_id": intent_id,
        "input_mode": inp.input_mode,
        "manuscript_path": inp.manuscript_path,
        "search_plan_path": inp.search_plan_path,
        "gap_ids": inp.gap_ids,
        "max_queries": inp.max_queries,
        "notes": inp.notes,
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }
    rec["search_plan_preview"] = _search_plan_preview(settings.workspace, inp.search_plan_path)
    store.upsert_intent(rec)
    return rec


@app.get("/api/orchestrator/intents/{intent_id}")
def api_get_intent(intent_id: str) -> Dict[str, Any]:
    rec = store.get_intent(intent_id)
    if not rec:
        raise HTTPException(status_code=404, detail="intent not found")
    return rec


@app.get("/api/orchestrator/connections/schema", response_model=ConnectionSchemaResponse)
def api_connection_schema(
    mode: str = Query(default="auto"),
    provider: str = Query(default="ebscohost"),
) -> ConnectionSchemaResponse:
    fields = required_connection_fields(mode=mode, provider=provider)
    return ConnectionSchemaResponse(mode=mode, provider=provider, fields=fields)


@app.post("/api/orchestrator/connections/save")
def api_connection_save(inp: ConnectionSaveInput) -> Dict[str, Any]:
    settings = _settings()
    write_env_updates(settings.env_path, inp.updates)
    # Reload after write so values are active for subsequent calls.
    refreshed = _settings()
    masked = {}
    for key, value in inp.updates.items():
        if "PASSWORD" in key or "KEY" in key or "TOKEN" in key:
            masked[key] = "***"
        else:
            masked[key] = value
    return {
        "saved": True,
        "env_path": str(refreshed.env_path),
        "updates": masked,
    }


def _start_background_run(run_id: str) -> None:
    settings = _settings()
    thread = threading.Thread(
        target=run_orchestration,
        args=(store, settings),
        kwargs={"run_id": run_id},
        daemon=True,
    )
    thread.start()


@app.post("/api/orchestrator/runs")
def api_create_run(inp: RunCreateInput) -> Dict[str, Any]:
    if inp.intent_id:
        intent = store.get_intent(inp.intent_id)
        if not intent:
            raise HTTPException(status_code=404, detail="intent not found")

    run_id = _new_id("run")
    rec = {
        "run_id": run_id,
        "status": "queued",
        "stage": "queued",
        "payload": inp.model_dump(),
        "created_at": now_utc(),
        "updated_at": now_utc(),
        "result": {},
        "error": None,
    }
    store.upsert_run(rec)
    emit_event(store, run_id=run_id, stage="queued", status="queued", message="Run queued")
    _start_background_run(run_id)
    return rec


@app.get("/api/orchestrator/runs")
def api_list_runs(limit: int = 30) -> Dict[str, Any]:
    return {"runs": store.list_runs(limit=limit)}


@app.get("/api/orchestrator/runs/{run_id}")
def api_get_run(run_id: str) -> Dict[str, Any]:
    rec = store.get_run(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")
    return rec


@app.get("/api/orchestrator/runs/{run_id}/events")
def api_run_events(run_id: str, limit: int = 500) -> Dict[str, Any]:
    rec = store.get_run(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "events": store.list_events(run_id, limit=limit)}


@app.post("/api/orchestrator/runs/{run_id}/retry")
def api_retry_run(run_id: str, inp: RetryInput) -> Dict[str, Any]:
    rec = store.get_run(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="run not found")
    if rec.get("status") not in {"failed", "partial_completed", "completed"}:
        raise HTTPException(status_code=409, detail="run is already active")

    payload = rec.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    if inp.force:
        payload["force"] = True

    updated = store.upsert_run(
        {
            "run_id": run_id,
            "status": "queued",
            "stage": "queued",
            "payload": payload,
            "updated_at": now_utc(),
            "error": None,
        }
    )
    emit_event(store, run_id=run_id, stage="queued", status="queued", message="Retry queued")
    _start_background_run(run_id)
    return updated


@app.get("/", include_in_schema=False)
def root_index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)
