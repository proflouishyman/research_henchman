"""FastAPI entrypoint for orchestrator v2."""

from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import OrchestratorSettings, load_runtime_env, read_env_values, write_env_updates
from .contracts import ConnectionSaveInput, RetryInput, RunCreateInput, RunRecord, RunStatus, run_record_from_dict, run_record_to_dict
from .layers.pull import SOURCE_REGISTRY, build_source_availability
from .pipeline import run_orchestration
from .store import OrchestratorStore, now_utc


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

store = OrchestratorStore(DATA_DIR)
app = FastAPI(title="Research Orchestrator", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

RUN_CREATE_LOCK = threading.Lock()
ACTIVE_RUN_STATUSES = {
    RunStatus.QUEUED.value,
    RunStatus.ANALYZING.value,
    RunStatus.PLANNING.value,
    RunStatus.PULLING.value,
    RunStatus.INGESTING.value,
    RunStatus.FITTING.value,
}


UNIVERSITY_DATABASES = [
    "Academic Search Ultimate",
    "Regional Business News",
    "EconLit with Full Text",
    "Business Source Complete",
    "JSTOR",
    "Project MUSE",
    "Statista",
]


def _settings() -> OrchestratorSettings:
    workspace = Path(os.getenv("ORCH_WORKSPACE", str(BASE_DIR.parents[0]))).resolve()
    load_runtime_env(workspace)
    settings = OrchestratorSettings.from_env()
    settings.data_root.mkdir(parents=True, exist_ok=True)
    settings.gap_map_cache_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


def _list_manuscripts(workspace: Path) -> List[Dict[str, str]]:
    allowed_ext = {".docx", ".md", ".txt", ".pdf"}
    rows: List[Dict[str, str]] = []

    manuscript_dir = workspace / "Manuscript"
    if manuscript_dir.exists():
        for path in sorted(manuscript_dir.glob("*")):
            if path.suffix.lower() not in allowed_ext:
                continue
            rows.append(
                {
                    "name": path.name,
                    "path": str(path.relative_to(workspace)),
                    "absolute_path": str(path),
                    "source": "workspace_manuscript",
                }
            )

    for path in sorted(UPLOAD_DIR.glob("*")):
        if path.suffix.lower() not in allowed_ext:
            continue
        rows.append(
            {
                "name": path.name,
                "path": str(path),
                "absolute_path": str(path),
                "source": "uploaded",
            }
        )

    return rows


def _resolve_manuscript_path(workspace: Path, manuscript_path: str) -> Path:
    raw = (manuscript_path or "").strip()
    if not raw:
        return Path("")
    p = Path(raw)
    return p.resolve() if p.is_absolute() else (workspace / p).resolve()


def _emit_event(run_id: str, stage: str, status: str, message: str, meta: Dict[str, Any] | None = None) -> None:
    store.append_event(
        {
            "event_id": f"evt_{run_id}_{now_utc()}",
            "run_id": run_id,
            "stage": stage,
            "status": status,
            "message": message,
            "meta": meta or {},
            "ts_utc": now_utc(),
        }
    )


def _parse_iso_utc(ts: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(ts).strip())
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reconcile_stale_runs(settings: OrchestratorSettings, scan_limit: int = 300) -> int:
    """Mark stale active runs as failed so new runs are never blocked indefinitely."""

    now = datetime.now(timezone.utc)
    changed = 0
    cutoff = max(600, int(settings.stale_stage_timeout_seconds))

    for row in store.list_runs(limit=max(1, scan_limit)):
        status = str(row.get("status", "")).strip()
        if status not in ACTIVE_RUN_STATUSES:
            continue

        run_id = str(row.get("run_id", "")).strip()
        if not run_id:
            continue

        updated = _parse_iso_utc(str(row.get("updated_at", "")))
        if not updated:
            continue

        age_seconds = int((now - updated).total_seconds())
        if age_seconds < cutoff:
            continue

        rec = run_record_from_dict(row)
        rec.status = RunStatus.FAILED
        rec.error = f"stale_run_watchdog: marked failed after {age_seconds}s"
        rec.updated_at = now_utc()
        store.upsert_run(run_record_to_dict(rec))
        _emit_event(
            run_id,
            stage="failed",
            status="failed",
            message=rec.error,
            meta={"reason": "stale_run_watchdog", "age_seconds": age_seconds, "cutoff_seconds": cutoff},
        )
        changed += 1

    return changed


def _latest_active_run() -> Dict[str, Any] | None:
    for row in store.list_runs(limit=100):
        if str(row.get("status", "")) in ACTIVE_RUN_STATUSES:
            return row
    return None


def _start_background_run(run_id: str) -> None:
    settings = _settings()
    thread = threading.Thread(
        target=run_orchestration,
        args=(store, settings),
        kwargs={"run_id": run_id},
        daemon=True,
    )
    thread.start()


def _mask_secret(key: str, value: str) -> str:
    if not re.search(r"(PASSWORD|KEY|TOKEN|SECRET)", key, flags=re.IGNORECASE):
        return value
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + ("*" * (len(value) - 4)) + value[-2:]


def _reset_for_stage(rec: RunRecord, from_stage: str) -> RunRecord:
    """Reset run outputs so retry can resume from a coarse stage boundary."""

    stage = (from_stage or "").strip().lower()
    if not stage:
        rec.gap_map = None
        rec.research_plan = None
        rec.pull_results = []
        rec.ingest_results = []
        rec.fit_results = []
        return rec

    if stage == "analyzing":
        rec.gap_map = None
        rec.research_plan = None
        rec.pull_results = []
        rec.ingest_results = []
        rec.fit_results = []
    elif stage == "planning":
        rec.research_plan = None
        rec.pull_results = []
        rec.ingest_results = []
        rec.fit_results = []
    elif stage == "pulling":
        rec.pull_results = []
        rec.ingest_results = []
        rec.fit_results = []
    elif stage == "ingesting":
        rec.ingest_results = []
        rec.fit_results = []
    elif stage == "fitting":
        rec.fit_results = []
    return rec


@app.get("/api/orchestrator/health")
def api_health() -> Dict[str, Any]:
    settings = _settings()
    availability = build_source_availability(settings)
    return {
        "status": "ok",
        "workspace": str(settings.workspace),
        "auto_ingest": settings.auto_ingest,
        "auto_llm_fit": settings.auto_llm_fit,
        "llm_backend": settings.llm_backend,
        "llm_model": settings.llm_model,
        "availability": {
            "free_apis": availability.free_apis,
            "keyed_apis": availability.keyed_apis,
            "playwright_sources": availability.playwright_sources,
            "missing_keys": availability.missing_keys,
            "playwright_unavailable_reason": availability.playwright_unavailable_reason,
        },
    }


@app.get("/api/orchestrator/manuscripts")
def api_manuscripts() -> Dict[str, Any]:
    settings = _settings()
    return {"workspace": str(settings.workspace), "manuscripts": _list_manuscripts(settings.workspace)}


@app.post("/api/orchestrator/manuscripts/upload")
async def api_upload_manuscript(file: UploadFile = File(...)) -> Dict[str, Any]:
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".docx", ".md", ".txt", ".pdf"}:
        raise HTTPException(status_code=400, detail="unsupported manuscript format")

    safe_name = f"{uuid.uuid4().hex[:10]}_{filename}"
    out_path = UPLOAD_DIR / safe_name
    out_path.write_bytes(await file.read())
    return {
        "uploaded": True,
        "name": filename,
        "stored_name": safe_name,
        "stored_path": str(out_path),
    }


