"""Generate manuscript-centric run artifacts for operator review."""

from __future__ import annotations

import json
import re
import shutil
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from config import OrchestratorSettings
from contracts import GapPullResult, RunRecord

URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def export_run_bundle(rec: RunRecord, settings: OrchestratorSettings) -> Optional[Path]:
    """Write one manuscript-centered artifact bundle and return its root path.

    Layout:
    - `<ORCH_DATA_ROOT>/manuscript_exports/<manuscript title>/`
      - copied manuscript file
      - `gap_report_<run_id>.md`
      - `gaps/<gap_id>/...` with copied related source artifacts

    Assumptions:
    - `rec.manuscript_path` can be workspace-relative or absolute.
    - Pull results may contain missing/invalid run dirs; these are skipped.
    """

    manuscript_src = _resolve_path(rec.manuscript_path, settings.workspace)
    if not manuscript_src.exists() or not manuscript_src.is_file():
        return None

    bundle_root = _bundle_root(settings.data_root, manuscript_src)
    bundle_root.mkdir(parents=True, exist_ok=True)

    manuscript_copy = bundle_root / manuscript_src.name
    _safe_copy(manuscript_src, manuscript_copy)

    copied_docs = _copy_gap_documents(rec.pull_results, settings.workspace, bundle_root)
    _write_gap_report(bundle_root, rec, copied_docs)
    _write_manifest_json(bundle_root, rec, manuscript_src, manuscript_copy, copied_docs)
    return bundle_root


def _bundle_root(data_root: Path, manuscript_src: Path) -> Path:
    """Return deterministic bundle root for one manuscript title."""

    raw_title = manuscript_src.stem.strip() or "manuscript"
    safe_title = _sanitize_name(raw_title)
    return data_root / "manuscript_exports" / safe_title


