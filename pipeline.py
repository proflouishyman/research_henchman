"""Pipeline orchestrator for v2 layered run execution."""

from __future__ import annotations

import re
import traceback
from typing import Any, Dict

from .config import OrchestratorSettings
from .contracts import RunStatus, run_record_from_dict, run_record_to_dict
from .layers.analysis import analyze_manuscript
from .layers.fit import fit_gap
from .layers.ingest import ingest_gap_result
from .layers.pull import build_source_availability, pull_for_plan
from .layers.reflection import reflect_on_gaps
from .store import OrchestratorStore, now_utc


SECRET_KEY_RE = re.compile(r"(PASSWORD|KEY|TOKEN|SECRET)", re.IGNORECASE)


def _scrub_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Redact secret-like fields recursively before event persistence."""

    out: Dict[str, Any] = {}
    for key, value in meta.items():
        if SECRET_KEY_RE.search(str(key)):
            out[str(key)] = "***"
            continue
        if isinstance(value, dict):
            out[str(key)] = _scrub_meta(value)
        elif isinstance(value, list):
            out[str(key)] = ["***" if SECRET_KEY_RE.search(str(key)) else item for item in value]
        else:
            out[str(key)] = value
    return out


def _emit_event(
    store: OrchestratorStore,
    *,
    run_id: str,
    stage: str,
    status: str,
    message: str,
    meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Append one structured run event."""

    payload = {
        "event_id": f"evt_{run_id}_{now_utc()}",
        "run_id": run_id,
        "stage": stage,
        "status": status,
        "message": message,
        "meta": _scrub_meta(meta or {}),
        "ts_utc": now_utc(),
    }
    return store.append_event(payload)


