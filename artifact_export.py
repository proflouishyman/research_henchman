"""Generate historian-friendly manuscript research artifacts.

Output layout (per manuscript, per run):

    manuscript_exports/
      <manuscript-title>/
        <manuscript-file>              ← copy of original
        _INDEX.md                      ← master cross-reference table
        _BIBLIOGRAPHY.md               ← all sources found, deduplicated
        gap_report_<run_id>.md         ← legacy flat report (compat)
        bundle_manifest_<run_id>.json  ← machine-readable metadata
        gaps/
          <ch-slug>--<claim-slug>/
            _README.md                 ← gap description + synthesis
            _SOURCES.md                ← bibliography for this gap only
            documents/
              <source_id>/             ← pulled artifacts per source
                ...
            related_urls.txt
        by_chapter/
          <chapter-slug>/
            <gap-slug> -> ../gaps/...  ← copies (no symlinks for portability)
        synthesis/
          <gap-slug>.md                ← Ollama-generated "what was found / what's missing"

Design principle: a historian opening any sub-folder should immediately
understand (1) which chapter and claim it addresses, (2) what evidence
was found, (3) what is still missing — without reading code or JSON.
"""

from __future__ import annotations

import json
import re
import shutil
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from config import OrchestratorSettings
from contracts import GapPullResult, RunRecord


URL_RE           = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
HREF_RE          = re.compile(r"""href=["']([^"'#]+)["']""", re.IGNORECASE)
DOC_EXTENSIONS   = {".pdf", ".doc", ".docx", ".txt", ".rtf"}
STATIC_ASSET_EXT = {".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                    ".ico", ".webp", ".woff", ".woff2", ".ttf", ".eot", ".map",
                    ".mp4", ".webm", ".mp3", ".wav", ".zip"}
MAX_FETCH_BYTES        = 4_000_000
FETCH_TIMEOUT_SECONDS  = 20
MAX_SEED_URLS_PER_SOURCE = 3
MAX_CHILD_LINKS_PER_SEED = 3


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve_gap_folder(rec: RunRecord, gap_id: str, settings: OrchestratorSettings) -> Optional[Path]:
    """Return the export folder path for one gap (may not exist if run hasn't exported yet)."""
    manuscript_src = _resolve_path(rec.manuscript_path, settings.workspace)
    if not manuscript_src.exists():
        return None
    bundle_root = _bundle_root(settings.data_root, manuscript_src)
    if rec.gap_map:
        for gap in rec.gap_map.gaps:
            if str(gap.gap_id) == str(gap_id):
                slug = _gap_slug(gap.gap_id, gap.chapter, gap.claim_text)
                return bundle_root / "gaps" / slug
    return bundle_root / "gaps" / _sanitize_name(gap_id)


def resolve_bundle_root(rec: RunRecord, settings: OrchestratorSettings) -> Optional[Path]:
    """Return the export bundle root folder for a run (may not exist yet)."""
    manuscript_src = _resolve_path(rec.manuscript_path, settings.workspace)
    if not manuscript_src.exists():
        return None
    return _bundle_root(settings.data_root, manuscript_src)


def export_run_bundle(rec: RunRecord, settings: OrchestratorSettings) -> Optional[Path]:
    """Write historian-friendly artifact bundle; return bundle root or None on error."""
    manuscript_src = _resolve_path(rec.manuscript_path, settings.workspace)
    if not manuscript_src.exists() or not manuscript_src.is_file():
        return None

    bundle_root = _bundle_root(settings.data_root, manuscript_src)
    bundle_root.mkdir(parents=True, exist_ok=True)

    # Copy original manuscript
    _safe_copy(manuscript_src, bundle_root / manuscript_src.name)

    # Fresh per-run export (remove stale gap folders from prior runs)
    _reset_gap_exports(bundle_root)

    # Resolve pulled artifacts into per-gap folder tree
    copied_docs = _copy_gap_documents(rec.pull_results, settings.workspace, bundle_root, rec)

    # Master index + bibliography
    _write_index(bundle_root, rec, copied_docs)
    _write_bibliography(bundle_root, rec, copied_docs)

    # Per-gap README + sources + optional synthesis
    _write_gap_readmes(bundle_root, rec, copied_docs, settings)

    # by_chapter/ mirror
    _write_chapter_mirror(bundle_root, rec)

    # Legacy flat report + manifest (backwards compat)
    _write_gap_report(bundle_root, rec, copied_docs)
    _write_manifest_json(bundle_root, rec, manuscript_src, bundle_root / manuscript_src.name, copied_docs)

    return bundle_root


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _bundle_root(data_root: Path, manuscript_src: Path) -> Path:
    raw_title = manuscript_src.stem.strip() or "manuscript"
    return data_root / "manuscript_exports" / _sanitize_name(raw_title)


