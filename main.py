"""FastAPI entrypoint for interactive orchestration app."""

from __future__ import annotations

import csv
import hashlib
import os
import re
import threading
import uuid
import zipfile
from html import unescape as html_unescape
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import (
    OrchestratorSettings,
    load_runtime_env,
    read_env_values,
    required_connection_fields,
    write_env_updates,
)
from .contracts import ConnectionSaveInput, ConnectionSchemaResponse, IntentCreateInput, RetryInput, RunCreateInput
from .pipeline import emit_event, run_orchestration
from .store import OrchestratorStore, now_utc


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
GAP_MAP_DIR = DATA_DIR / "gap_maps"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
GAP_MAP_DIR.mkdir(parents=True, exist_ok=True)

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


SOURCE_CATALOG: Dict[str, List[Dict[str, str]]] = {
    "free_apis": [
        {"name": "World Bank Indicators API", "notes": "Public macro indicators"},
        {"name": "FRED Public CSV endpoint", "notes": "Federal Reserve economic series"},
        {"name": "DOL OLMS public disclosure", "notes": "Union disclosure endpoints"},
        {"name": "ILOSTAT API", "notes": "Union/labor indicators"},
        {"name": "IMF SDMX API", "notes": "IMTS/BOP annual panels"},
        {"name": "UN Comtrade Preview API", "notes": "Trade slices under preview limits"},
        {"name": "OECD SDMX API", "notes": "TiVA and comparators"},
        {"name": "WITS SDMX API", "notes": "Trade/tariff indicators"},
    ],
    "closed_apis": [
        {"name": "BLS Public Data API v2", "env_key": "BLS_API_KEY", "notes": "Registered access for extended windows"},
        {"name": "BEA API", "env_key": "BEA_USER_ID", "notes": "GDP-by-industry and related pulls"},
        {"name": "Census MRTS API", "env_key": "CENSUS_API_KEY", "notes": "Retail and nonstore annualized context"},
        {"name": "USITC DataWeb API", "env_key": "USITC_DATAWEB_TOKEN", "notes": "Imports by country"},
        {"name": "UN Comtrade live API", "env_key": "UNCOMTRADE_API_KEY", "notes": "Non-preview and higher limits"},
        {"name": "WTO API", "env_key": "WTO_API_KEY", "notes": "WTO timeseries access"},
        {"name": "EBSCOhost API", "env_key": "EBSCO_API_KEY", "notes": "Institutional source pulls"},
        {"name": "Statista exports/session", "notes": "Subscription-gated data retrieval"},
    ],
    "university_databases": [
        {"name": "Academic Search Ultimate", "provider": "EBSCOhost"},
        {"name": "Regional Business News", "provider": "EBSCOhost"},
        {"name": "EconLit with Full Text", "provider": "EBSCOhost"},
        {"name": "CINAHL Plus with Full Text", "provider": "EBSCOhost"},
        {"name": "APA PsycINFO", "provider": "EBSCOhost"},
        {"name": "APA PsycArticles", "provider": "EBSCOhost"},
        {"name": "Legal Source", "provider": "EBSCOhost"},
        {"name": "MLA International Bibliography", "provider": "EBSCOhost"},
        {"name": "ERIC", "provider": "EBSCOhost"},
        {"name": "MasterFILE Premier", "provider": "EBSCOhost"},
        {"name": "Medline (EBSCO)", "provider": "EBSCOhost"},
    ],
}


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


