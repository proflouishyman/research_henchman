"""Layer 4: ingest pull artifacts into evidence-hub-aligned local index."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from config import OrchestratorSettings
from contracts import GapPullResult, IngestResult, SourceResult


INGEST_INDEX_FILE = "ingest_index.jsonl"


def ingest_gap_result(
    pull_result: GapPullResult,
    settings: OrchestratorSettings,
    run_id: str,
) -> IngestResult:
    """Ingest one gap pull result set and return typed ingest summary."""

    if pull_result.status == "unresolvable":
        return IngestResult(
            gap_id=pull_result.gap_id,
            run_id=run_id,
            ingested=False,
            skipped=True,
            skip_reason="no documents pulled",
        )

    errors = []
    total_docs = 0

    for source_result in pull_result.results:
        if source_result.status == "failed":
            continue
        try:
            docs = _ingest_source_result(source_result, pull_result.gap_id, settings)
            total_docs += docs
        except Exception as exc:  # noqa: BLE001 - collect partial ingest errors.
            errors.append(str(exc)[:200])

    return IngestResult(
        gap_id=pull_result.gap_id,
        run_id=run_id,
        ingested=total_docs > 0,
        documents_upserted=total_docs,
        results_upserted=total_docs,
        fit_links_upserted=0,
        error="; ".join(errors) if errors else "",
    )


def _ingest_source_result(source_result: SourceResult, gap_id: str, settings: OrchestratorSettings) -> int:
    """Route source artifact to corresponding ingest routine."""

    if source_result.artifact_type == "ebsco_manifest_pair":
        return _run_ebsco_ingest(source_result, gap_id, settings)
    if source_result.artifact_type == "json_records":
        return _run_json_ingest(source_result, gap_id, settings)
    if source_result.artifact_type == "external_packet":
        return _run_external_ingest(source_result, gap_id, settings)
    raise RuntimeError(f"Unknown artifact_type: {source_result.artifact_type}")


def _append_ingest_index(settings: OrchestratorSettings, row: Dict[str, Any]) -> None:
    """Append one ingest trace row for fit stage scoping and observability."""

    out_path = settings.data_root / INGEST_INDEX_FILE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _run_json_ingest(source_result: SourceResult, gap_id: str, settings: OrchestratorSettings) -> int:
    """Ingest JSON records produced by source adapters."""

    root = Path(source_result.run_dir)
    count = 0
    for path in root.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        rows = payload if isinstance(payload, list) else [payload]
        for idx, row in enumerate(rows, start=1):
            _append_ingest_index(
                settings,
                {
                    "gap_id": gap_id,
                    "source_id": source_result.source_id,
                    "query": source_result.query,
                    "artifact": str(path),
                    "doc_id": f"{path.stem}_{idx}",
                    "record": row,
                },
            )
            count += 1
    return count


def _run_external_ingest(source_result: SourceResult, gap_id: str, settings: OrchestratorSettings) -> int:
    """Ingest generic external packets by indexing file metadata only."""

    root = Path(source_result.run_dir)
    count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        _append_ingest_index(
            settings,
            {
                "gap_id": gap_id,
                "source_id": source_result.source_id,
                "query": source_result.query,
                "artifact": str(path),
                "doc_id": path.stem,
                "record": {"size": path.stat().st_size, "name": path.name},
            },
        )
        count += 1
    return count


def _run_ebsco_ingest(source_result: SourceResult, gap_id: str, settings: OrchestratorSettings) -> int:
    """Run evidence-hub EBSCO ingester when available; fallback to metadata indexing."""

    run_root = Path(source_result.run_dir)
    run_id = run_root.parent.name if run_root.parent.name else run_root.name

    if settings.ingest_ebsco_script.exists():
        cmd = [
            sys.executable,
            str(settings.ingest_ebsco_script),
            "--workspace",
            str(settings.workspace),
            "--run-id",
            run_id,
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(settings.workspace),
            capture_output=True,
            text=True,
            timeout=max(60, settings.pull_timeout_seconds * 5),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ebsco ingest failed: {(proc.stderr or '').strip()[:200]}")

    # Index local artifact files regardless so fit stage can discover scope.
    return _run_external_ingest(source_result, gap_id, settings)