@app.post("/api/orchestrator/runs")
def api_create_run(inp: RunCreateInput) -> Dict[str, Any]:
    settings = _settings()
    _reconcile_stale_runs(settings)

    manuscript_path = _resolve_manuscript_path(settings.workspace, inp.manuscript_path)
    if not manuscript_path.exists():
        raise HTTPException(status_code=400, detail="manuscript_path not found")

    with RUN_CREATE_LOCK:
        active = _latest_active_run()
        if active and not inp.force:
            reused = dict(active)
            reused["reused_active_run"] = True
            reused["message"] = "Active run already in progress; reused existing run."
            return reused

        run_id = _new_id("run")
        rec = RunRecord(
            run_id=run_id,
            manuscript_path=inp.manuscript_path,
            status=RunStatus.QUEUED,
            stage_detail="Queued",
            created_at=now_utc(),
            updated_at=now_utc(),
            force=inp.force,
            pull_timeout_seconds=max(10, int(inp.pull_timeout_seconds)),
        )
        payload = run_record_to_dict(rec)
        payload["reused_active_run"] = False
        store.upsert_run(payload)

        _emit_event(run_id, "queued", "queued", "Run queued")
        _start_background_run(run_id)
        return payload


@app.get("/api/orchestrator/runs")
def api_list_runs(limit: int = Query(default=30, ge=1, le=500)) -> Dict[str, Any]:
    _reconcile_stale_runs(_settings())
    return {"runs": store.list_runs(limit=limit)}


