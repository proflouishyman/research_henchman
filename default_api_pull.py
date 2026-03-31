#!/usr/bin/env python3
"""Default API pull command for orchestrator API mode.

Purpose:
- Provide a stable command contract when `ORCH_API_PULL_COMMAND` is not set.
- Return the newest compatible EBSCO run artifact so pull -> ingest can proceed.

Notes:
- This fallback does not create a brand-new upstream pull by itself.
- For live upstream API calls, set `ORCH_API_PULL_COMMAND` to your puller command.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable, List


def _iter_candidates(root: Path, patterns: Iterable[str]) -> List[Path]:
    out: List[Path] = []
    for pattern in patterns:
        for row in root.glob(pattern):
            if row.is_dir():
                out.append(row)
    return out


def _is_compatible_ebsco_run(run_dir: Path) -> bool:
    return (run_dir / "ebsco_search_results.csv").exists() and (run_dir / "ebsco_document_manifest.csv").exists()


def _resolve_latest_ebsco_run(workspace: Path, patterns: Iterable[str]) -> Path:
    ext = workspace / "codex" / "add_to_cart_audit" / "external_sources"
    candidates = [row for row in _iter_candidates(ext, patterns) if _is_compatible_ebsco_run(row)]
    if not candidates:
        raise SystemExit(
            "No compatible EBSCO run folder found. "
            "Set ORCH_API_PULL_COMMAND to a live puller command, or create a run under "
            "codex/add_to_cart_audit/external_sources/ebsco_api_run_*."
        )
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--provider", default="ebscohost")
    parser.add_argument(
        "--artifact-type",
        default="ebsco_manifest_pair",
        help="Artifact type consumed by ingest stage.",
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    provider = str(args.provider or "ebscohost").strip().lower()
    artifact_type = str(args.artifact_type or "ebsco_manifest_pair").strip() or "ebsco_manifest_pair"

    if provider != "ebscohost":
        raise SystemExit(
            f"Default API puller supports provider='ebscohost' only, received '{provider}'. "
            "Set ORCH_API_PULL_COMMAND for custom providers."
        )

    run_dir = _resolve_latest_ebsco_run(
        workspace,
        patterns=("ebsco_api_run_*", "ebsco_run_*"),
    )
    try:
        run_dir_rel = str(run_dir.relative_to(workspace))
    except ValueError:
        run_dir_rel = str(run_dir)

    payload = {
        "run_id": run_dir.name,
        "provider": provider,
        "run_dir": run_dir_rel,
        "artifact_type": artifact_type,
        "status": "completed",
        "stats": {"fallback": "latest_existing_run"},
    }
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()

