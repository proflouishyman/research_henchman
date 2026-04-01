"""Runtime configuration and `.env` helpers for orchestrator v2."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def parse_bool(value: str | None, default: bool = False) -> bool:
    """Parse a boolean-like environment value with sensible defaults."""

    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_csv_list(value: str | None) -> List[str]:
    """Parse comma-separated env values into compact string list."""

    if value is None:
        return []
    out: List[str] = []
    for raw in str(value).split(","):
        item = raw.strip()
        if not item:
            continue
        out.append(item)
    return out


def default_workspace() -> Path:
    """Infer repository workspace root from app module location."""

    return Path(__file__).resolve().parents[1]


def load_dotenv_file(path: Path) -> None:
    """Load `.env` values into process env without overriding existing vars."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def load_runtime_env(workspace: Path) -> None:
    """Load runtime `.env` for app and scripts executed by orchestrator."""

    load_dotenv_file(workspace / ".env")


def read_env_values(env_path: Path) -> Dict[str, str]:
    """Read key/value assignments from `.env` into a plain dictionary."""

    if not env_path.exists():
        return {}
    out: Dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE_RE.match(line)
        if not match:
            continue
        out[match.group(1).strip()] = match.group(2).strip().strip("'").strip('"')
    return out


def _sanitize_env_value(value: str) -> str:
    return str(value).replace("\n", " ").replace("\r", " ").strip()


