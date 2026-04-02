"""FastAPI entrypoint for orchestrator v2."""

from __future__ import annotations

import json
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

from config import OrchestratorSettings, load_runtime_env, read_env_values, write_env_updates
from contracts import ConnectionSaveInput, RetryInput, RunCreateInput, RunRecord, RunStatus, run_record_from_dict, run_record_to_dict
from library_profiles import get_active_library_profile, get_active_university_databases, load_library_profiles
from layers.pull import SOURCE_REGISTRY, build_source_availability, source_capability_catalog
from pipeline import run_orchestration
from store import OrchestratorStore, now_utc


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
_DOC_EXTENSIONS = {".pdf", ".html", ".htm", ".txt", ".md", ".csv", ".json"}
_TITLE_KEYS = {"title", "name", "headline", "record_title", "document_title"}


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


def _allowed_file_roots(settings: OrchestratorSettings) -> List[Path]:
    """Return root directories allowed for file click-through serving."""

    return [settings.workspace.resolve(), settings.data_root.resolve(), UPLOAD_DIR.resolve(), settings.pull_output_root.resolve()]


def _safe_file_path(settings: OrchestratorSettings, raw_path: str) -> Path:
    """Resolve file path and enforce that it stays within approved roots."""

    candidate = Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (settings.workspace / candidate).resolve()
    allowed = _allowed_file_roots(settings)
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise HTTPException(status_code=403, detail="path outside allowed roots")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return resolved


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