def _list_manuscripts(workspace: Path) -> List[Dict[str, str]]:
    """Return available manuscript files for intake selection.

    Sources:
    - workspace `Manuscript/` files
    - locally uploaded files under app data storage
    """
    manuscript_dir = workspace / "Manuscript"
    allowed_ext = {".docx", ".md", ".txt", ".pdf"}
    rows: List[Dict[str, str]] = []
    if manuscript_dir.exists():
        for path in sorted(manuscript_dir.glob("*")):
            if path.suffix.lower() not in allowed_ext:
                continue
            rel = str(path.relative_to(workspace))
            rows.append(
                {
                    "name": path.name,
                    "path": rel,
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


def _resolve_manuscript_path(workspace: Path, manuscript_path: str) -> Path | None:
    """Resolve manuscript path from relative/absolute UI input."""
    raw = (manuscript_path or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    return (workspace / p).resolve()


def _default_gap_claims(workspace: Path) -> Path:
    return workspace / "codex" / "add_to_cart_audit" / "gap_claims.csv"


def _load_backlog_map(workspace: Path) -> Dict[str, Dict[str, str]]:
    backlog = workspace / "codex" / "evidence_hub" / "data" / "pull_backlog_by_gap.csv"
    backlog_map: Dict[str, Dict[str, str]] = {}
    if not backlog.exists():
        return backlog_map
    try:
        with backlog.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                gap_id = str(row.get("gap_id", "")).strip()
                if gap_id:
                    backlog_map[gap_id] = row
    except Exception:
        return {}
    return backlog_map


def _extract_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"</w:tr>", "\n", xml)
    xml = re.sub(r"<[^>]+>", " ", xml)
    return html_unescape(xml)


def _extract_text_for_gap_generation(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="ignore")
        if suffix == ".docx":
            return _extract_docx_text(path)
    except Exception:
        return ""
    return ""


def _candidate_chapters_from_text(text: str) -> List[str]:
    """Extract likely chapter-like headings from manuscript text."""
    chapters: List[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = " ".join((raw_line or "").strip().split())
        if not line:
            continue
        low = line.lower()

        looks_like = False
        if re.match(r"^chapter\s+\d+[a-z]?(?:[:.\-]\s*|\s+).+", line, flags=re.IGNORECASE):
            looks_like = True
        elif low.startswith("introduction"):
            looks_like = True
        elif low.startswith("conclusion"):
            looks_like = True
        elif re.match(r"^(part|section)\s+[ivx0-9]+", line, flags=re.IGNORECASE):
            looks_like = True

        if not looks_like:
            continue
        key = low
        if key in seen:
            continue
        seen.add(key)
        chapters.append(line)
        if len(chapters) >= 40:
            break
    return chapters


def _generated_gap_map_path(manuscript_file: Path) -> Path:
    signature = hashlib.sha1(str(manuscript_file).encode("utf-8")).hexdigest()[:16]
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", manuscript_file.stem)[:40] or "manuscript"
    return GAP_MAP_DIR / f"{safe_stem}_{signature}_gap_claims.csv"


def _generate_gap_claims_for_manuscript(manuscript_file: Path, out_csv: Path) -> Dict[str, Any]:
    """Generate fallback gap claims CSV when manuscript has no mapped gap file."""
    text = _extract_text_for_gap_generation(manuscript_file)
    chapters = _candidate_chapters_from_text(text)
    rows: List[Dict[str, str]] = []

    if not chapters:
        chapters = ["Auto Generated: Manuscript Review"]
    for idx, chapter in enumerate(chapters, start=1):
        rows.append(
            {
                "gap_id": f"AUTO-{idx:02d}-G1",
                "chapter": chapter,
                "claim_text": (
                    "Auto-generated placeholder gap for selected manuscript. "
                    "Refine this claim text before production pull runs."
                ),
            }
        )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["gap_id", "chapter", "claim_text"])
        writer.writeheader()
        writer.writerows(rows)
    return {"generated": True, "row_count": len(rows), "source_manuscript": str(manuscript_file)}


def _gap_claims_for_manuscript(workspace: Path, manuscript_path: str, refresh: bool = False) -> Dict[str, Any]:
    """Resolve or create gap-claims source for the selected manuscript."""
    default_claims = _default_gap_claims(workspace)
    manuscript_file = _resolve_manuscript_path(workspace, manuscript_path)
    if not manuscript_file or not manuscript_file.exists():
        return {"path": default_claims, "generated": False, "reason": "missing_manuscript_path"}

    # Keep current canonical mapping for Add To Cart manuscript variants.
    add_to_cart_hint = "add to cart" in manuscript_file.name.lower()
    if add_to_cart_hint and default_claims.exists():
        return {"path": default_claims, "generated": False, "reason": "canonical_add_to_cart_map"}

    sidecar_candidates = [
        manuscript_file.with_suffix(".gap_claims.csv"),
        manuscript_file.with_name(f"{manuscript_file.stem}_gap_claims.csv"),
    ]
    for cand in sidecar_candidates:
        if cand.exists():
            return {"path": cand, "generated": False, "reason": "manuscript_sidecar_map"}

    generated_csv = _generated_gap_map_path(manuscript_file)
    if refresh or (not generated_csv.exists()):
        meta = _generate_gap_claims_for_manuscript(manuscript_file, generated_csv)
        return {"path": generated_csv, **meta, "reason": "generated_missing_map"}
    return {"path": generated_csv, "generated": False, "reason": "existing_generated_map"}


def _gap_layout(workspace: Path, manuscript_path: str = "", refresh: bool = False) -> Dict[str, Any]:
    """Build chapter -> gaps layout for selected manuscript.

    Behavior:
    - Uses manuscript-specific sidecar map when present.
    - Uses canonical Add-to-Cart map for Add-to-Cart manuscript variants.
    - Auto-generates and persists a gap map when missing.
    """
    claims_meta = _gap_claims_for_manuscript(workspace, manuscript_path, refresh=refresh)
    gap_claims = Path(claims_meta["path"])
    backlog_map = _load_backlog_map(workspace)

    if not gap_claims.exists():
        return {
            "source": str(gap_claims),
            "chapters": [],
            "gaps": [],
            "generated": bool(claims_meta.get("generated", False)),
            "reason": str(claims_meta.get("reason", "missing_gap_claims")),
        }

    chapters: Dict[str, Dict[str, Any]] = {}
    gaps_flat: List[Dict[str, Any]] = []
    with gap_claims.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            gap_id = str(row.get("gap_id", "")).strip()
            chapter = str(row.get("chapter", "")).strip()
            claim_text = str(row.get("claim_text", "")).strip()
            if not gap_id:
                continue

            extra = backlog_map.get(gap_id, {})
            gap = {
                "gap_id": gap_id,
                "chapter": chapter,
                "claim_text": claim_text,
                "current_linked_docs": int(str(extra.get("current_linked_docs", "0") or "0")),
                "priority": str(extra.get("priority", "")).strip(),
                "target_total_docs": int(str(extra.get("target_total_docs", "0") or "0")),
                "status": str(extra.get("status", "")).strip(),
            }
            gaps_flat.append(gap)

            chapter_rec = chapters.setdefault(chapter, {"chapter": chapter, "gaps": []})
            chapter_rec["gaps"].append(gap)

    chapter_rows = sorted(chapters.values(), key=lambda x: x["chapter"])
    for chapter in chapter_rows:
        chapter["gap_count"] = len(chapter["gaps"])
    return {
        "source": str(gap_claims),
        "manuscript_path": manuscript_path,
        "generated": bool(claims_meta.get("generated", False)),
        "reason": str(claims_meta.get("reason", "")),
        "chapter_count": len(chapter_rows),
        "gap_count": len(gaps_flat),
        "chapters": chapter_rows,
        "gaps": gaps_flat,
    }


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


@app.get("/api/orchestrator/manuscripts")
def api_manuscripts() -> Dict[str, Any]:
    settings = _settings()
    return {"workspace": str(settings.workspace), "manuscripts": _list_manuscripts(settings.workspace)}


@app.post("/api/orchestrator/manuscripts/upload")
async def api_upload_manuscript(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Upload manuscript from local machine so non-workspace files can be selected."""
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".docx", ".md", ".txt", ".pdf"}:
        raise HTTPException(status_code=400, detail="unsupported manuscript format")

    safe_name = f"{uuid.uuid4().hex[:10]}_{filename}"
    out_path = UPLOAD_DIR / safe_name
    content = await file.read()
    out_path.write_bytes(content)

    return {
        "uploaded": True,
        "name": filename,
        "stored_name": safe_name,
        "stored_path": str(out_path),
    }


@app.get("/api/orchestrator/gaps/layout")
def api_gaps_layout(
    manuscript_path: str = Query(default=""),
    refresh: bool = Query(default=False),
) -> Dict[str, Any]:
    settings = _settings()
    return _gap_layout(settings.workspace, manuscript_path=manuscript_path, refresh=refresh)


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


@app.get("/api/orchestrator/connections/values")
def api_connection_values(mask_secrets: bool = Query(default=True)) -> Dict[str, Any]:
    """Return current .env values for settings page editing."""
    settings = _settings()
    values = read_env_values(settings.env_path)
    rows: List[Dict[str, Any]] = []
    for key in sorted(values.keys()):
        value = str(values.get(key, ""))
        is_secret = any(token in key.upper() for token in ["PASSWORD", "KEY", "TOKEN", "SECRET"])
        display = value
        if mask_secrets and is_secret:
            if len(value) <= 4:
                display = "*" * len(value)
            else:
                display = value[:2] + "*" * (len(value) - 4) + value[-2:]
        rows.append(
            {
                "key": key,
                "value": display,
                "raw_value": value if (not mask_secrets or not is_secret) else "",
                "is_secret": is_secret,
                "has_value": bool(value),
            }
        )
    return {"env_path": str(settings.env_path), "values": rows}


@app.get("/api/orchestrator/sources/catalog")
def api_sources_catalog() -> Dict[str, Any]:
    """Return source inventory for Settings: free APIs, closed APIs, university DBs."""
    settings = _settings()
    env_values = read_env_values(settings.env_path)

    def with_env_status(rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        enriched: List[Dict[str, Any]] = []
        for row in rows:
            rec: Dict[str, Any] = dict(row)
            env_key = str(row.get("env_key", "")).strip()
            if env_key:
                rec["env_key"] = env_key
                rec["configured"] = bool(str(env_values.get(env_key, "")).strip())
            else:
                rec["env_key"] = ""
                rec["configured"] = None
            enriched.append(rec)
        return enriched

    return {
        "workspace": str(settings.workspace),
        "free_apis": with_env_status(SOURCE_CATALOG["free_apis"]),
        "closed_apis": with_env_status(SOURCE_CATALOG["closed_apis"]),
        "university_databases": with_env_status(SOURCE_CATALOG["university_databases"]),
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