def write_env_updates(env_path: Path, updates: Dict[str, str]) -> None:
    """Merge updates into `.env` and keep file permissions restricted."""

    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines() if env_path.exists() else []
    pending = {k: _sanitize_env_value(v) for k, v in updates.items() if k and str(v).strip()}

    out = []
    seen = set()
    for line in lines:
        m = ENV_LINE_RE.match(line.strip())
        if not m:
            out.append(line)
            continue
        key = m.group(1)
        if key in pending:
            out.append(f"{key}={pending[key]}")
            seen.add(key)
        else:
            out.append(line)

    for key in sorted(pending):
        if key in seen:
            continue
        out.append(f"{key}={pending[key]}")

    env_path.write_text("\n".join(out).strip() + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


def _as_abs(workspace: Path, value: str) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    return (workspace / p).resolve()


@dataclass(frozen=True)
class OrchestratorSettings:
    """Application settings loaded from env with strict layer-level knobs."""

    workspace: Path
    env_path: Path
    data_root: Path
    gap_map_cache_dir: Path

    auto_ingest: bool
    auto_llm_fit: bool
    fail_fast: bool

    gap_analysis_use_ollama: bool
    gap_analysis_model: str
    gap_analysis_timeout_seconds: int
    gap_analysis_max_chars: int

    reflection_use_ollama: bool
    reflection_model: str
    reflection_timeout_seconds: int
    routing_min_confidence: float
    plan_review_use_ollama: bool
    plan_review_model: str
    plan_review_timeout_seconds: int

    pull_timeout_seconds: int
    pull_noise_threshold: int
    pull_noise_threshold_free_api: int
    pull_noise_threshold_keyed_api: int
    pull_noise_threshold_playwright: int
    pull_min_accept_docs: int
    pull_max_query_attempts: int
    pull_synonym_cap: int
    pull_output_root: Path
    playwright_cdp_url: str
    library_system: str
    library_profiles_path: Path
    playwright_extra_sources: List[str]

    ingest_ebsco_script: Path
    ingest_external_script: Path

    llm_backend: str
    llm_model: str
    llm_ctx: int
    llm_source_char_cap: int
    llm_timeout_seconds: int
    llm_temperature: float
    ollama_base_url: str

    stale_stage_timeout_seconds: int

    @staticmethod
    def from_env() -> "OrchestratorSettings":
        """Build settings from process env and defaults.

        Non-obvious logic:
        - `data_root` is app-local (`app/data`) for predictable Docker behavior.
        - source/output roots remain workspace-relative for existing evidence hub tools.
        """

        workspace = Path(os.getenv("ORCH_WORKSPACE", str(default_workspace()))).resolve()
        app_root = Path(__file__).resolve().parent
        data_root = _as_abs(workspace, os.getenv("ORCH_DATA_ROOT", str(app_root / "data")))
        data_root.mkdir(parents=True, exist_ok=True)
        gap_cache_dir = _as_abs(workspace, os.getenv("ORCH_GAP_MAP_CACHE_DIR", str(data_root / "gap_maps")))

        return OrchestratorSettings(
            workspace=workspace,
            env_path=workspace / ".env",
            data_root=data_root,
            gap_map_cache_dir=gap_cache_dir,
            auto_ingest=parse_bool(os.getenv("ORCH_AUTO_INGEST"), True),
            auto_llm_fit=parse_bool(os.getenv("ORCH_AUTO_LLM_FIT"), True),
            fail_fast=parse_bool(os.getenv("ORCH_FAIL_FAST"), False),
            gap_analysis_use_ollama=parse_bool(os.getenv("ORCH_GAP_ANALYSIS_USE_OLLAMA"), True),
            gap_analysis_model=os.getenv("ORCH_GAP_ANALYSIS_MODEL", "qwen2.5:7b").strip(),
            gap_analysis_timeout_seconds=int(os.getenv("ORCH_GAP_ANALYSIS_TIMEOUT_SECONDS", "120")),
            gap_analysis_max_chars=int(os.getenv("ORCH_GAP_ANALYSIS_MAX_CHARS", "40000")),
            reflection_use_ollama=parse_bool(os.getenv("ORCH_REFLECTION_USE_OLLAMA"), True),
            reflection_model=os.getenv("ORCH_REFLECTION_MODEL", "qwen2.5:7b").strip(),
            reflection_timeout_seconds=int(os.getenv("ORCH_REFLECTION_TIMEOUT_SECONDS", "120")),
            routing_min_confidence=float(os.getenv("ORCH_ROUTING_MIN_CONFIDENCE", "0.67")),
            plan_review_use_ollama=parse_bool(os.getenv("ORCH_PLAN_REVIEW_USE_OLLAMA"), True),
            plan_review_model=os.getenv("ORCH_PLAN_REVIEW_MODEL", os.getenv("ORCH_REFLECTION_MODEL", "qwen2.5:7b")).strip(),
            plan_review_timeout_seconds=int(os.getenv("ORCH_PLAN_REVIEW_TIMEOUT_SECONDS", "90")),
            pull_timeout_seconds=int(os.getenv("ORCH_PULL_TIMEOUT_SECONDS", "60")),
            pull_noise_threshold=int(os.getenv("ORCH_PULL_NOISE_THRESHOLD", "50")),
            pull_noise_threshold_free_api=int(
                os.getenv("ORCH_PULL_NOISE_THRESHOLD_FREE_API", os.getenv("ORCH_PULL_NOISE_THRESHOLD", "50"))
            ),
            pull_noise_threshold_keyed_api=int(
                os.getenv("ORCH_PULL_NOISE_THRESHOLD_KEYED_API", os.getenv("ORCH_PULL_NOISE_THRESHOLD", "50"))
            ),
            pull_noise_threshold_playwright=int(
                os.getenv("ORCH_PULL_NOISE_THRESHOLD_PLAYWRIGHT", os.getenv("ORCH_PULL_NOISE_THRESHOLD", "50"))
            ),
            pull_min_accept_docs=int(os.getenv("ORCH_PULL_MIN_ACCEPT_DOCS", "2")),
            pull_max_query_attempts=int(os.getenv("ORCH_PULL_MAX_QUERY_ATTEMPTS", "4")),
            pull_synonym_cap=int(os.getenv("ORCH_PULL_SYNONYM_CAP", "4")),
            pull_output_root=_as_abs(
                workspace,
                os.getenv("ORCH_PULL_OUTPUT_ROOT", "codex/add_to_cart_audit/external_sources"),
            ),
            playwright_cdp_url=os.getenv("ORCH_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222").strip(),
            library_system=os.getenv("ORCH_LIBRARY_SYSTEM", "jhu").strip().lower(),
            library_profiles_path=_as_abs(
                workspace,
                os.getenv("ORCH_LIBRARY_PROFILES_PATH", str(app_root / "library_profiles.default.json")),
            ),
            playwright_extra_sources=parse_csv_list(os.getenv("ORCH_PLAYWRIGHT_EXTRA_SOURCES")),
            ingest_ebsco_script=_as_abs(
                workspace,
                os.getenv("ORCH_INGEST_EBSCO_SCRIPT", "codex/evidence_hub/ingest_ebsco_runs.py"),
            ),
            ingest_external_script=_as_abs(
                workspace,
                os.getenv("ORCH_INGEST_EXTERNAL_SCRIPT", "codex/evidence_hub/ingest_external_run.py"),
            ),
            llm_backend=os.getenv("ORCH_LLM_BACKEND", "ollama").strip().lower(),
            llm_model=os.getenv("ORCH_LLM_MODEL", "qwen2.5:7b").strip(),
            llm_ctx=int(os.getenv("ORCH_LLM_CTX", "1024")),
            llm_source_char_cap=int(os.getenv("ORCH_LLM_SOURCE_CHAR_CAP", "1600")),
            llm_timeout_seconds=int(os.getenv("ORCH_LLM_TIMEOUT_SECONDS", "90")),
            llm_temperature=float(os.getenv("ORCH_LLM_TEMPERATURE", "0.1")),
            ollama_base_url=os.getenv("ORCH_OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip(),
            stale_stage_timeout_seconds=int(os.getenv("ORCH_STALE_STAGE_TIMEOUT_SECONDS", "3600")),
        )
