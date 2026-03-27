"""Pipeline orchestration logic for pull -> ingest -> llm stages."""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List

from .adapters import PullRunArtifact, build_pull_adapter, validate_artifact_run_dir
from .config import OrchestratorSettings, required_connection_fields
from .store import OrchestratorStore, now_utc


def _mask_env_value(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _required_env_for_run(settings: OrchestratorSettings, mode: str, provider: str) -> List[str]:
    fields = required_connection_fields(mode=mode, provider=provider)
    required_keys = [str(row["key"]) for row in fields if bool(row.get("required"))]
    return required_keys


def emit_event(
    store: OrchestratorStore,
    *,
    run_id: str,
    stage: str,
    status: str,
    message: str,
    meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Append structured run event for frontend timeline/log rendering."""
    event = {
        "event_id": f"evt_{run_id}_{now_utc()}",
        "run_id": run_id,
        "stage": stage,
        "status": status,
        "message": message,
        "meta": meta or {},
        "ts_utc": now_utc(),
    }
    return store.append_event(event)


def _update_run_stage(store: OrchestratorStore, run_id: str, *, status: str, stage: str, **extra: Any) -> Dict[str, Any]:
    rec = store.get_run(run_id) or {"run_id": run_id, "created_at": now_utc()}
    rec.update(extra)
    rec["status"] = status
    rec["stage"] = stage
    rec["updated_at"] = now_utc()
    return store.upsert_run(rec)


def _validate_preflight(settings: OrchestratorSettings, payload: Dict[str, Any]) -> None:
    """Fail fast on missing required env and unavailable scripts."""
    mode = str(payload.get("pull_mode", settings.pull_mode))
    provider = str(payload.get("pull_provider", settings.pull_provider))

    missing: List[str] = []
    for key in _required_env_for_run(settings, mode=mode, provider=provider):
        if not str(os.environ.get(key, "")).strip():
            missing.append(key)
    if missing:
        raise RuntimeError(f"Missing required env keys: {', '.join(sorted(missing))}")

    if settings.auto_ingest:
        if not settings.ingest_ebsco_script.exists():
            raise RuntimeError(f"Missing ingest script: {settings.ingest_ebsco_script}")
        if not settings.ingest_external_script.exists():
            raise RuntimeError(f"Missing ingest script: {settings.ingest_external_script}")
    if settings.auto_llm_fit and settings.llm_backend == "ollama" and not settings.llm_script.exists():
        raise RuntimeError(f"Missing llm script: {settings.llm_script}")


def _run_ingest_stage(settings: OrchestratorSettings, artifact: PullRunArtifact) -> Dict[str, Any]:
    """Run the correct incremental ingester based on artifact type."""
    validate_artifact_run_dir(settings, artifact)
    workspace = settings.workspace

    if artifact.artifact_type == "ebsco_manifest_pair":
        cmd = [
            sys.executable,
            str(settings.ingest_ebsco_script),
            "--workspace",
            str(workspace),
            "--run-id",
            artifact.run_id,
        ]
    elif artifact.artifact_type == "external_packet":
        cmd = [
            sys.executable,
            str(settings.ingest_external_script),
            "--workspace",
            str(workspace),
            "--run-id",
            artifact.run_id,
        ]
    else:
        raise RuntimeError(
            f"unsupported artifact_type '{artifact.artifact_type}' (expected ebsco_manifest_pair|external_packet)"
        )

    proc = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=max(60, int(settings.ingest_timeout_seconds)),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ingest failed code={proc.returncode}: {(proc.stderr or '').strip()[:800]}")

    return {
        "artifact_type": artifact.artifact_type,
        "run_id": artifact.run_id,
        "stdout_tail": (proc.stdout or "").strip()[-600:],
    }


def _run_llm_stage(settings: OrchestratorSettings, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run LLM fit generation with current environment-driven defaults."""
    if settings.llm_backend == "none":
        return {"skipped": True, "reason": "llm_backend=none"}
    if settings.llm_backend != "ollama":
        raise RuntimeError(f"Unsupported llm backend for MVP: {settings.llm_backend}")

    cmd = [
        sys.executable,
        str(settings.llm_script),
        "--workspace",
        str(settings.workspace),
        "--model",
        settings.llm_model,
        "--base-url",
        settings.ollama_base_url,
        "--ctx",
        str(settings.llm_ctx),
        "--source-char-cap",
        str(settings.llm_source_char_cap),
        "--timeout-seconds",
        str(settings.llm_timeout_seconds),
        "--temperature",
        str(settings.llm_temperature),
    ]
    gap_id = str(payload.get("gap_id", "")).strip()
    if gap_id:
        cmd.extend(["--gap-id", gap_id])

    proc = subprocess.run(
        cmd,
        cwd=str(settings.workspace),
        capture_output=True,
        text=True,
        timeout=max(60, int(settings.llm_timeout_seconds) * 20),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"llm fit failed code={proc.returncode}: {(proc.stderr or '').strip()[:800]}")

    return {"stdout_tail": (proc.stdout or "").strip()[-800:]}


def run_orchestration(
    store: OrchestratorStore,
    settings: OrchestratorSettings,
    *,
    run_id: str,
) -> None:
    """Execute pipeline with strict stage ordering and event logging."""
    run_rec = store.get_run(run_id)
    if not run_rec:
        return
    payload = run_rec.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}

    try:
        _update_run_stage(store, run_id, status="validating_config", stage="validating_config")
        emit_event(store, run_id=run_id, stage="validating_config", status="started", message="Validating config")
        _validate_preflight(settings, payload)
        emit_event(store, run_id=run_id, stage="validating_config", status="completed", message="Preflight passed")

        _update_run_stage(store, run_id, status="planning", stage="planning")
        emit_event(store, run_id=run_id, stage="planning", status="started", message="Planning run")
        emit_event(
            store,
            run_id=run_id,
            stage="planning",
            status="completed",
            message="Planning complete",
            meta={
                "pull_mode": payload.get("pull_mode", settings.pull_mode),
                "pull_provider": payload.get("pull_provider", settings.pull_provider),
            },
        )

        _update_run_stage(store, run_id, status="pulling", stage="pulling")
        emit_event(store, run_id=run_id, stage="pulling", status="started", message="Starting pull stage")
        adapter = build_pull_adapter(str(payload.get("pull_mode", settings.pull_mode)), settings)
        artifact = adapter.run(payload=payload, settings=settings)
        emit_event(
            store,
            run_id=run_id,
            stage="pulling",
            status="completed",
            message="Pull stage completed",
            meta={
                "run_id": artifact.run_id,
                "run_dir": artifact.run_dir,
                "artifact_type": artifact.artifact_type,
                "provider": artifact.provider,
            },
        )
        _update_run_stage(
            store,
            run_id,
            status="pulling_completed",
            stage="pulling",
            artifact={
                "run_id": artifact.run_id,
                "run_dir": artifact.run_dir,
                "provider": artifact.provider,
                "artifact_type": artifact.artifact_type,
                "stats": artifact.stats,
            },
        )

        ingest_result: Dict[str, Any] = {}
        if settings.auto_ingest:
            _update_run_stage(store, run_id, status="ingesting", stage="ingesting")
            emit_event(store, run_id=run_id, stage="ingesting", status="started", message="Starting ingest stage")
            ingest_result = _run_ingest_stage(settings, artifact)
            emit_event(
                store,
                run_id=run_id,
                stage="ingesting",
                status="completed",
                message="Ingest stage completed",
                meta=ingest_result,
            )

        llm_result: Dict[str, Any] = {}
        if settings.auto_llm_fit:
            _update_run_stage(store, run_id, status="llm_processing", stage="llm_processing")
            emit_event(store, run_id=run_id, stage="llm_processing", status="started", message="Starting LLM fit stage")
            llm_result = _run_llm_stage(settings, payload=payload)
            emit_event(
                store,
                run_id=run_id,
                stage="llm_processing",
                status="completed",
                message="LLM fit stage completed",
                meta=llm_result,
            )

        _update_run_stage(
            store,
            run_id,
            status="completed",
            stage="completed",
            result={
                "artifact": artifact.__dict__,
                "ingest": ingest_result,
                "llm_fit": llm_result,
                "masked_config": {
                    "llm_model": settings.llm_model,
                    "llm_backend": settings.llm_backend,
                    "ollama_base_url": settings.ollama_base_url,
                    "playwright_cdp_url": settings.playwright_cdp_url,
                    "api_pull_command": _mask_env_value(settings.api_pull_command),
                    "playwright_pull_command": _mask_env_value(settings.playwright_pull_command),
                },
            },
            error=None,
        )
        emit_event(store, run_id=run_id, stage="completed", status="completed", message="Run completed")
    except Exception as exc:
        _update_run_stage(
            store,
            run_id,
            status="failed",
            stage="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        emit_event(
            store,
            run_id=run_id,
            stage="failed",
            status="failed",
            message=f"Run failed: {type(exc).__name__}: {exc}",
            meta={"traceback": traceback.format_exc()[-4000:]},
        )
        if settings.fail_fast:
            raise
