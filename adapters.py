"""Pull adapter abstractions and command-backed implementations."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .config import OrchestratorSettings


JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


@dataclass
class PullRunArtifact:
    """Normalized pull artifact contract consumed by ingest stage."""

    run_id: str
    provider: str
    run_dir: str
    artifact_type: str
    status: str
    stats: Dict[str, Any]


class PullAdapter:
    """Base pull adapter contract."""

    def run(self, payload: Dict[str, Any], settings: OrchestratorSettings) -> PullRunArtifact:
        raise NotImplementedError


def _parse_json_from_stdout(stdout_text: str) -> Dict[str, Any]:
    text = (stdout_text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    match = JSON_OBJECT_RE.search(text)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class CommandPullAdapter(PullAdapter):
    """Execute configurable shell command and parse pull artifact output.

    Non-obvious logic:
    - If an existing run handoff is provided, we skip command execution and
      return normalized artifact directly so users can orchestrate historical runs.
    """

    def __init__(self, mode: str, command_template: str) -> None:
        self.mode = mode
        self.command_template = command_template

    def run(self, payload: Dict[str, Any], settings: OrchestratorSettings) -> PullRunArtifact:
        existing_run_id = str(payload.get("existing_run_id", "")).strip()
        existing_run_dir = str(payload.get("existing_run_dir", "")).strip()
        artifact_type = str(payload.get("artifact_type", "ebsco_manifest_pair")).strip() or "ebsco_manifest_pair"
        provider = str(payload.get("pull_provider", settings.pull_provider)).strip().lower() or settings.pull_provider

        if existing_run_id and existing_run_dir:
            return PullRunArtifact(
                run_id=existing_run_id,
                provider=provider,
                run_dir=existing_run_dir,
                artifact_type=artifact_type,
                status="completed",
                stats={"handoff": True},
            )

        if not self.command_template:
            raise RuntimeError(
                f"No pull command configured for mode='{self.mode}'. "
                "Provide existing_run_id/existing_run_dir or set command env vars."
            )

        cmd = self.command_template.format(
            workspace=str(settings.workspace),
            provider=provider,
            mode=self.mode,
        )
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(settings.workspace),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Pull command failed (mode={self.mode}, code={proc.returncode}): "
                f"{(proc.stderr or '').strip()[:600]}"
            )

        payload_json = _parse_json_from_stdout(proc.stdout)
        run_id = str(payload_json.get("run_id", "")).strip()
        run_dir = str(payload_json.get("run_dir", "")).strip()
        if not run_id or not run_dir:
            raise RuntimeError(
                "Pull command succeeded but did not emit required artifact JSON "
                "with `run_id` and `run_dir`."
            )

        return PullRunArtifact(
            run_id=run_id,
            provider=str(payload_json.get("provider", provider) or provider).strip().lower(),
            run_dir=run_dir,
            artifact_type=str(payload_json.get("artifact_type", artifact_type) or artifact_type),
            status=str(payload_json.get("status", "completed") or "completed"),
            stats=payload_json.get("stats", {}) if isinstance(payload_json.get("stats"), dict) else {},
        )


def build_pull_adapter(mode: str, settings: OrchestratorSettings) -> PullAdapter:
    """Build mode-selected pull adapter with fallback behavior."""
    normalized = (mode or settings.pull_mode or "auto").strip().lower()
    if normalized == "api":
        return CommandPullAdapter(mode="api", command_template=settings.api_pull_command)
    if normalized == "playwright":
        return CommandPullAdapter(mode="playwright", command_template=settings.playwright_pull_command)

    # Auto mode: prefer API command if configured, otherwise Playwright.
    if settings.api_pull_command:
        return CommandPullAdapter(mode="api", command_template=settings.api_pull_command)
    return CommandPullAdapter(mode="playwright", command_template=settings.playwright_pull_command)


def validate_artifact_run_dir(settings: OrchestratorSettings, artifact: PullRunArtifact) -> Path:
    """Resolve run_dir into absolute path and verify it exists."""
    run_dir = Path(artifact.run_dir)
    if not run_dir.is_absolute():
        run_dir = (settings.workspace / run_dir).resolve()
    if not run_dir.exists():
        raise RuntimeError(f"pull artifact run_dir does not exist: {run_dir}")
    return run_dir

