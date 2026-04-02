"""Generate manuscript-centric run artifacts for operator review."""

from __future__ import annotations

import json
import re
import shutil
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
        source_count = 0
        file_count = 0
        urls: Set[str] = set()

        for source_result in gap_result.results:
            run_dir = Path(str(source_result.run_dir or "")).expanduser()
            if not run_dir.is_absolute():
                run_dir = (workspace / run_dir).resolve()
            if not run_dir.exists() or not run_dir.is_dir():
                continue

            source_id = _sanitize_name(str(source_result.source_id or "source"))
            dst_root = gap_root / "related_documents" / source_id
            copied = _copy_tree_files(run_dir, dst_root)
            if copied:
                source_count += 1
                file_count += copied
            urls.update(_extract_urls_from_json_files(run_dir))

        if urls:
            _write_urls_file(gap_root, sorted(urls))

        summaries[gap_id] = {
            "source_count": source_count,
            "file_count": file_count,
            "url_count": len(urls),
            "urls": sorted(urls),
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

    for index, gap in enumerate(gaps, start=1):
        gap_code = str(gap.gap_id or f"GAP-{index:03d}")
        chapter = str(gap.chapter or "Unknown Chapter")
        claim = str(gap.claim_text or "").strip() or "(no claim text)"
        excerpt = str(gap.source_text_excerpt or "").strip()
        doc_meta = copied_docs.get(gap_code, {})
        source_count = int(doc_meta.get("source_count", 0) or 0)
        file_count = int(doc_meta.get("file_count", 0) or 0)
        url_count = int(doc_meta.get("url_count", 0) or 0)

        lines.append(f"## {gap_code}")
        lines.append(f"- Code: `{gap_code}`")
        lines.append(f"- Chapter: {chapter}")
        lines.append(f"- Type: {str(gap.gap_type)}")
        lines.append(f"- Priority: {str(gap.priority)}")
        lines.append(f"- Claim: {claim}")
        lines.append(f"- Related Sources: {source_count}")
        lines.append(f"- Related Files: {file_count}")
        lines.append(f"- Related URLs: {url_count}")
        lines.append("- Snippet:")
        lines.append("")
        lines.append("```text")
        lines.append(excerpt if excerpt else "(no snippet captured by analyzer)")
        lines.append("```")
        lines.append("")

    out_path = bundle_root / f"gap_report_{rec.run_id}.md"
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_path


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

