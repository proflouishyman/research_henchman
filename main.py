"""FastAPI entrypoint for interactive orchestration app."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import threading
import urllib.error
import urllib.request
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
ANALYSIS_VERSION = 2

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


def _extract_text_for_gap_generation(path: Path) -> tuple[str, Dict[str, Any]]:
    suffix = path.suffix.lower()
    meta: Dict[str, Any] = {
        "status": "unsupported_format",
        "format": suffix or "unknown",
        "char_count": 0,
        "line_count": 0,
    }
    try:
        if suffix in {".txt", ".md"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            meta.update(
                {
                    "status": "ok",
                    "char_count": len(text),
                    "line_count": len(text.splitlines()),
                }
            )
            return text, meta
        if suffix == ".docx":
            text = _extract_docx_text(path)
            meta.update(
                {
                    "status": "ok",
                    "char_count": len(text),
                    "line_count": len(text.splitlines()),
                }
            )
            return text, meta
    except Exception:
        meta["status"] = "extract_failed"
        return "", meta
    return "", meta


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
        elif re.match(r"^chapter\s+[a-z0-9ivx]+", line, flags=re.IGNORECASE):
            looks_like = True
        elif re.match(r"^chapter\b", line, flags=re.IGNORECASE):
            looks_like = len(line.split()) <= 14
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
    # Include file fingerprint so map cache invalidates when manuscript content changes.
    try:
        stat = manuscript_file.stat()
        fingerprint = f"{manuscript_file.resolve()}::{stat.st_size}::{stat.st_mtime_ns}"
    except OSError:
        fingerprint = str(manuscript_file.resolve())
    signature = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", manuscript_file.stem)[:40] or "manuscript"
    return GAP_MAP_DIR / f"{safe_stem}_{signature}_gap_claims.csv"


def _generated_gap_meta_path(gap_csv: Path) -> Path:
    return gap_csv.with_suffix(".meta.json")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_json_object(text: str) -> Dict[str, Any]:
    cleaned = (text or "").strip()
    if not cleaned:
        return {}
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ollama_list_models(base_url: str, timeout_seconds: int) -> List[str]:
    req = urllib.request.Request(f"{base_url.rstrip('/')}/api/tags")
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    out: List[str] = []
    for row in payload.get("models", []):
        name = str(row.get("name", "")).strip()
        if name:
            out.append(name)
    return out


def _pick_smart_model(available: List[str], preferred: str) -> str:
    preferred = (preferred or "").strip()
    if preferred and preferred in available:
        return preferred
    ranking = [
        "nemotron-3-super:120b",
        "gpt-oss:120b",
        "qwen2.5:72b",
        "qwen3:32b",
        "qwen2.5:32b",
        "qwen2.5:14b",
        "llama3.3:70b",
        "llama3.1:70b",
        "qwen2.5:7b",
        "llama3.2:latest",
        "llama3.1:8b",
    ]
    for target in ranking:
        for name in available:
            if name == target or name.startswith(f"{target}:"):
                return name
    if available:
        return available[0]
    return preferred or "qwen2.5:32b"


def _ollama_generate_json(
    *,
    base_url: str,
    model: str,
    prompt: str,
    timeout_seconds: int,
    temperature: float = 0.1,
    num_ctx: int = 4096,
) -> Dict[str, Any]:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="ignore"))
    response_text = str(body.get("response", "")).strip()
    return _parse_json_object(response_text)


def _split_sections(text: str) -> List[Dict[str, Any]]:
    """Split manuscript text into heading-oriented sections."""
    lines = [re.sub(r"\s+", " ", (line or "").strip()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    sections: List[Dict[str, Any]] = []

    def looks_like_heading(line: str) -> bool:
        low = line.lower()
        if re.match(r"^chapter\s+\d+[a-z]?(?:[:.\-]\s*|\s+).+", line, flags=re.IGNORECASE):
            return True
        if re.match(r"^chapter\s+[a-z0-9ivx]+", line, flags=re.IGNORECASE):
            return True
        if low.startswith("introduction") or low.startswith("conclusion"):
            return True
        if re.match(r"^(part|section)\s+[ivx0-9]+", line, flags=re.IGNORECASE):
            return True
        # Short all-caps lines often represent section headings in OCR/docx extracts.
        if line.isupper() and 2 <= len(line.split()) <= 12 and len(line) <= 90:
            return True
        return False

    current = {"heading": "Manuscript Body", "lines": []}
    for line in lines:
        if looks_like_heading(line):
            if current["lines"]:
                sections.append(current)
            current = {"heading": line, "lines": []}
        else:
            current["lines"].append(line)
    if current["lines"]:
        sections.append(current)
    if not sections:
        sections = [{"heading": "Manuscript Body", "lines": lines}]
    return sections


def _count_hits(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


def _todo_markers(text: str) -> List[str]:
    markers: List[str] = []
    for line in text.splitlines():
        low = line.lower()
        if any(token in low for token in ["todo", "tbd", "fixme", "missing", "[", "]", "??", "insert "]):
            snippet = re.sub(r"\s+", " ", line).strip()
            if snippet:
                markers.append(snippet[:220])
    return markers


def _build_gap_rows_from_text_heuristic(text: str) -> Dict[str, Any]:
    """Produce manuscript-aware gap analysis rows from extracted text."""
    sections = _split_sections(text)
    rows: List[Dict[str, str]] = []
    claim_seen: set[str] = set()
    section_index = 0
    total_todo = 0

    for section in sections:
        section_index += 1
        heading = section["heading"]
        body = "\n".join(section["lines"])
        body_compact = re.sub(r"\s+", " ", body).strip()
        if not body_compact:
            continue

        citation_hits = _count_hits(r"\(\d{4}\)|\[\d+\]|doi|source:|https?://|www\.", body_compact)
        number_hits = _count_hits(r"\b\d{4}\b|\b\d+(?:\.\d+)?%|\$\s?\d+", body_compact)
        hedge_hits = _count_hits(r"\b(maybe|perhaps|likely|appears|seems|suggests|could|might)\b", body_compact)
        todo_hits = _todo_markers(body)
        total_todo += len(todo_hits)
        paragraph_count = len([ln for ln in section["lines"] if len(ln) > 60])

        candidates: List[str] = []
        for marker in todo_hits[:2]:
            candidates.append(f"Unresolved placeholder or note in section requires source-backed completion: '{marker}'.")

        if len(body_compact) > 500 and citation_hits < 2:
            candidates.append("Section lacks explicit citations or source references for major claims.")
        if len(body_compact) > 500 and number_hits < 2:
            candidates.append("Section lacks quantitative evidence (figures, percentages, or dated metrics) to anchor key assertions.")
        if hedge_hits >= 4 and citation_hits == 0:
            candidates.append("Section relies on hedged language without direct evidence anchors; add stronger sourcing for causal claims.")
        if paragraph_count <= 1 and len(body_compact) > 280:
            candidates.append("Section argument is compressed into too little structured exposition; split claims and add supporting evidence per claim.")

        if not candidates:
            candidates.append("Section needs an explicit evidence map tying each major claim to at least one verifiable source.")

        local_i = 0
        for claim in candidates:
            if claim in claim_seen:
                continue
            claim_seen.add(claim)
            local_i += 1
            rows.append(
                {
                    "gap_id": f"AUTO-{section_index:02d}-G{local_i}",
                    "chapter": heading,
                    "claim_text": claim,
                }
            )
            if local_i >= 6:
                break
        if len(rows) >= 80:
            break

    if not rows:
        rows = [
            {
                "gap_id": "AUTO-01-G1",
                "chapter": "Auto Generated: Manuscript Review",
                "claim_text": (
                    "Auto-generated placeholder gap for selected manuscript. "
                    "Refine this claim text before production pull runs."
                ),
            }
        ]

    return {
        "rows": rows,
        "section_count": len(sections),
        "todo_markers": total_todo,
        "analysis_method": "heuristic",
    }


def _build_gap_rows_from_text_ollama(text: str) -> Dict[str, Any]:
    """Use Ollama to produce structured gap analysis from manuscript text."""
    base_url = os.getenv("ORCH_GAP_ANALYSIS_OLLAMA_BASE_URL", os.getenv("ORCH_OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    preferred_model = os.getenv("ORCH_GAP_ANALYSIS_MODEL", "qwen2.5:32b").strip()
    timeout_seconds = int(os.getenv("ORCH_GAP_ANALYSIS_TIMEOUT_SECONDS", "240"))

    available = _ollama_list_models(base_url, timeout_seconds=timeout_seconds)
    model = _pick_smart_model(available, preferred_model)
    chapter_hints = _candidate_chapters_from_text(text)[:20]
    excerpt = text[:18000]
    prompt = (
        "You are a rigorous manuscript gap analyst.\n"
        "Task: find evidence gaps in this manuscript content.\n"
        "Output STRICT JSON object with key `gaps` only.\n"
        "Schema: {\"gaps\":[{\"chapter\":\"...\",\"claim_text\":\"...\"}]}\n"
        "Rules:\n"
        "- 2 to 5 gaps per detected chapter when possible.\n"
        "- claim_text must be concrete, evidence-oriented, and actionable.\n"
        "- avoid generic filler.\n"
        "- max 60 gaps total.\n\n"
        f"Chapter hints: {chapter_hints}\n"
        f"Manuscript excerpt:\n{excerpt}\n"
    )
    parsed = _ollama_generate_json(
        base_url=base_url,
        model=model,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        temperature=0.1,
        num_ctx=4096,
    )
    gap_items = parsed.get("gaps", [])
    if not isinstance(gap_items, list):
        raise RuntimeError("ollama_response_missing_gaps_array")

    rows: List[Dict[str, str]] = []
    per_chapter_counts: Dict[str, int] = {}
    for item in gap_items:
        if not isinstance(item, dict):
            continue
        chapter = " ".join(str(item.get("chapter", "")).split()).strip() or "Auto Generated: Manuscript Review"
        claim_text = " ".join(str(item.get("claim_text", "")).split()).strip()
        if len(claim_text) < 25:
            continue
        per_chapter_counts[chapter] = per_chapter_counts.get(chapter, 0) + 1
        gap_id = f"AUTO-{len(per_chapter_counts):02d}-G{per_chapter_counts[chapter]}"
        rows.append({"gap_id": gap_id, "chapter": chapter, "claim_text": claim_text})
        if len(rows) >= 60:
            break
    if not rows:
        raise RuntimeError("ollama_no_valid_gap_rows")

    return {
        "rows": rows,
        "section_count": len(set(r["chapter"] for r in rows)),
        "todo_markers": 0,
        "analysis_method": "ollama",
        "model": model,
    }


def _is_placeholder_only_gap_map(path: Path) -> bool:
    """Detect legacy one-row placeholder maps that should be regenerated."""
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return False
    if len(rows) != 1:
        return False
    claim = str(rows[0].get("claim_text", "")).lower()
    return "placeholder gap for selected manuscript" in claim


def _generate_gap_claims_for_manuscript(manuscript_file: Path, out_csv: Path) -> Dict[str, Any]:
    """Generate fallback gap claims CSV when manuscript has no mapped gap file."""
    text, extract_meta = _extract_text_for_gap_generation(manuscript_file)
    llm_error = ""
    if _env_bool("ORCH_GAP_ANALYSIS_USE_OLLAMA", default=True):
        try:
            analysis = _build_gap_rows_from_text_ollama(text)
        except Exception as exc:
            llm_error = f"{type(exc).__name__}: {exc}"
            analysis = _build_gap_rows_from_text_heuristic(text)
    else:
        analysis = _build_gap_rows_from_text_heuristic(text)
    rows: List[Dict[str, str]] = analysis["rows"]

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["gap_id", "chapter", "claim_text"])
        writer.writeheader()
        writer.writerows(rows)

    meta = {
        "generated": True,
        "analysis_version": ANALYSIS_VERSION,
        "row_count": len(rows),
        "source_manuscript": str(manuscript_file),
        "extraction": {
            **extract_meta,
            "section_count": analysis.get("section_count", 0),
            "todo_markers_detected": analysis.get("todo_markers", 0),
            "chapter_candidates_detected": len({row["chapter"] for row in rows if row.get("chapter")}),
            "chapter_candidates_preview": list({row["chapter"] for row in rows if row.get("chapter")})[:12],
            "used_fallback_single_gap": len(rows) == 1 and rows[0]["chapter"].startswith("Auto Generated"),
            "analysis_method": analysis.get("analysis_method", "heuristic"),
            "analysis_model": analysis.get("model", ""),
            "llm_error": llm_error,
            "message": (
                "Gap analysis generated from manuscript text."
                if rows
                else "No analyzable text extracted; fallback placeholder gap map generated."
            ),
        },
    }
    _generated_gap_meta_path(out_csv).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _gap_claims_for_manuscript(workspace: Path, manuscript_path: str, refresh: bool = False) -> Dict[str, Any]:
    """Resolve or create gap-claims source for the selected manuscript."""
    default_claims = _default_gap_claims(workspace)
    manuscript_file = _resolve_manuscript_path(workspace, manuscript_path)
    if not manuscript_file or not manuscript_file.exists():
        return {"path": default_claims, "generated": False, "reason": "missing_manuscript_path"}

    # Keep current canonical mapping for Add To Cart manuscript variants.
    add_to_cart_hint = "add to cart" in manuscript_file.name.lower()
    if add_to_cart_hint and default_claims.exists():
        return {
            "path": default_claims,
            "generated": False,
            "reason": "canonical_add_to_cart_map",
            "extraction": {
                "status": "skipped",
                "message": "Using canonical Add-to-Cart gap map for selected manuscript.",
            },
        }

    sidecar_candidates = [
        manuscript_file.with_suffix(".gap_claims.csv"),
        manuscript_file.with_name(f"{manuscript_file.stem}_gap_claims.csv"),
    ]
    for cand in sidecar_candidates:
        if cand.exists():
            return {
                "path": cand,
                "generated": False,
                "reason": "manuscript_sidecar_map",
                "extraction": {
                    "status": "skipped",
                    "message": "Using manuscript sidecar gap map.",
                },
            }

    generated_csv = _generated_gap_map_path(manuscript_file)
    if refresh or (not generated_csv.exists()):
        meta = _generate_gap_claims_for_manuscript(manuscript_file, generated_csv)
        return {"path": generated_csv, **meta, "reason": "generated_missing_map"}
    if _is_placeholder_only_gap_map(generated_csv):
        meta = _generate_gap_claims_for_manuscript(manuscript_file, generated_csv)
        return {"path": generated_csv, **meta, "reason": "regenerated_placeholder_map"}
    meta_path = _generated_gap_meta_path(generated_csv)
    if meta_path.exists():
        try:
            prev = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                version = int(prev.get("analysis_version", 0) or 0)
                if version < ANALYSIS_VERSION:
                    meta = _generate_gap_claims_for_manuscript(manuscript_file, generated_csv)
                    return {"path": generated_csv, **meta, "reason": "regenerated_analysis_upgrade"}
                return {"path": generated_csv, "generated": False, "reason": "existing_generated_map", **prev}
        except Exception:
            pass
    # Legacy maps without metadata should be regenerated to produce read diagnostics.
    meta = _generate_gap_claims_for_manuscript(manuscript_file, generated_csv)
    return {"path": generated_csv, **meta, "reason": "regenerated_missing_metadata"}


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
        "extraction": claims_meta.get("extraction", {}),
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