def _resolve_path(raw_path: str, workspace: Path) -> Path:
    p = Path(str(raw_path or "").strip())
    return p.resolve() if p.is_absolute() else (workspace / p).resolve()


def _sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^\w\s\.-]+", "", value, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return (cleaned or "manuscript")[:80]


def _gap_slug(gap_id: str, chapter: str, claim_text: str) -> str:
    """Build a human-readable folder name from chapter + claim."""
    # Prefer chapter heading over generic gap_id
    ch = re.sub(r"(?i)chapter\s+", "ch", chapter or "")
    ch = _sanitize_name(ch or "gap")[:30]
    claim_short = _sanitize_name(claim_text[:60] if claim_text else gap_id)[:40]
    return f"{ch}--{claim_short}"


# ---------------------------------------------------------------------------
# Gap folder construction
# ---------------------------------------------------------------------------

def _reset_gap_exports(bundle_root: Path) -> None:
    for subdir in ("gaps", "by_chapter", "synthesis"):
        d = bundle_root / subdir
        if d.exists():
            shutil.rmtree(d)


def _copy_gap_documents(
    gap_pull_results: Iterable[GapPullResult],
    workspace: Path,
    bundle_root: Path,
    rec: RunRecord,
) -> Dict[str, Dict[str, Any]]:
    """Copy pulled artifacts into per-gap folders; return summary map keyed by gap_id."""
    # Build slug map from gap_map for human-readable folder names
    slug_map: Dict[str, Tuple[str, str, str]] = {}  # gap_id → (chapter, claim_text, slug)
    if rec.gap_map:
        for gap in rec.gap_map.gaps:
            slug = _gap_slug(gap.gap_id, gap.chapter, gap.claim_text)
            slug_map[gap.gap_id] = (gap.chapter, gap.claim_text, slug)

    summaries: Dict[str, Dict[str, Any]] = {}
    for gap_result in gap_pull_results:
        gap_id = str(gap_result.gap_id or "").strip() or "UNKNOWN-GAP"
        chapter, claim_text, slug = slug_map.get(gap_id, ("Unknown Chapter", "", _sanitize_name(gap_id)))

        gap_root = bundle_root / "gaps" / slug
        source_ids: Set[str] = set()
        seen_roots: Set[str] = set()
        file_count  = 0
        urls:  Set[str] = set()
        quality = {"high": 0, "medium": 0, "seed": 0}
        fetched_files = 0

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
            dst_root   = gap_root / "documents" / source_id
            copied     = _copy_tree_files(run_dir, dst_root)
            if copied:
                source_ids.add(source_id)
                file_count += copied
            urls.update(_extract_urls_from_json_files(run_dir))
            _merge_quality(quality, _extract_quality_counts(run_dir))
            fetch_meta    = _fetch_seed_documents_from_dir(dst_root)
            fetched_files += int(fetch_meta.get("fetched_files", 0) or 0)
            file_count    += int(fetch_meta.get("fetched_files", 0) or 0)
            _merge_quality(quality, fetch_meta.get("quality", {}))

        if urls:
            _write_urls_file(gap_root, sorted(urls))

        summaries[gap_id] = {
            "gap_id":      gap_id,
            "chapter":     chapter,
            "claim_text":  claim_text,
            "slug":        slug,
            "source_count": len(source_ids),
            "file_count":   file_count,
            "url_count":    len(urls),
            "urls":         sorted(urls),
            "quality_high":   int(quality.get("high",   0)),
            "quality_medium": int(quality.get("medium", 0)),
            "quality_seed":   int(quality.get("seed",   0)),
            "fetched_files":  fetched_files,
        }
    return summaries