def _sanitize_name(value: str) -> str:
    """Convert human title into filesystem-safe folder name."""

    cleaned = re.sub(r"[^\w\s\.-]+", "", value, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "manuscript"


def _resolve_path(raw_path: str, workspace: Path) -> Path:
    """Resolve absolute/relative manuscript path consistently."""

    p = Path(str(raw_path or "").strip())
    if p.is_absolute():
        return p.resolve()
    return (workspace / p).resolve()


def _safe_copy(src: Path, dst: Path) -> None:
    """Copy file with metadata preservation when possible."""

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_gap_documents(
    gap_pull_results: Iterable[GapPullResult],
    workspace: Path,
    bundle_root: Path,
) -> Dict[str, Dict[str, object]]:
    """Copy pulled artifact files into per-gap folders and return summary map."""

    summaries: Dict[str, Dict[str, object]] = {}
    for gap_result in gap_pull_results:
        gap_id = str(gap_result.gap_id or "").strip() or "UNKNOWN-GAP"
        gap_root = bundle_root / "gaps" / _sanitize_name(gap_id)
        source_ids: Set[str] = set()
        seen_roots: Set[str] = set()
        file_count = 0
        urls: Set[str] = set()
        quality = {"high": 0, "medium": 0, "seed": 0}

        for source_result in gap_result.results:
            run_dir = Path(str(source_result.run_dir or "")).expanduser()
            if not run_dir.is_absolute():
                run_dir = (workspace / run_dir).resolve()
            if not run_dir.exists() or not run_dir.is_dir():
                continue
            root_key = str(run_dir.resolve())
            if root_key in seen_roots:
                continue
            seen_roots.add(root_key)

            source_id = _sanitize_name(str(source_result.source_id or "source"))
            dst_root = gap_root / "related_documents" / source_id
            copied = _copy_tree_files(run_dir, dst_root)
            if copied:
                source_ids.add(source_id)
                file_count += copied
            urls.update(_extract_urls_from_json_files(run_dir))
            _merge_quality_counts(quality, _extract_quality_counts_from_json_files(run_dir))

        if urls:
            _write_urls_file(gap_root, sorted(urls))

        summaries[gap_id] = {
            "source_count": len(source_ids),
            "file_count": file_count,
            "url_count": len(urls),
            "urls": sorted(urls),
            "quality_high": int(quality.get("high", 0)),
            "quality_medium": int(quality.get("medium", 0)),
            "quality_seed": int(quality.get("seed", 0)),
        }
    return summaries


def _copy_tree_files(src_root: Path, dst_root: Path) -> int:
    """Copy all files under one source run folder to destination."""

    count = 0
    for src_path in sorted(src_root.rglob("*")):
        if not src_path.is_file():
            continue
        relative = src_path.relative_to(src_root)
        target = dst_root / relative
        _safe_copy(src_path, target)
        count += 1
    return count


def _extract_urls_from_json_files(root: Path, max_urls: int = 200) -> Set[str]:
    """Extract URL references from JSON artifacts for quick human review."""

    out: Set[str] = set()
    for json_file in sorted(root.rglob("*.json")):
        if len(out) >= max_urls:
            break
        try:
            raw = json_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for match in URL_RE.findall(raw):
            out.add(match.strip().rstrip(".,);]"))
            if len(out) >= max_urls:
                break
    return out


def _write_urls_file(gap_root: Path, urls: List[str]) -> None:
    """Persist related URL list for one gap."""

    if not urls:
        return
    gap_root.mkdir(parents=True, exist_ok=True)
    out = gap_root / "related_urls.txt"
    out.write_text("\n".join(urls) + "\n", encoding="utf-8")


def _write_gap_report(
    bundle_root: Path,
    rec: RunRecord,
    copied_docs: Dict[str, Dict[str, object]],
) -> Path:
    """Write markdown report listing coded gaps with snippets and pull summary."""

    gaps = list(rec.gap_map.gaps if rec.gap_map else [])
    lines: List[str] = []
    lines.append(f"# Gap Report - {rec.run_id}")
    lines.append("")
    lines.append(f"- Manuscript Path: `{rec.manuscript_path}`")
    lines.append(f"- Total Gaps: {len(gaps)}")
    lines.append("")
    plan_map = {
        str(gap.gap_id): gap
        for gap in (rec.research_plan.gaps if rec.research_plan else [])
        if str(gap.gap_id).strip()
    }
    pulled_gap_ids = {str(row.gap_id) for row in rec.pull_results if str(row.gap_id).strip()}

    for index, gap in enumerate(gaps, start=1):
        gap_code = str(gap.gap_id or f"GAP-{index:03d}")
        chapter = str(gap.chapter or "Unknown Chapter")
        claim = str(gap.claim_text or "").strip() or "(no claim text)"
        excerpt = str(gap.source_text_excerpt or "").strip()
        doc_meta = copied_docs.get(gap_code, {})
        source_count = int(doc_meta.get("source_count", 0) or 0)
        file_count = int(doc_meta.get("file_count", 0) or 0)
        url_count = int(doc_meta.get("url_count", 0) or 0)
        q_high = int(doc_meta.get("quality_high", 0) or 0)
        q_medium = int(doc_meta.get("quality_medium", 0) or 0)
        q_seed = int(doc_meta.get("quality_seed", 0) or 0)
        quality_note = _quality_note(q_high=q_high, q_medium=q_medium, q_seed=q_seed)
        plan_gap = plan_map.get(gap_code)
        pull_status = "pulled" if gap_code in pulled_gap_ids else "not_pulled"
        skip_reason = ""
        if plan_gap is not None and bool(getattr(plan_gap, "skip", False)):
            pull_status = "skipped"
            skip_reason = str(getattr(plan_gap, "skip_reason", "") or "").strip()
            quality_note = "Gap skipped by plan; no retrieval attempted."

        lines.append(f"## {gap_code}")
        lines.append(f"- Code: `{gap_code}`")
        lines.append(f"- Chapter: {chapter}")
        lines.append(f"- Type: {_display_enum(gap.gap_type)}")
        lines.append(f"- Priority: {_display_enum(gap.priority)}")
        lines.append(f"- Claim: {claim}")
        lines.append(f"- Related Sources: {source_count}")
        lines.append(f"- Related Files: {file_count}")
        lines.append(f"- Related URLs: {url_count}")
        lines.append(f"- Pull Status: {pull_status}")
        if skip_reason:
            lines.append(f"- Skip Reason: {skip_reason}")
        lines.append(f"- Quality Mix: high={q_high}, medium={q_medium}, seed={q_seed}")
        if quality_note:
            lines.append(f"- Quality Note: {quality_note}")
        lines.append("- Snippet:")
        lines.append("")
        lines.append("```text")
        lines.append(excerpt if excerpt else "(no snippet captured by analyzer)")
        lines.append("```")
        lines.append("")

    out_path = bundle_root / f"gap_report_{rec.run_id}.md"
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_path


def _display_enum(value: object) -> str:
    """Render enum values as simple strings in human-readable reports."""

    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _quality_note(*, q_high: int, q_medium: int, q_seed: int) -> str:
    """Return concise quality guidance for one gap's retrieved artifacts."""

    if q_high + q_medium + q_seed <= 0:
        return "No quality-labeled links were extracted."
    if q_high <= 0 and q_medium <= 0 and q_seed > 0:
        return "Seed-only retrieval. Improve by adding site-specific Playwright extraction for direct article/document URLs."
    if q_high <= 0 and q_medium > 0:
        return "No high-confidence links yet. Consider tighter entity/date queries for direct document captures."
    return ""


def _merge_quality_counts(target: Dict[str, int], inc: Dict[str, int]) -> None:
    """Merge quality buckets from one source folder into accumulator."""

    for key in ("high", "medium", "seed"):
        target[key] = int(target.get(key, 0) or 0) + int(inc.get(key, 0) or 0)


def _extract_quality_counts_from_json_files(root: Path) -> Dict[str, int]:
    """Count `quality_label` values in JSON artifacts for quality reporting."""

    counts = {"high": 0, "medium": 0, "seed": 0}
    for json_file in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        _walk_quality(payload, counts)
    return counts


def _walk_quality(node: object, counts: Dict[str, int]) -> None:
    """Recursively walk JSON payload for `quality_label` fields."""

    if isinstance(node, dict):
        raw = str(node.get("quality_label", "")).strip().lower()
        if raw in counts:
            counts[raw] += 1
        for value in node.values():
            _walk_quality(value, counts)
    elif isinstance(node, list):
        for item in node:
            _walk_quality(item, counts)


def _write_manifest_json(
    bundle_root: Path,
    rec: RunRecord,
    manuscript_src: Path,
    manuscript_copy: Path,
    copied_docs: Dict[str, Dict[str, object]],
) -> Path:
    """Write machine-readable metadata for the generated artifact bundle."""

    payload = {
        "run_id": rec.run_id,
        "manuscript_path": rec.manuscript_path,
        "manuscript_source": str(manuscript_src),
        "manuscript_copy": str(manuscript_copy),
        "gap_count": len(rec.gap_map.gaps if rec.gap_map else []),
        "gaps": copied_docs,
    }
    out_path = bundle_root / f"bundle_manifest_{rec.run_id}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_path