def _record_title(row: Dict[str, Any]) -> str:
    for key in _TITLE_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_external_link(key: str, value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith(("http://", "https://")):
        return raw

    # Convert DOI-like values into clickable links.
    doi = raw
    if raw.lower().startswith("doi:"):
        doi = raw.split(":", 1)[1].strip()
    if "doi" in key.lower() or re.match(r"^10\.\d{4,9}/\S+$", doi):
        return f"https://doi.org/{doi}"
    return ""


def _resolve_local_artifact(packet_dir: Path, value: str) -> Path | None:
    raw = str(value or "").strip().strip('"').strip("'")
    if not raw:
        return None
    if raw.lower().startswith(("http://", "https://")):
        return None
    suffix = Path(raw).suffix.lower()
    if suffix not in _DOC_EXTENSIONS:
        return None

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (packet_dir / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def _extract_linked_documents_from_json(json_path: Path, max_docs: int = 40) -> List[Dict[str, Any]]:
    """Extract likely document links/paths from JSON artifact packets."""

    try:
        payload = json.loads(json_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _link_rank(row: Dict[str, Any]) -> tuple[int, str]:
        # Explicit quality from adapter rows wins.
        raw_rank = row.get("quality_rank")
        try:
            if raw_rank is not None:
                return (int(str(raw_rank)), str(row.get("quality_label", "")))
        except Exception:
            pass

        path = str(row.get("path", "")).strip()
        url = str(row.get("url", "")).strip().lower()
        source_key = str(row.get("source_key", "")).strip().lower()
        link_type = str(row.get("link_type", "")).strip().lower()

        if path:
            ext = Path(path).suffix.lower()
            if ext == ".pdf":
                return (100, "high")
            return (88, "high")

        if "doi.org/" in url or source_key == "doi":
            return (84, "medium")
        if url.endswith(".pdf") or ".pdf?" in url:
            return (82, "medium")
        if link_type == "provider_search" or "search" in source_key:
            return (20, "seed")
        return (58, "medium")

    def _append(row: Dict[str, Any]) -> None:
        key = row.get("url") or row.get("path") or row.get("name") or row.get("title")
        stable = str(key or "").strip().lower()
        if not stable or stable in seen:
            return
        seen.add(stable)
        rank, label = _link_rank(row)
        row["quality_rank"] = rank
        row["quality_label"] = label
        out.append(row)

    def _walk(node: Any, parent: Dict[str, Any] | None = None, depth: int = 0) -> None:
        if depth > 7 or len(out) >= max_docs:
            return
        if isinstance(node, dict):
            # If adapter already emitted normalized link rows, preserve that row
            # and its explicit quality metadata instead of reconstructing from keys.
            direct_url = str(node.get("url", "")).strip()
            direct_path = str(node.get("path", "")).strip()
            if direct_url or direct_path:
                title = _record_title(node) or _record_title(parent or {}) or "document"
                row: Dict[str, Any] = {
                    "title": title,
                    "source_key": str(node.get("source_key", "")).strip(),
                    "kind": str(node.get("kind", "")).strip() or ("external" if direct_url else "local"),
                }
                if direct_url.lower().startswith(("http://", "https://")):
                    row["url"] = direct_url
                if direct_path:
                    local = _resolve_local_artifact(json_path.parent, direct_path)
                    if local is not None:
                        row["path"] = str(local)
                        row["name"] = local.name
                if node.get("quality_rank") is not None:
                    row["quality_rank"] = node.get("quality_rank")
                if node.get("quality_label") is not None:
                    row["quality_label"] = node.get("quality_label")
                if node.get("link_type") is not None:
                    row["link_type"] = node.get("link_type")
                _append(row)
                # Continue scanning in case the row also includes nested content.

            title = _record_title(node) or _record_title(parent or {})
            for key, value in node.items():
                if len(out) >= max_docs:
                    return
                if isinstance(value, str):
                    ext = _normalize_external_link(str(key), value)
                    if ext:
                        _append({"kind": "external", "title": title or str(key), "url": ext, "source_key": str(key)})
                        continue
                    local = _resolve_local_artifact(json_path.parent, value)
                    if local is not None:
                        _append(
                            {
                                "kind": "local",
                                "title": title or local.name,
                                "path": str(local),
                                "name": local.name,
                                "source_key": str(key),
                            }
                        )
                elif isinstance(value, (dict, list)):
                    _walk(value, node, depth + 1)
        elif isinstance(node, list):
            for item in node:
                if len(out) >= max_docs:
                    return
                _walk(item, parent, depth + 1)

    _walk(payload)
    out.sort(
        key=lambda row: (
            int(row.get("quality_rank", 0)),
            str(row.get("title", "")),
        ),
        reverse=True,
    )
    return out


def _run_document_packets(settings: OrchestratorSettings, rec_row: Dict[str, Any], max_files: int = 600) -> List[Dict[str, Any]]:
    """Build artifact packets with extracted linked documents for Results UI."""

    rec = run_record_from_dict(rec_row)
    packets: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()
    for gap_result in rec.pull_results:
        gap_id = gap_result.gap_id
        for source_result in gap_result.results:
            run_dir = Path(str(source_result.run_dir or "")).expanduser()
            if not run_dir.is_absolute():
                run_dir = (settings.workspace / run_dir).resolve()
            if not run_dir.exists() or not run_dir.is_dir():
                continue

            source_id = source_result.source_id
            query = source_result.query
            for file_path in sorted(run_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                path_str = str(file_path.resolve())
                # Multiple query results can point at the same adapter folder.
                # Keep each artifact path only once in the click-through list.
                if path_str in seen_paths:
                    continue
                seen_paths.add(path_str)
                linked_documents: List[Dict[str, Any]]
                if file_path.suffix.lower() == ".json":
                    linked_documents = _extract_linked_documents_from_json(file_path, max_docs=40)
                else:
                    ext = file_path.suffix.lower()
                    quality_rank = 100 if ext == ".pdf" else 88
                    linked_documents = [
                        {
                            "kind": "local",
                            "title": file_path.name,
                            "path": path_str,
                            "name": file_path.name,
                            "quality_rank": quality_rank,
                            "quality_label": "high",
                        }
                    ]
                packets.append(
                    {
                        "gap_id": gap_id,
                        "source_id": source_id,
                        "query": query,
                        "artifact_type": source_result.artifact_type,
                        "path": path_str,
                        "name": file_path.name,
                        "size_bytes": file_path.stat().st_size,
                        "linked_documents": linked_documents,
                    }
                )
                if len(packets) >= max_files:
                    return packets
    return packets


def _run_document_rows(settings: OrchestratorSettings, rec_row: Dict[str, Any], max_files: int = 600) -> List[Dict[str, Any]]:
    """Build legacy flattened click-through rows (derived from packet links)."""

    packets = _run_document_packets(settings, rec_row, max_files=max_files)
    rows: List[Dict[str, Any]] = []
    for packet in packets:
        linked = packet.get("linked_documents", [])
        if isinstance(linked, list) and linked:
            for item in linked:
                if not isinstance(item, dict):
                    continue
                row = {
                    "gap_id": packet.get("gap_id", ""),
                    "source_id": packet.get("source_id", ""),
                    "query": packet.get("query", ""),
                    "artifact_type": packet.get("artifact_type", ""),
                    "name": item.get("name") or item.get("title") or "document",
                    "kind": item.get("kind", ""),
                    "quality_rank": item.get("quality_rank", 0),
                    "quality_label": item.get("quality_label", ""),
                }
                if item.get("path"):
                    row["path"] = item.get("path")
                if item.get("url"):
                    row["url"] = item.get("url")
                rows.append(row)
                if len(rows) >= max_files:
                    return rows
            continue

        # Fallback for packets with no extracted links: keep raw artifact row.
        rows.append(
            {
                "gap_id": packet.get("gap_id", ""),
                "source_id": packet.get("source_id", ""),
                "query": packet.get("query", ""),
                "artifact_type": packet.get("artifact_type", ""),
                "path": packet.get("path", ""),
                "name": packet.get("name", ""),
                "raw_packet": True,
                "kind": "local",
                "quality_rank": 5,
                "quality_label": "seed",
            }
        )
        if len(rows) >= max_files:
            return rows
    rows.sort(
        key=lambda row: (
            int(str(row.get("quality_rank", 0)) or "0"),
            str(row.get("name", "")),
        ),
        reverse=True,
    )
    return rows


@app.get("/api/orchestrator/health")
def api_health() -> Dict[str, Any]:
    settings = _settings()
    availability = build_source_availability(settings)
    profile = get_active_library_profile(settings)
    return {
        "status": "ok",
        "workspace": str(settings.workspace),
        "library_system": str(profile.get("key", settings.library_system)),
        "library_name": str(profile.get("name", "")),
        "library_profiles_path": str(settings.library_profiles_path),
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


@app.get("/api/orchestrator/runs/{run_id}/documents")
def api_run_documents(run_id: str, limit: int = Query(default=300, ge=1, le=2000)) -> Dict[str, Any]:
    """List pulled artifact files for run-complete click-through in UI."""

    row = store.get_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    settings = _settings()
    packets = _run_document_packets(settings, row, max_files=limit)
    docs = _run_document_rows(settings, row, max_files=limit)
    linked_total = sum(len(packet.get("linked_documents", [])) for packet in packets if isinstance(packet.get("linked_documents"), list))
    return {"run_id": run_id, "documents": docs, "packets": packets, "linked_document_count": linked_total}


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
    # Ensure edited keys are re-read from `.env` on the next settings load.
    for key in inp.updates.keys():
        if key in os.environ:
            os.environ.pop(key, None)
    refreshed = _settings()
    return {
        "saved": True,
        "env_path": str(refreshed.env_path),
        "updated_keys": sorted(inp.updates.keys()),
    }


@app.get("/api/orchestrator/library/profiles")
def api_library_profiles() -> Dict[str, Any]:
    settings = _settings()
    payload = load_library_profiles(settings)
    systems = payload.get("systems", {}) if isinstance(payload, dict) else {}
    if not isinstance(systems, dict):
        systems = {}

    rows = []
    for key, value in sorted(systems.items(), key=lambda item: str(item[0])):
        if not isinstance(value, dict):
            continue
        db_rows = value.get("databases", [])
        db_count = (
            len([row for row in db_rows if isinstance(row, dict) and str(row.get("source_id", "")).strip()])
            if isinstance(db_rows, list)
            else 0
        )
        rows.append(
            {
                "key": str(key).strip().lower(),
                "name": str(value.get("name", key)).strip() or str(key),
                "database_count": db_count,
            }
        )

    active = get_active_library_profile(settings)
    return {
        "library_system": str(active.get("key", settings.library_system)),
        "library_name": str(active.get("name", "")),
        "systems": rows,
    }


@app.get("/api/orchestrator/sources/catalog")
def api_sources_catalog() -> Dict[str, Any]:
    settings = _settings()
    availability = build_source_availability(settings)
    caps = source_capability_catalog(settings)
    profile = get_active_library_profile(settings)
    universities = get_active_university_databases(settings)

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
            "capabilities": caps.get(source_id, {}),
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
        "library_system": str(profile.get("key", settings.library_system)),
        "library_name": str(profile.get("name", "")),
        "library_profiles_path": str(settings.library_profiles_path),
        "free_apis": sorted(free, key=lambda row: row["source_id"]),
        "closed_apis": sorted(keyed, key=lambda row: row["source_id"]),
        "playwright_sources": sorted(playwright, key=lambda row: row["source_id"]),
        "university_databases": sorted(universities, key=lambda row: row.get("source_id", "")),
    }


@app.get("/api/orchestrator/files")
def api_file(path: str = Query(...)) -> FileResponse:
    """Serve one artifact/document file for click-through download/open."""

    settings = _settings()
    resolved = _safe_file_path(settings, path)
    return FileResponse(str(resolved))


@app.get("/", include_in_schema=False)
def root_index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)