def run_orchestration(
    store: OrchestratorStore,
    settings: OrchestratorSettings,
    *,
    run_id: str,
) -> None:
    """Execute full pipeline for one run ID.

    Stage sequence:
    1. analyze manuscript
    2. reflect plan
    3. pull
    4. ingest
    5. fit
    """

    rec_row = store.get_run(run_id)
    if not isinstance(rec_row, dict):
        return
    rec = run_record_from_dict(rec_row)

    def emit(stage: str, status: str, message: str, meta: Dict[str, Any] | None = None) -> None:
        _emit_event(store, run_id=run_id, stage=stage, status=status, message=message, meta=meta)

    def save(status: RunStatus, detail: str = "", **extra: Any) -> None:
        rec.status = status
        rec.stage_detail = detail
        rec.updated_at = now_utc()
        for key, value in extra.items():
            setattr(rec, key, value)
        store.upsert_run(run_record_to_dict(rec))

    try:
        save(RunStatus.ANALYZING, "Reading manuscript and identifying gaps")
        emit("analyzing", "started", "Starting manuscript analysis")
        gap_map = analyze_manuscript(rec.manuscript_path, settings, refresh=rec.force)
        rec.gap_map = gap_map
        save(RunStatus.ANALYZING, f"Found {len(gap_map.gaps)} gaps")
        emit(
            "analyzing",
            "completed",
            f"Found {len(gap_map.gaps)} gaps",
            {
                "explicit": gap_map.explicit_count,
                "implicit": gap_map.implicit_count,
                "analysis_method": gap_map.analysis_method,
                "analysis_model": gap_map.analysis_model,
                "fallback_reason": gap_map.fallback_reason,
            },
        )
    except Exception as exc:  # noqa: BLE001 - hard fail stage.
        save(RunStatus.FAILED, str(exc)[:200], error=str(exc)[:200])
        emit("analyzing", "failed", str(exc)[:200], {"traceback": traceback.format_exc()[-1200:]})
        if settings.fail_fast:
            raise
        return

    availability = build_source_availability(settings)
    if availability.playwright_unavailable_reason:
        emit("setup", "warning", availability.playwright_unavailable_reason)
    if availability.missing_keys:
        emit("setup", "warning", "Some keyed API credentials are missing", {"missing_keys": availability.missing_keys})

    try:
        save(RunStatus.PLANNING, "Local LLM reviewing gap map and planning research")
        emit("planning", "started", "LLM reflecting on gaps and source availability")

        plan = reflect_on_gaps(gap_map, availability, run_id, settings)
        rec.research_plan = plan
        active = sum(1 for gap in plan.gaps if not gap.skip)
        skipped = sum(1 for gap in plan.gaps if gap.skip)
        save(RunStatus.PLANNING, f"Plan ready: {active} gaps to pull, {skipped} skipped")
        emit(
            "planning",
            "completed",
            plan.plan_summary,
            {
                "active_gaps": active,
                "skipped_gaps": skipped,
                "estimated_pulls": plan.estimated_pull_count,
                "method": plan.reflection_method,
                "routing_method": plan.routing_method,
                "review_required": plan.review_required_count,
                "review_resolved": plan.review_resolved_count,
            },
        )
        if plan.review_required_count:
            emit(
                "planning",
                "warning",
                f"Routing review flagged {plan.review_required_count} gap(s)",
                {"review_required": plan.review_required_count, "review_resolved": plan.review_resolved_count},
            )
    except Exception as exc:  # noqa: BLE001 - hard fail stage.
        save(RunStatus.FAILED, str(exc)[:200], error=str(exc)[:200])
        emit("planning", "failed", str(exc)[:200], {"traceback": traceback.format_exc()[-1200:]})
        if settings.fail_fast:
            raise
        return

    try:
        save(RunStatus.PULLING, "Executing research pulls")
        emit("pulling", "started", "Starting pull stage")

        def emit_pull(stage: str, status: str, message: str, meta: Dict[str, Any] | None = None) -> None:
            emit(stage, status, message, meta)
            if stage == "pulling" and status == "progress":
                save(RunStatus.PULLING, message)

        pull_results = pull_for_plan(plan, settings, emit_pull, run_id)
        rec.pull_results = pull_results

        unresolvable = sum(1 for row in pull_results if row.status == "unresolvable")
        total_docs = sum(row.total_documents for row in pull_results)
        save(RunStatus.PULLING, f"Pulled {total_docs} documents, {unresolvable} gaps unresolvable")
        emit(
            "pulling",
            "completed",
            "Pull complete",
            {"total_documents": total_docs, "unresolvable_gaps": unresolvable},
        )
    except Exception as exc:  # noqa: BLE001 - hard fail stage.
        save(RunStatus.FAILED, str(exc)[:200], error=str(exc)[:200])
        emit("pulling", "failed", str(exc)[:200], {"traceback": traceback.format_exc()[-1200:]})
        if settings.fail_fast:
            raise
        return

    ingest_results = []
    try:
        save(RunStatus.INGESTING, "Ingesting pulled documents")
        emit("ingesting", "started", f"Ingesting {len(pull_results)} gap result sets")

        for idx, result in enumerate(pull_results, start=1):
            detail = f"Ingesting gap {idx}/{len(pull_results)}: {result.gap_id}"
            save(RunStatus.INGESTING, detail)
            emit("ingesting", "progress", detail, {"gap_id": result.gap_id, "position": f"{idx}/{len(pull_results)}"})
            ingest_results.append(ingest_gap_result(result, settings, run_id))

        rec.ingest_results = ingest_results
        ingested = sum(1 for row in ingest_results if row.ingested)
        save(RunStatus.INGESTING, f"Ingested {ingested}/{len(ingest_results)} gaps")
        emit("ingesting", "completed", "Ingestion complete", {"ingested": ingested, "total": len(ingest_results)})
    except Exception as exc:  # noqa: BLE001 - hard fail stage.
        save(RunStatus.FAILED, str(exc)[:200], error=str(exc)[:200])
        emit("ingesting", "failed", str(exc)[:200], {"traceback": traceback.format_exc()[-1200:]})
        if settings.fail_fast:
            raise
        return

    fit_results = []
    if settings.auto_llm_fit:
        try:
            save(RunStatus.FITTING, "Running LLM fit scoring")
            emit("fitting", "started", "Scoring document-gap fit with LLM")

            for idx, ingest_result in enumerate(ingest_results, start=1):
                detail = f"Scoring fit for gap {idx}/{len(ingest_results)}: {ingest_result.gap_id}"
                save(RunStatus.FITTING, detail)
                emit("fitting", "progress", detail, {"gap_id": ingest_result.gap_id, "position": f"{idx}/{len(ingest_results)}"})
                fit_results.append(fit_gap(ingest_result, settings, run_id))

            rec.fit_results = fit_results
            links_scored = sum(row.links_scored for row in fit_results)
            emit("fitting", "completed", "Fit scoring complete", {"links_scored": links_scored})
        except Exception as exc:  # noqa: BLE001 - fit failure does not fail run.
            emit("fitting", "failed", str(exc)[:200], {"traceback": traceback.format_exc()[-1200:]})

    had_unresolvable = any(row.status == "unresolvable" for row in pull_results)
    final_status = RunStatus.PARTIAL if had_unresolvable else RunStatus.COMPLETE
    save(final_status, "Run complete")
    emit("complete", "completed", f"Pipeline finished with status: {final_status.value}")