@app.get("/api/orchestrator/runs/{run_id}")
def api_get_run(run_id: str) -> Dict[str, Any]:
    _reconcile_stale_runs(_settings())
    row = store.get_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    return row


@app.get("/api/orchestrator/runs/{run_id}/events")
def api_run_events(run_id: str, limit: int = Query(default=500, ge=1, le=5000)) -> Dict[str, Any]:
    row = store.get_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "events": store.list_events(run_id, limit=limit)}


@app.post("/api/orchestrator/runs/{run_id}/retry")
def api_retry_run(run_id: str, inp: RetryInput) -> Dict[str, Any]:
    row = store.get_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run not found")

    current_status = str(row.get("status", "")).strip()
    if current_status in ACTIVE_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="run is already active")

    rec = run_record_from_dict(row)
    rec = _reset_for_stage(rec, inp.from_stage)
    rec.status = RunStatus.QUEUED
    rec.stage_detail = "Retry queued"
    rec.updated_at = now_utc()
    rec.error = ""
    if inp.force:
        rec.force = True

    store.upsert_run(run_record_to_dict(rec))
    _emit_event(run_id, "queued", "queued", "Retry queued", {"from_stage": inp.from_stage, "force": inp.force})
    _start_background_run(run_id)
    return store.get_run(run_id) or {}


@app.get("/api/orchestrator/connections/values")
def api_connection_values(mask_secrets: bool = Query(default=True)) -> Dict[str, Any]:
    settings = _settings()
    file_values = read_env_values(settings.env_path)

    keys = set(file_values.keys())
    for key in os.environ.keys():
        if key.startswith("ORCH_") or re.search(r"(API_KEY|TOKEN|PASSWORD|SECRET)", key):
            keys.add(key)

    rows = []
    for key in sorted(keys):
        value = str(os.environ.get(key, file_values.get(key, "")))
        rows.append(
            {
                "key": key,
                "value": _mask_secret(key, value) if mask_secrets else value,
                "raw_value": value if not mask_secrets else "",
                "is_secret": bool(re.search(r"(PASSWORD|KEY|TOKEN|SECRET)", key, flags=re.IGNORECASE)),
                "has_value": bool(value.strip()),
                "source": "process_env" if key in os.environ else ".env",
            }
        )
    return {"env_path": str(settings.env_path), "values": rows}


@app.post("/api/orchestrator/connections/save")
def api_connection_save(inp: ConnectionSaveInput) -> Dict[str, Any]:
    settings = _settings()
    write_env_updates(settings.env_path, inp.updates)
    refreshed = _settings()
    return {
        "saved": True,
        "env_path": str(refreshed.env_path),
        "updated_keys": sorted(inp.updates.keys()),
    }


@app.get("/api/orchestrator/sources/catalog")
def api_sources_catalog() -> Dict[str, Any]:
    settings = _settings()
    availability = build_source_availability(settings)

    free = []
    keyed = []
    playwright = []
    for source_id, adapter in SOURCE_REGISTRY.items():
        row = {
            "source_id": source_id,
            "source_type": str(adapter.source_type),
            "configured": True,
            "available": adapter.is_available(availability),
            "validation": adapter.validate(availability),
        }
        if adapter.source_type.value == "free_api":
            free.append(row)
        elif adapter.source_type.value == "keyed_api":
            row["env_key"] = str(getattr(adapter, "env_key", ""))
            row["configured"] = source_id in availability.keyed_apis
            keyed.append(row)
        else:
            playwright.append(row)

    return {
        "workspace": str(settings.workspace),
        "free_apis": sorted(free, key=lambda row: row["source_id"]),
        "closed_apis": sorted(keyed, key=lambda row: row["source_id"]),
        "playwright_sources": sorted(playwright, key=lambda row: row["source_id"]),
        "university_databases": [{"name": name, "provider": "library"} for name in UNIVERSITY_DATABASES],
    }


@app.get("/", include_in_schema=False)
def root_index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)
