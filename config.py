"""Runtime configuration for the orchestrator app.

Purpose:
- Keep all runtime choices in environment variables.
- Provide one place for .env load/validation and connection field schema.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def parse_bool(value: str, default: bool = False) -> bool:
    """Convert common env boolean strings into bool."""
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def default_workspace() -> Path:
    """Return repository root inferred from this file location."""
    return Path(__file__).resolve().parents[1]


def load_dotenv_file(path: Path) -> None:
    """Load .env file values into process env without overriding existing vars."""
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
    """Load .env from workspace for consistent CLI/API behavior."""
    env_path = workspace / ".env"
    load_dotenv_file(env_path)


def _as_abs(workspace: Path, value: str) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    return (workspace / p).resolve()


@dataclass(frozen=True)
class OrchestratorSettings:
    """Configuration object consumed by orchestrator execution paths."""

    workspace: Path
    env_path: Path
    enabled: bool
    auto_ingest: bool
    auto_llm_fit: bool
    fail_fast: bool
    pull_provider: str
    pull_mode: str
    pull_output_root: Path
    api_pull_command: str
    playwright_pull_command: str
    playwright_cdp_url: str
    ingest_ebsco_script: Path
    ingest_external_script: Path
    llm_script: Path
    ingest_timeout_seconds: int
    llm_backend: str
    llm_model: str
    llm_ctx: int
    llm_source_char_cap: int
    llm_timeout_seconds: int
    llm_temperature: float
    ollama_base_url: str

    @staticmethod
    def from_env() -> "OrchestratorSettings":
        """Build settings from environment variables with stable defaults."""
        workspace = Path(os.getenv("ORCH_WORKSPACE", str(default_workspace()))).resolve()
        env_path = workspace / ".env"
        ingest_ebsco_script = _as_abs(
            workspace,
            os.getenv("ORCH_INGEST_EBSCO_SCRIPT", "codex/evidence_hub/ingest_ebsco_runs.py"),
        )
        ingest_external_script = _as_abs(
            workspace,
            os.getenv("ORCH_INGEST_EXTERNAL_SCRIPT", "codex/evidence_hub/ingest_external_run.py"),
        )
        llm_script = _as_abs(
            workspace,
            os.getenv("ORCH_LLM_SCRIPT", "codex/evidence_hub/generate_llm_fit_evidence.py"),
        )
        pull_root = _as_abs(
            workspace,
            os.getenv("ORCH_PULL_OUTPUT_ROOT", "codex/add_to_cart_audit/external_sources"),
        )
        return OrchestratorSettings(
            workspace=workspace,
            env_path=env_path,
            enabled=parse_bool(os.getenv("ORCH_ENABLED", "true"), default=True),
            auto_ingest=parse_bool(os.getenv("ORCH_AUTO_INGEST", "true"), default=True),
            auto_llm_fit=parse_bool(os.getenv("ORCH_AUTO_LLM_FIT", "true"), default=True),
            fail_fast=parse_bool(os.getenv("ORCH_FAIL_FAST", "false"), default=False),
            pull_provider=os.getenv("ORCH_PULL_PROVIDER", "ebscohost").strip().lower(),
            pull_mode=os.getenv("ORCH_PULL_MODE", "auto").strip().lower(),
            pull_output_root=pull_root,
            api_pull_command=os.getenv("ORCH_API_PULL_COMMAND", "").strip(),
            playwright_pull_command=os.getenv("ORCH_PLAYWRIGHT_PULL_COMMAND", "").strip(),
            playwright_cdp_url=os.getenv("ORCH_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222").strip(),
            ingest_ebsco_script=ingest_ebsco_script,
            ingest_external_script=ingest_external_script,
            llm_script=llm_script,
            ingest_timeout_seconds=int(os.getenv("ORCH_INGEST_TIMEOUT_SECONDS", "1800")),
            llm_backend=os.getenv("ORCH_LLM_BACKEND", "ollama").strip().lower(),
            llm_model=os.getenv("ORCH_LLM_MODEL", "qwen2.5:7b").strip(),
            llm_ctx=int(os.getenv("ORCH_LLM_CTX", "1024")),
            llm_source_char_cap=int(os.getenv("ORCH_LLM_SOURCE_CHAR_CAP", "1600")),
            llm_timeout_seconds=int(os.getenv("ORCH_LLM_TIMEOUT_SECONDS", "90")),
            llm_temperature=float(os.getenv("ORCH_LLM_TEMPERATURE", "0.1")),
            ollama_base_url=os.getenv("ORCH_OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip(),
        )


def required_connection_fields(mode: str, provider: str) -> List[Dict[str, object]]:
    """Return required env fields for selected pull mode/provider.

    Non-obvious logic:
    - `auto` mode is treated as requiring both API and Playwright credentials so
      runtime mode switches do not silently fail.
    """
    mode = (mode or "auto").strip().lower()
    provider = (provider or "ebscohost").strip().lower()

    fields: List[Dict[str, object]] = [
        {"key": "ORCH_WORKSPACE", "label": "Workspace", "secret": False, "required": True},
        {"key": "ORCH_PULL_PROVIDER", "label": "Pull Provider", "secret": False, "required": True},
    ]

    if mode in {"api", "auto"}:
        fields.append(
            {
                "key": "ORCH_API_PULL_COMMAND",
                "label": "API Pull Command",
                "secret": False,
                "required": mode == "api",
            }
        )
        if provider == "ebscohost":
            fields.append({"key": "EBSCO_API_KEY", "label": "EBSCO API Key", "secret": True, "required": False})

    if mode in {"playwright", "auto"}:
        fields.extend(
            [
                {
                    "key": "ORCH_PLAYWRIGHT_PULL_COMMAND",
                    "label": "Playwright Pull Command",
                    "secret": False,
                    "required": mode == "playwright",
                },
                {
                    "key": "ORCH_PLAYWRIGHT_CDP_URL",
                    "label": "Playwright CDP URL",
                    "secret": False,
                    "required": False,
                },
            ]
        )
        if provider == "ebscohost":
            fields.extend(
                [
                    {"key": "EBSCO_PROFILE_ID", "label": "EBSCO Profile ID", "secret": True, "required": False},
                    {
                        "key": "EBSCO_PROFILE_PASSWORD",
                        "label": "EBSCO Profile Password",
                        "secret": True,
                        "required": False,
                    },
                ]
            )

    fields.extend(
        [
            {"key": "ORCH_AUTO_INGEST", "label": "Auto Ingest", "secret": False, "required": True},
            {"key": "ORCH_AUTO_LLM_FIT", "label": "Auto LLM Fit", "secret": False, "required": True},
            {"key": "ORCH_LLM_BACKEND", "label": "LLM Backend", "secret": False, "required": True},
            {"key": "ORCH_LLM_MODEL", "label": "LLM Model", "secret": False, "required": True},
            {"key": "ORCH_OLLAMA_BASE_URL", "label": "Ollama Base URL", "secret": False, "required": False},
        ]
    )
    return fields


def _sanitize_env_value(value: str) -> str:
    cleaned = str(value).replace("\r", " ").replace("\n", " ").strip()
    return cleaned


def write_env_updates(env_path: Path, updates: Dict[str, str]) -> None:
    """Merge provided key-value updates into .env and enforce secure permissions."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    else:
        lines = []

    pending = {k: _sanitize_env_value(v) for k, v in updates.items() if k and str(v).strip()}
    output: List[str] = []
    seen_keys: set[str] = set()

    for line in lines:
        match = ENV_LINE_RE.match(line.strip())
        if not match:
            output.append(line)
            continue
        key = match.group(1)
        if key in pending:
            output.append(f"{key}={pending[key]}")
            seen_keys.add(key)
        else:
            output.append(line)

    for key in sorted(pending.keys()):
        if key in seen_keys:
            continue
        output.append(f"{key}={pending[key]}")

    env_path.write_text("\n".join(output).strip() + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        # Best-effort on filesystems that may not support POSIX chmod.
        pass


def read_env_values(env_path: Path) -> Dict[str, str]:
    """Read key-value pairs from .env preserving only valid assignments."""
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
        key = match.group(1).strip()
        value = match.group(2).strip().strip("'").strip('"')
        if key:
            out[key] = value
    return out