# ---------------------------------------------------------------------------
# _INDEX.md  — master cross-reference
# ---------------------------------------------------------------------------

def _write_index(bundle_root: Path, rec: RunRecord, copied_docs: Dict[str, Dict[str, Any]]) -> None:
    gaps = list(rec.gap_map.gaps if rec.gap_map else [])
    plan_map = {str(g.gap_id): g for g in (rec.research_plan.gaps if rec.research_plan else [])}

    lines = [
        f"# Research Index: {Path(rec.manuscript_path).stem}",
        "",
        f"Run: `{rec.run_id}`  |  Gaps found: {len(gaps)}  |  Status: `{getattr(rec.status, 'value', rec.status)}`",
        "",
        "## Gap Cross-Reference",
        "",
        "| # | Chapter | Claim (short) | Type | Priority | Files | Quality | Confidence |",
        "|---|---------|---------------|------|----------|-------|---------|------------|",
    ]

    for idx, gap in enumerate(gaps, start=1):
        doc = copied_docs.get(gap.gap_id, {})
        plan_gap = plan_map.get(gap.gap_id)
        conf = f"{plan_gap.route_confidence:.0%}" if plan_gap and hasattr(plan_gap, "route_confidence") else "—"
        q_high   = int(doc.get("quality_high",   0))
        q_medium = int(doc.get("quality_medium", 0))
        q_seed   = int(doc.get("quality_seed",   0))
        quality  = f"H:{q_high} M:{q_medium} S:{q_seed}"
        claim_short = (gap.claim_text or "")[:60].replace("|", "/")
        slug = doc.get("slug", _sanitize_name(gap.gap_id))
        chapter_display = (gap.chapter or "Unknown")[:40]
        lines.append(
            f"| {idx} | {chapter_display} | [{claim_short}](gaps/{slug}/_README.md) "
            f"| {_val(gap.gap_type)} | {_val(gap.priority)} | {doc.get('file_count', 0)} "
            f"| {quality} | {conf} |"
        )

    lines += [
        "",
        "## How to Navigate",
        "",
        "- **`gaps/<chapter>--<claim>/`** — one folder per evidentiary gap",
        "  - `_README.md` — what the gap is, what was found, what's missing",
        "  - `_SOURCES.md` — bibliography for this gap",
        "  - `documents/<source>/` — pulled data files",
        "- **`by_chapter/`** — same folders re-organized by manuscript chapter",
        "- **`_BIBLIOGRAPHY.md`** — all sources across all gaps",
        "",
    ]
    (bundle_root / "_INDEX.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# _BIBLIOGRAPHY.md  — deduplicated source list
# ---------------------------------------------------------------------------

def _write_bibliography(bundle_root: Path, rec: RunRecord, copied_docs: Dict[str, Dict[str, Any]]) -> None:
    all_urls: Set[str] = set()
    for doc in copied_docs.values():
        for url in doc.get("urls", []):
            if url:
                all_urls.add(url)

    lines = [
        f"# Bibliography: {Path(rec.manuscript_path).stem}",
        "",
        f"Generated from run `{rec.run_id}`. {len(all_urls)} unique source URLs recovered.",
        "",
    ]

    by_domain: Dict[str, List[str]] = {}
    for url in sorted(all_urls):
        try:
            domain = urllib.parse.urlparse(url).netloc
        except Exception:
            domain = "unknown"
        by_domain.setdefault(domain, []).append(url)

    for domain in sorted(by_domain):
        lines.append(f"## {domain}")
        lines.append("")
        for url in sorted(by_domain[domain]):
            lines.append(f"- {url}")
        lines.append("")

    (bundle_root / "_BIBLIOGRAPHY.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-gap _README.md and _SOURCES.md
# ---------------------------------------------------------------------------

def _write_gap_readmes(
    bundle_root: Path,
    rec: RunRecord,
    copied_docs: Dict[str, Dict[str, Any]],
    settings: OrchestratorSettings,
) -> None:
    if not rec.gap_map:
        return
    plan_map = {str(g.gap_id): g for g in (rec.research_plan.gaps if rec.research_plan else [])}

    for gap in rec.gap_map.gaps:
        doc     = copied_docs.get(gap.gap_id, {})
        slug    = doc.get("slug") or _gap_slug(gap.gap_id, gap.chapter, gap.claim_text)
        gap_dir = bundle_root / "gaps" / slug
        gap_dir.mkdir(parents=True, exist_ok=True)

        plan_gap = plan_map.get(gap.gap_id)

        # Generate synthesis via LLM (best-effort; skipped on failure)
        synthesis = _generate_synthesis(gap, doc, plan_gap, settings)

        _write_readme(gap_dir, gap, doc, plan_gap, synthesis)
        _write_sources_md(gap_dir, gap, doc)

        # Write synthesis to synthesis/ folder as well
        if synthesis:
            synth_dir = bundle_root / "synthesis"
            synth_dir.mkdir(parents=True, exist_ok=True)
            synth_path = synth_dir / f"{slug}.md"
            synth_path.write_text(synthesis, encoding="utf-8")


def _write_readme(
    gap_dir: Path,
    gap: Any,
    doc: Dict[str, Any],
    plan_gap: Any,
    synthesis: str,
) -> None:
    chapter    = str(gap.chapter or "Unknown Chapter")
    claim      = str(gap.claim_text or "").strip() or "(no claim text)"
    excerpt    = str(gap.source_text_excerpt or "").strip()
    gap_type   = _val(gap.gap_type)
    priority   = _val(gap.priority)
    q_high     = int(doc.get("quality_high",   0))
    q_medium   = int(doc.get("quality_medium", 0))
    q_seed     = int(doc.get("quality_seed",   0))
    file_count = int(doc.get("file_count", 0))
    url_count  = int(doc.get("url_count",  0))

    conf_line  = ""
    kind_line  = ""
    need_line  = ""
    ladder_section = ""

    if plan_gap:
        conf = getattr(plan_gap, "route_confidence", None)
        if conf is not None:
            conf_line = f"**Route Confidence:** {float(conf):.0%}\n"
        ck = getattr(plan_gap, "claim_kind", None)
        en = getattr(plan_gap, "evidence_need", None)
        if ck:
            kind_line = f"**Claim Kind:** {_val(ck)}  \n"
        if en:
            need_line = f"**Evidence Need:** {_val(en)}  \n"

        # Accordion ladder summary
        ladder_data = getattr(plan_gap, "query_ladder", None)
        if ladder_data and isinstance(ladder_data, dict):
            queries = _format_ladder_queries(ladder_data)
            if queries:
                ladder_section = "\n## Search Queries Used\n\n" + queries

    lines = [
        f"# {chapter}: {claim[:80]}",
        "",
        f"**Gap Type:** {gap_type}  |  **Priority:** {priority}  ",
        kind_line + need_line + conf_line,
        "---",
        "",
        "## Manuscript Context",
        "",
        "```text",
        excerpt or "(no excerpt captured)",
        "```",
        "",
        "---",
        "",
        "## Evidence Retrieved",
        "",
        f"- Files pulled: **{file_count}**",
        f"- URLs collected: **{url_count}**",
        f"- Quality breakdown: High={q_high}  Medium={q_medium}  Seed={q_seed}",
        "",
        "| Source | Folder |",
        "|--------|--------|",
    ]

    docs_dir = gap_dir / "documents"
    if docs_dir.exists():
        for src_dir in sorted(docs_dir.iterdir()):
            if src_dir.is_dir():
                file_list = list(src_dir.rglob("*"))
                n = sum(1 for f in file_list if f.is_file())
                lines.append(f"| {src_dir.name} | `documents/{src_dir.name}/` ({n} files) |")
    else:
        lines.append("| — | No documents pulled |")

    lines += ["", "---", ""]

    if synthesis:
        lines += [synthesis, ""]
    else:
        lines += [
            "## Synthesis",
            "",
            "_Synthesis not available (LLM offline or skipped)._",
            "",
        ]

    if ladder_section:
        lines.append(ladder_section)

    (gap_dir / "_README.md").write_text("\n".join(lines), encoding="utf-8")


def _write_sources_md(gap_dir: Path, gap: Any, doc: Dict[str, Any]) -> None:
    lines = [
        f"# Sources: {str(gap.claim_text or gap.gap_id)[:80]}",
        "",
        f"Chapter: {gap.chapter or 'Unknown'}  |  Gap ID: `{gap.gap_id}`",
        "",
    ]

    urls = doc.get("urls", [])
    if urls:
        lines += ["## URLs", ""]
        for url in urls:
            lines.append(f"- {url}")
        lines.append("")
    else:
        lines += ["_No source URLs collected for this gap._", ""]

    (gap_dir / "_SOURCES.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# by_chapter/ mirror
# ---------------------------------------------------------------------------

def _write_chapter_mirror(bundle_root: Path, rec: RunRecord) -> None:
    """Create by_chapter/ with copies of gap folders organized by chapter slug."""
    if not rec.gap_map:
        return
    gaps_root = bundle_root / "gaps"
    if not gaps_root.exists():
        return

    chapters: Dict[str, List[Any]] = {}
    for gap in rec.gap_map.gaps:
        ch_slug = _sanitize_name(re.sub(r"(?i)chapter\s+", "ch", gap.chapter or "unknown"))[:40]
        chapters.setdefault(ch_slug, []).append(gap)

    for ch_slug, chapter_gaps in chapters.items():
        ch_dir = bundle_root / "by_chapter" / ch_slug
        ch_dir.mkdir(parents=True, exist_ok=True)
        for gap in chapter_gaps:
            gap_slug = _gap_slug(gap.gap_id, gap.chapter, gap.claim_text)
            src = gaps_root / gap_slug
            if not src.exists():
                continue
            dst = ch_dir / gap_slug
            if not dst.exists():
                shutil.copytree(src, dst)


# ---------------------------------------------------------------------------
# LLM synthesis per gap
# ---------------------------------------------------------------------------

def _generate_synthesis(gap: Any, doc: Dict[str, Any], plan_gap: Any, settings: OrchestratorSettings) -> str:
    """Ask the LLM to write a 'what was found / what's missing' synthesis for one gap."""
    if settings.llm_backend == "none":
        return ""

    try:
        from layers.llm_client import make_llm_client
        client = make_llm_client(
            settings,
            model=settings.llm_model,
            timeout_seconds=min(60, settings.llm_timeout_seconds),
            temperature=0.3,
        )

        q_high   = int(doc.get("quality_high",   0))
        q_medium = int(doc.get("quality_medium", 0))
        q_seed   = int(doc.get("quality_seed",   0))
        total    = q_high + q_medium + q_seed

        claim_kind = _val(getattr(plan_gap, "claim_kind",    "")) if plan_gap else ""
        evid_need  = _val(getattr(plan_gap, "evidence_need", "")) if plan_gap else ""

        prompt = f"""You are a research editor summarizing evidence retrieved for a historian.

GAP:
- Chapter: {gap.chapter or "Unknown"}
- Claim: {gap.claim_text or "(no claim)"}
- Gap Type: {_val(gap.gap_type)}
- Claim Kind: {claim_kind}
- Evidence Need: {evid_need}

RETRIEVED:
- Files pulled: {doc.get('file_count', 0)}
- High-quality documents: {q_high}
- Medium-quality documents: {q_medium}
- Seed links only: {q_seed}
- Total source signals: {total}

Write a brief synthesis in two sections:
1. **What Was Found** — what evidence was retrieved and how useful it likely is (2-3 sentences)
2. **What's Still Missing** — what the historian still needs to resolve this gap (2-3 sentences)

Be specific and honest. If only seed links were found, say so and explain what deeper search would help.
Write in plain prose. Do not use headers or bullet points inside the sections."""

        raw = client.complete(prompt=prompt)
        return _format_synthesis_output(raw)
    except Exception:  # noqa: BLE001 — synthesis is best-effort
        return ""


def _format_synthesis_output(raw: str) -> str:
    """Wrap raw LLM synthesis in consistent markdown sections."""
    text = raw.strip()
    if not text:
        return ""
    # If LLM already provided headers, preserve them; otherwise wrap
    if "## What Was Found" in text or "**What Was Found**" in text:
        return f"## Synthesis\n\n{text}"
    return f"## Synthesis\n\n{text}"


# ---------------------------------------------------------------------------
# Legacy flat gap report (backwards compat)
# ---------------------------------------------------------------------------

def _write_gap_report(
    bundle_root: Path,
    rec: RunRecord,
    copied_docs: Dict[str, Dict[str, Any]],
) -> Path:
    gaps     = list(rec.gap_map.gaps if rec.gap_map else [])
    plan_map = {str(g.gap_id): g for g in (rec.research_plan.gaps if rec.research_plan else [])}
    pulled   = {str(r.gap_id) for r in rec.pull_results if str(r.gap_id).strip()}

    lines  = [f"# Gap Report - {rec.run_id}", "", f"- Manuscript: `{rec.manuscript_path}`",
              f"- Total Gaps: {len(gaps)}", ""]

    for idx, gap in enumerate(gaps, start=1):
        code    = str(gap.gap_id or f"GAP-{idx:03d}")
        doc     = copied_docs.get(code, {})
        plan_g  = plan_map.get(code)
        slug    = doc.get("slug", _sanitize_name(code))
        lines  += [
            f"## Gap {idx}: [{(gap.claim_text or code)[:70]}](gaps/{slug}/_README.md)",
            f"- ID: `{code}`  Chapter: {gap.chapter or 'Unknown'}",
            f"- Type: {_val(gap.gap_type)}  Priority: {_val(gap.priority)}",
            f"- Files: {doc.get('file_count', 0)}  URLs: {doc.get('url_count', 0)}",
            f"- Quality: H={doc.get('quality_high',0)} M={doc.get('quality_medium',0)} S={doc.get('quality_seed',0)}",
            f"- Pull status: {'pulled' if code in pulled else 'not_pulled'}",
            "",
            "```text",
            str(gap.source_text_excerpt or "")[:220] or "(no excerpt)",
            "```",
            "",
        ]

    out = bundle_root / f"gap_report_{rec.run_id}.md"
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Machine-readable manifest
# ---------------------------------------------------------------------------

def _write_manifest_json(
    bundle_root: Path,
    rec: RunRecord,
    manuscript_src: Path,
    manuscript_copy: Path,
    copied_docs: Dict[str, Dict[str, Any]],
) -> Path:
    payload = {
        "run_id":           rec.run_id,
        "manuscript_path":  rec.manuscript_path,
        "manuscript_source": str(manuscript_src),
        "manuscript_copy":   str(manuscript_copy),
        "gap_count": len(rec.gap_map.gaps if rec.gap_map else []),
        "gaps": copied_docs,
    }
    out = bundle_root / f"bundle_manifest_{rec.run_id}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def _safe_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_tree_files(src_root: Path, dst_root: Path) -> int:
    count = 0
    for src_path in sorted(src_root.rglob("*")):
        if not src_path.is_file():
            continue
        target = dst_root / src_path.relative_to(src_root)
        _safe_copy(src_path, target)
        count += 1
    return count


def _write_urls_file(gap_root: Path, urls: List[str]) -> None:
    if not urls:
        return
    gap_root.mkdir(parents=True, exist_ok=True)
    (gap_root / "related_urls.txt").write_text("\n".join(urls) + "\n", encoding="utf-8")


def _extract_urls_from_json_files(root: Path, max_urls: int = 200) -> Set[str]:
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


def _extract_quality_counts(root: Path) -> Dict[str, int]:
    counts: Dict[str, int] = {"high": 0, "medium": 0, "seed": 0}
    for jf in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(jf.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        _walk_quality(payload, counts)
    return counts


def _walk_quality(node: Any, counts: Dict[str, int]) -> None:
    if isinstance(node, dict):
        raw = str(node.get("quality_label", "")).strip().lower()
        if raw in counts:
            counts[raw] += 1
        for v in node.values():
            _walk_quality(v, counts)
    elif isinstance(node, list):
        for item in node:
            _walk_quality(item, counts)


def _merge_quality(target: Dict[str, int], inc: Dict[str, Any]) -> None:
    for key in ("high", "medium", "seed"):
        target[key] = int(target.get(key, 0) or 0) + int(inc.get(key, 0) or 0)


# ---------------------------------------------------------------------------
# Seed URL fetching (follow provider links into local artifacts)
# ---------------------------------------------------------------------------

def _fetch_seed_documents_from_dir(source_root: Path) -> Dict[str, Any]:
    urls = _extract_seed_urls(source_root)
    if not urls:
        return {"fetched_files": 0, "quality": {"high": 0, "medium": 0, "seed": 0}}

    out_root = source_root / "_fetched_urls"
    out_root.mkdir(parents=True, exist_ok=True)
    seen: Set[str] = set()
    fetched = 0
    quality: Dict[str, int] = {"high": 0, "medium": 0, "seed": 0}

    for idx, url in enumerate(urls[:MAX_SEED_URLS_PER_SOURCE], start=1):
        if url in seen:
            continue
        seen.add(url)
        parent_save = _fetch_and_save(url=url, out_root=out_root, prefix=f"seed_{idx:02d}")
        if parent_save is None:
            continue
        fetched += 1
        _bump_quality(parent_save.suffix.lower(), quality)

        if parent_save.suffix.lower() in {".html", ".htm"}:
            html = parent_save.read_text(encoding="utf-8", errors="ignore")
            for cidx, child_url in enumerate(_extract_child_links(url, html)[:MAX_CHILD_LINKS_PER_SEED], start=1):
                if child_url in seen:
                    continue
                seen.add(child_url)
                child_save = _fetch_and_save(url=child_url, out_root=out_root,
                                              prefix=f"seed_{idx:02d}_child_{cidx:02d}")
                if child_save is None:
                    continue
                fetched += 1
                _bump_quality(child_save.suffix.lower(), quality)

    return {"fetched_files": fetched, "quality": quality}


def _extract_seed_urls(source_root: Path) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for jf in sorted(source_root.rglob("*.json")):
        try:
            payload = json.loads(jf.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for url in _walk_urls(payload):
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(url)
    return out


def _walk_urls(node: Any) -> List[str]:
    out: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and key.lower() in {"url", "link", "href"}:
                cand = value.strip()
                if cand.lower().startswith(("http://", "https://")):
                    out.append(cand)
            else:
                out.extend(_walk_urls(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_walk_urls(item))
    return out


def _extract_child_links(base_url: str, html: str) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    base_netloc = urllib.parse.urlparse(base_url).netloc.lower()
    for raw in HREF_RE.findall(html or ""):
        full   = urllib.parse.urljoin(base_url, raw.strip())
        parsed = urllib.parse.urlparse(full)
        if parsed.scheme not in {"http", "https"}:
            continue
        ext = Path(parsed.path).suffix.lower()
        if ext in STATIC_ASSET_EXT:
            continue
        if base_netloc and parsed.netloc.lower() != base_netloc:
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


def _fetch_and_save(url: str, out_root: Path, prefix: str) -> Optional[Path]:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            ct   = str(resp.headers.get("Content-Type", "")).lower()
            body = resp.read(MAX_FETCH_BYTES)
    except Exception:
        return None
    suffix = _suffix_for(url, ct, body)
    target = out_root / f"{prefix}{suffix}"
    target.write_bytes(body)
    return target


def _suffix_for(url: str, ct: str, body: bytes) -> str:
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if ext in DOC_EXTENSIONS.union({".html", ".htm"}):
        return ext
    if "pdf" in ct:
        return ".pdf"
    if "html" in ct or body.lstrip().startswith((b"<!doctype html", b"<html")):
        return ".html"
    if "text/plain" in ct:
        return ".txt"
    return ".bin"


def _bump_quality(suffix: str, quality: Dict[str, int]) -> None:
    if suffix in DOC_EXTENSIONS:
        quality["high"] = quality.get("high", 0) + 1
    elif suffix in {".html", ".htm"}:
        quality["medium"] = quality.get("medium", 0) + 1


# ---------------------------------------------------------------------------
# Accordion ladder formatting
# ---------------------------------------------------------------------------

def _format_ladder_queries(ladder_data: Dict[str, Any]) -> str:
    lines: List[str] = []
    for rung in ("constrained", "contextual", "broad", "fallback"):
        val = str(ladder_data.get(rung, "")).strip()
        if val:
            lines.append(f"- **{rung}:** `{val}`")
    ring = ladder_data.get("synonym_ring", {})
    if isinstance(ring, dict):
        shifts = ring.get("terminology_shifts", [])
        if shifts:
            lines.append(f"- *Era terms:* {', '.join(str(s) for s in shifts[:4])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Misc display helpers
# ---------------------------------------------------------------------------

def _val(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value or "")
