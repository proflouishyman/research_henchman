"""Layer 1: manuscript analysis -> GapMap."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from html import unescape as html_unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import OrchestratorSettings
from contracts import Gap, GapMap, GapPriority, GapType, from_primitive, to_primitive
from layers.llm_client import make_llm_client


EXPLICIT_PATTERNS = [
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
    re.compile(r"\bINSERT\b", re.IGNORECASE),
    re.compile(r"\bPLACEHOLDER\b", re.IGNORECASE),
    re.compile(r"\[citation needed\]", re.IGNORECASE),
    re.compile(r"\[(source|reference)\]", re.IGNORECASE),
    re.compile(r"\[(CHECK THIS|FIND STAT|ADD SOURCE)\]", re.IGNORECASE),
]

# ── Paragraph suspicion heuristics ───────────────────────────────────────────

_RE_CAUSAL    = re.compile(r"\b(because|led to|resulted in|caused|due to|therefore|consequently|thus|hence)\b", re.IGNORECASE)
_RE_QUANT     = re.compile(r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?%|\$\s?\d[\d,]*|\b\d{4}\b|\b\d+(?:\.\d+)?\s*(billion|million|thousand|percent)\b", re.IGNORECASE)
_RE_SUPERLAT  = re.compile(r"\b(most|largest|fastest|highest|lowest|best|worst|first|only|unprecedented|dramatic|significant|substantial)\b", re.IGNORECASE)
_RE_HEDGE     = re.compile(r"\b(perhaps|maybe|likely|appears|seems|suggests|could|might|possibly|probably|arguably)\b", re.IGNORECASE)
_RE_CITATION  = re.compile(r"\(\w[^)]{1,40}\d{4}\)|\[\d+\]|doi:|https?://|cf\.\s|ibid\.|op\.cit\.|et al\.", re.IGNORECASE)
_RE_EXPLICIT  = re.compile(r"TODO|FIXME|INSERT|\[citation needed\]|\[CHECK|FIND STAT|ADD SOURCE|PLACEHOLDER", re.IGNORECASE)


def _score_paragraph(text: str) -> int:
    """Heuristic suspicion score 0-100. Higher = more likely to contain evidence gaps."""
    if len(text.split()) < 8:
        return 0
    causal   = len(_RE_CAUSAL.findall(text))
    quant    = len(_RE_QUANT.findall(text))
    superlat = len(_RE_SUPERLAT.findall(text))
    hedge    = len(_RE_HEDGE.findall(text))
    cited    = len(_RE_CITATION.findall(text))
    explicit = len(_RE_EXPLICIT.findall(text))

    score = explicit * 35  # explicit markers are certain gaps
    if cited == 0:
        score += causal * 18 + quant * 15 + superlat * 10 + hedge * 8
    else:
        # citations mitigate but don't eliminate concern
        score += causal * 6 + quant * 5 + superlat * 3 + hedge * 3
    return min(score, 100)


def _split_paragraphs(text: str, min_words: int = 8) -> List[str]:
    """Split raw text on blank lines; discard very short fragments."""
    blocks = re.split(r"\n\s*\n", text)
    result = []
    for block in blocks:
        clean = re.sub(r"\s+", " ", block).strip()
        if len(clean.split()) >= min_words:
            result.append(clean)
    return result


def _annotate_paragraphs(text: str) -> List[Dict[str, Any]]:
    """Return list of {chapter, text} dicts preserving blank-line paragraph breaks.

    Uses heading detection from _split_sections logic but operates on raw text
    so blank-line paragraph boundaries are preserved.
    """
    chapter = "Manuscript Body"
    annotated: List[Dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text):
        clean = re.sub(r"\s+", " ", block).strip()
        if not clean:
            continue
        # Reuse heading detection
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        first = lines[0] if lines else ""
        low = first.lower()
        is_heading = (
            re.match(r"^chapter\s+[a-z0-9ivx]+", low)
            or low.startswith("introduction")
            or low.startswith("conclusion")
            or re.match(r"^(part|section)\s+[ivx0-9]+", low)
            or (first.isupper() and 2 <= len(first.split()) <= 12 and len(first) < 100)
        )
        if is_heading:
            chapter = first
            body = " ".join(lines[1:]).strip()
            if len(body.split()) >= 8:
                annotated.append({"chapter": chapter, "text": body})
        else:
            if len(clean.split()) >= 8:
                annotated.append({"chapter": chapter, "text": clean})
    return annotated


def analyze_manuscript(
    manuscript_path: str,
    settings: OrchestratorSettings,
    *,
    refresh: bool = False,
    emit_event: Optional[Any] = None,
) -> GapMap:
    """Analyze manuscript with Ollama-first fallback to heuristic rules."""

    cache_dir = settings.gap_map_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)

    path = _resolve_path(manuscript_path, settings.workspace)
    if not path.exists():
        return GapMap(
            manuscript_path=manuscript_path,
            manuscript_fingerprint="missing",
            gaps=[],
            analysis_method="heuristic",
            fallback_reason="manuscript_not_found",
        )

    fingerprint = _fingerprint(path)
    if not refresh:
        cached = _load_cached_gap_map(fingerprint, cache_dir)
        if cached is not None:
            return cached

    text, extraction_meta = _extract_text(path)
    gap_map: Optional[GapMap] = None
    fallback_reason = ""

    if settings.gap_analysis_use_ollama and text.strip():
        try:
            gap_map = _analyze_with_ollama(
                text, manuscript_path, fingerprint, extraction_meta, settings,
                emit_event=emit_event,
            )
        except Exception as exc:  # noqa: BLE001 - fallback is part of contract.
            fallback_reason = str(exc)[:200]

    if gap_map is None:
        gap_map = _analyze_heuristic(
            text,
            manuscript_path,
            fingerprint,
            extraction_meta,
            fallback_reason=fallback_reason,
        )

    _save_cached_gap_map(gap_map, cache_dir)
    return gap_map


def _resolve_path(manuscript_path: str, workspace: Path) -> Path:
    p = Path(manuscript_path)
    if p.is_absolute():
        return p.resolve()
    return (workspace / p).resolve()


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}::{stat.st_size}::{int(stat.st_mtime_ns)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cache_path(cache_dir: Path, fingerprint: str) -> Path:
    return cache_dir / f"gapmap_{fingerprint}.json"


def _load_cached_gap_map(fingerprint: str, cache_dir: Path) -> Optional[GapMap]:
    cache_path = _cache_path(cache_dir, fingerprint)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return from_primitive(GapMap, payload)


def _save_cached_gap_map(gap_map: GapMap, cache_dir: Path) -> None:
    cache_path = _cache_path(cache_dir, gap_map.manuscript_fingerprint or "unknown")
    cache_path.write_text(json.dumps(to_primitive(gap_map), ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_text(path: Path) -> Tuple[str, Dict[str, Any]]:
    """Extract manuscript text into plain UTF-8 string."""

    suffix = path.suffix.lower()
    meta: Dict[str, Any] = {"format": suffix, "status": "ok", "char_count": 0, "line_count": 0, "section_count": 0}
    text = ""

    try:
        if suffix in {".txt", ".md"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
        elif suffix == ".docx":
            text = _extract_docx(path)
        elif suffix == ".pdf":
            text = _extract_pdf(path)
        else:
            meta["status"] = "unsupported_format"
            return "", meta
    except Exception as exc:  # noqa: BLE001 - extraction failures become metadata.
        meta["status"] = "extract_failed"
        meta["error"] = str(exc)[:200]
        return "", meta

    sections = _split_sections(text)
    meta["char_count"] = len(text)
    meta["line_count"] = len(text.splitlines())
    meta["section_count"] = len(sections)
    return text, meta


def _extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"</w:tr>", "\n", xml)
    xml = re.sub(r"<[^>]+>", " ", xml)
    return html_unescape(xml)


def _extract_pdf(path: Path) -> str:
    """Best-effort PDF extraction without hard dependency requirements."""

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        # Keep fallback lightweight: decode raw bytes so analysis can still run.
        raw = path.read_bytes()
        return raw.decode("utf-8", errors="ignore")


def _split_sections(text: str) -> List[Dict[str, Any]]:
    """Split manuscript text into heading-based sections."""

    lines = [re.sub(r"\s+", " ", (line or "").strip()) for line in text.splitlines()]
    lines = [line for line in lines if line]

    def looks_like_heading(line: str) -> bool:
        low = line.lower()
        if re.match(r"^chapter\s+[a-z0-9ivx]+", low):
            return True
        if low.startswith("introduction") or low.startswith("conclusion"):
            return True
        if re.match(r"^(part|section)\s+[ivx0-9]+", low):
            return True
        if line.isupper() and 2 <= len(line.split()) <= 12 and len(line) < 100:
            return True
        return False

    sections: List[Dict[str, Any]] = []
    current = {"heading": "Manuscript Body", "lines": []}
    for line in lines:
        if looks_like_heading(line):
            if current["lines"]:
                sections.append(current)
            current = {"heading": line, "lines": []}
        else:
            current["lines"].append(line)
    if current["lines"]:
        sections.append(current)
    if not sections:
        return [{"heading": "Manuscript Body", "lines": lines}]
    return sections


def _find_explicit_markers(lines: List[str]) -> List[Tuple[str, str]]:
    """Return explicit marker/excerpt pairs from section lines."""

    hits: List[Tuple[str, str]] = []
    for line in lines:
        compact = re.sub(r"\s+", " ", line).strip()
        if not compact:
            continue
        for patt in EXPLICIT_PATTERNS:
            match = patt.search(compact)
            if not match:
                continue
            marker = match.group(0)
            hits.append((marker, compact[:220]))
            break
    return hits


def _count_hits(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


def _find_implicit_gaps(section_text: str) -> List[Tuple[str, GapPriority]]:
    """Infer implicit gaps using citation/number/hedge density heuristics."""

    findings: List[Tuple[str, GapPriority]] = []
    citation_count = _count_hits(r"\(\d{4}\)|\[\d+\]|doi|source:|https?://|www\.", section_text)
    number_count = _count_hits(r"\b\d{4}\b|\b\d+(?:\.\d+)?%|\$\s?\d+", section_text)
    hedge_count = _count_hits(r"\b(maybe|perhaps|likely|appears|seems|suggests|could|might)\b", section_text)

    if hedge_count >= 4 and citation_count == 0:
        findings.append(
            (
                "Section uses hedged language without supporting citations; causal claims need concrete sources.",
                GapPriority.HIGH,
            )
        )

    if len(section_text) >= 500 and citation_count < 2:
        findings.append(("Section makes claims without enough citations or source references.", GapPriority.HIGH))

    if len(section_text) >= 500 and number_count < 2:
        priority = GapPriority.MEDIUM if citation_count > 0 else GapPriority.HIGH
        findings.append(("Section includes quantitative assertions without data anchors.", priority))

    if len(section_text) > 280 and "\n" not in section_text:
        findings.append(("Argument is highly compressed; split claims and tie each to evidence.", GapPriority.LOW))

    # Ensure short but citation-free claim sections still produce at least one
    # implicit gap candidate instead of silently yielding only explicit TODOs.
    if not findings and citation_count == 0 and len(section_text) >= 80:
        findings.append(("Section contains unsupported claims without citation anchors.", GapPriority.MEDIUM))

    return findings


def _queries_from_claim(claim_text: str) -> List[str]:
    """Create 2-3 lightweight query variants for retrieval planning."""

    compact = re.sub(r"\s+", " ", claim_text).strip()
    if not compact:
        return []
    seed = compact[:180]
    return [
        seed,
        f"historical evidence {seed[:120]}",
        f"primary source {seed[:120]}",
    ]


def _analyze_heuristic(
    text: str,
    manuscript_path: str,
    fingerprint: str,
    extraction_meta: Dict[str, Any],
    fallback_reason: str = "",
) -> GapMap:
    """Heuristic gap analyzer for deterministic offline fallback."""

    sections = _split_sections(text)
    gaps: List[Gap] = []
    seen_claims: set[str] = set()

    for section_index, section in enumerate(sections, start=1):
        body = " ".join(section.get("lines", []))
        body_compact = re.sub(r"\s+", " ", body).strip()
        if len(body_compact) < 60:
            continue

        local_count = 0

        for marker, excerpt in _find_explicit_markers(section.get("lines", [])):
            claim = f"Unresolved explicit placeholder requires source evidence: '{marker}'."
            if claim in seen_claims:
                continue
            seen_claims.add(claim)
            local_count += 1
            gaps.append(
                Gap(
                    gap_id=f"AUTO-{section_index:02d}-G{local_count}",
                    chapter=section.get("heading", "Manuscript Body"),
                    claim_text=claim,
                    gap_type=GapType.EXPLICIT,
                    priority=GapPriority.HIGH,
                    suggested_queries=_queries_from_claim(claim),
                    source_text_excerpt=excerpt,
                    analysis_method="heuristic",
                )
            )

        for claim, priority in _find_implicit_gaps(body_compact):
            if claim in seen_claims:
                continue
            seen_claims.add(claim)
            local_count += 1
            gaps.append(
                Gap(
                    gap_id=f"AUTO-{section_index:02d}-G{local_count}",
                    chapter=section.get("heading", "Manuscript Body"),
                    claim_text=claim,
                    gap_type=GapType.IMPLICIT,
                    priority=priority,
                    suggested_queries=_queries_from_claim(claim),
                    analysis_method="heuristic",
                )
            )

    if not gaps and text.strip():
        default_claim = "Manuscript section needs source-backed evidence map for major claims."
        gaps = [
            Gap(
                gap_id="AUTO-01-G1",
                chapter="Manuscript Body",
                claim_text=default_claim,
                gap_type=GapType.IMPLICIT,
                priority=GapPriority.MEDIUM,
                suggested_queries=_queries_from_claim(default_claim),
                analysis_method="heuristic",
            )
        ]

    explicit_count = sum(1 for gap in gaps if gap.gap_type == GapType.EXPLICIT)
    implicit_count = sum(1 for gap in gaps if gap.gap_type == GapType.IMPLICIT)

    return GapMap(
        manuscript_path=manuscript_path,
        manuscript_fingerprint=fingerprint,
        gaps=gaps,
        section_count=int(extraction_meta.get("section_count", 0) or 0),
        char_count=int(extraction_meta.get("char_count", 0) or 0),
        explicit_count=explicit_count,
        implicit_count=implicit_count,
        analysis_method="heuristic",
        fallback_reason=fallback_reason,
    )


def _summarize_manuscript_thesis(text: str, client: Any) -> str:
    """One-sentence overall argument of the manuscript (cheap single call).
    Raises on LLM failure so callers can record the error."""
    sample = text[:6000]
    prompt = (
        "Read the opening of this manuscript and state its central argument or thesis "
        "in ONE sentence. Return only that sentence, nothing else.\n\nMANUSCRIPT OPENING:\n" + sample
    )
    return client.complete(prompt=prompt).strip()


def _summarize_chapter_role(chapter: str, body: str, thesis: str, client: Any) -> str:
    """One-sentence role of this chapter in advancing the overall argument.
    Returns empty string on failure — chapter context is nice-to-have, not required."""
    context = f"Manuscript thesis: {thesis}\n\n" if thesis else ""
    prompt = (
        f"{context}What role does the chapter \"{chapter}\" play in advancing "
        "the manuscript's argument? Answer in ONE sentence.\n\nCHAPTER OPENING:\n" + body[:2000]
    )
    try:
        return client.complete(prompt=prompt).strip()
    except Exception:
        return ""


def _build_paragraph_prompt(
    para: str,
    chapter: str,
    thesis: str,
    chapter_role: str,
    prev_para: str,
) -> str:
    """Focused prompt for a single paragraph with full argument context."""
    ctx_parts: List[str] = []
    if thesis:
        ctx_parts.append(f"MANUSCRIPT THESIS: {thesis[:200]}")
    if chapter_role:
        ctx_parts.append(f"THIS CHAPTER'S ROLE: {chapter_role[:200]}")
    if prev_para:
        ctx_parts.append(f"PRECEDING PARAGRAPH (context): {prev_para[:300]}")
    context_block = "\n".join(ctx_parts)

    return f"""{context_block}

PARAGRAPH TO ANALYZE (from chapter "{chapter}"):
{para}

Does this paragraph make any claims that lack adequate supporting evidence?
Consider its position in the argument above when judging whether a claim needs citation.

If yes, return a JSON array of gap objects. If no gaps, return [].
Each gap must have:
  chapter           — "{chapter}"
  claim_text        — the specific unsupported claim (1-2 sentences)
  gap_type          — "explicit" or "implicit"
  priority          — "high", "medium", or "low"
  suggested_queries — 2-3 search strings that would find relevant evidence
  excerpt           — verbatim text from the paragraph (max 200 chars)

Return ONLY a JSON array. No preamble, no fences.
"""


def _parse_gap_json(response: str) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(response)
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
    except json.JSONDecodeError:
        pass

    array_match = re.search(r"\[[\s\S]*\]", response)
    if not array_match:
        raise RuntimeError("analysis_response_not_json_array")
    payload = json.loads(array_match.group(0))
    if not isinstance(payload, list):
        raise RuntimeError("analysis_response_not_list")
    return [row for row in payload if isinstance(row, dict)]


def _parse_para_rows(response: str, chapter: str) -> List[Dict[str, Any]]:
    """Parse LLM response for one paragraph; return empty list on any error."""
    try:
        rows = _parse_gap_json(response)
        for row in rows:
            if not str(row.get("chapter", "")).strip():
                row["chapter"] = chapter
        return rows
    except Exception:
        return []


# Maximum paragraphs to send through LLM (after heuristic ranking).
_MAX_LLM_PARAGRAPHS = 80


def _analyze_with_ollama(
    text: str,
    manuscript_path: str,
    fingerprint: str,
    extraction_meta: Dict[str, Any],
    settings: OrchestratorSettings,
    emit_event: Optional[Any] = None,
) -> GapMap:
    client = make_llm_client(
        settings,
        model=settings.gap_analysis_model,
        timeout_seconds=settings.gap_analysis_timeout_seconds,
        temperature=0.1,
    )

    # ── Stage 1: build argument context (2 + N cheap calls) ──────────────────
    if emit_event:
        emit_event(stage="analyzing", status="progress",
                   message="Building manuscript argument context…", meta={})

    first_error: Optional[str] = None
    try:
        thesis = _summarize_manuscript_thesis(text, client)
    except Exception as exc:
        first_error = str(exc)[:200]
        thesis = ""

    annotated = _annotate_paragraphs(text)  # [{chapter, text}, …]

    # Summarize each chapter once (deduplicated)
    chapter_roles: Dict[str, str] = {}
    seen_chapters = list(dict.fromkeys(p["chapter"] for p in annotated))
    for ch_idx, chapter in enumerate(seen_chapters, start=1):
        if emit_event:
            emit_event(
                stage="analyzing", status="progress",
                message=f"Mapping chapter {ch_idx}/{len(seen_chapters)}: {chapter[:60]}",
                meta={"chapter": chapter, "chapter_idx": ch_idx, "total_chapters": len(seen_chapters)},
            )
        # Collect this chapter's paragraphs as its body text
        chapter_body = " ".join(p["text"] for p in annotated if p["chapter"] == chapter)
        chapter_roles[chapter] = _summarize_chapter_role(chapter, chapter_body, thesis, client)

    # ── Stage 2: heuristic scoring + ranking ─────────────────────────────────
    for para in annotated:
        para["score"] = _score_paragraph(para["text"])

    # Sort descending by suspicion; keep document order within ties via stable sort
    ranked = sorted(annotated, key=lambda p: p["score"], reverse=True)
    # Discard paragraphs with score 0 (unambiguously non-suspect)
    ranked = [p for p in ranked if p["score"] > 0]
    top = ranked[:_MAX_LLM_PARAGRAPHS]

    # Restore original document order for sliding-window prev_para to make sense
    original_indices = {id(p): i for i, p in enumerate(annotated)}
    top_sorted = sorted(top, key=lambda p: original_indices.get(id(p), 0))

    if emit_event:
        emit_event(
            stage="analyzing", status="progress",
            message=f"Selected {len(top_sorted)} paragraphs for deep analysis (of {len(annotated)} total)",
            meta={"total_paragraphs": len(annotated), "selected": len(top_sorted)},
        )

    # ── Stage 3: per-paragraph LLM analysis with sliding context ─────────────
    all_rows: List[Dict[str, Any]] = []

    # Build a fast lookup: paragraph text → index in annotated for prev_para
    annotated_texts = [p["text"] for p in annotated]

    for para_idx, para in enumerate(top_sorted, start=1):
        chapter = para["chapter"]
        para_text = para["text"]
        chapter_role = chapter_roles.get(chapter, "")

        # Sliding window: previous paragraph from the same sequence in annotated
        try:
            pos = annotated_texts.index(para_text)
            prev_para = annotated[pos - 1]["text"] if pos > 0 else ""
        except (ValueError, IndexError):
            prev_para = ""

        if emit_event:
            emit_event(
                stage="analyzing", status="progress",
                message=f"Analyzing paragraph {para_idx}/{len(top_sorted)} in '{chapter[:50]}'",
                meta={"para_idx": para_idx, "total": len(top_sorted),
                      "chapter": chapter, "score": para["score"]},
            )

        prompt = _build_paragraph_prompt(para_text, chapter, thesis, chapter_role, prev_para)
        try:
            response = client.complete(prompt=prompt)
            rows = _parse_para_rows(response, chapter)
            all_rows.extend(rows)
        except Exception as exc:
            if first_error is None:
                first_error = str(exc)[:200]

    gaps: List[Gap] = []
    seen_claims: set = set()
    for index, row in enumerate(all_rows, start=1):
        chapter = str(row.get("chapter", "Manuscript Body")).strip() or "Manuscript Body"
        claim_text = str(row.get("claim_text", "")).strip()
        if len(claim_text) < 12:
            continue
        # Deduplicate near-identical claims
        claim_key = re.sub(r"\s+", " ", claim_text[:80].lower())
        if claim_key in seen_claims:
            continue
        seen_claims.add(claim_key)

        gap_type_raw = str(row.get("gap_type", "implicit")).strip().lower()
        priority_raw = str(row.get("priority", "medium")).strip().lower()

        try:
            gap_type = GapType(gap_type_raw)
        except Exception:
            gap_type = GapType.IMPLICIT
        try:
            priority = GapPriority(priority_raw)
        except Exception:
            priority = GapPriority.MEDIUM

        suggested = row.get("suggested_queries", [])
        queries = [str(item).strip() for item in suggested if str(item).strip()] if isinstance(suggested, list) else []
        if not queries:
            queries = _queries_from_claim(claim_text)

        gaps.append(
            Gap(
                gap_id=f"AUTO-{index:02d}-G1",
                chapter=chapter,
                claim_text=claim_text,
                gap_type=gap_type,
                priority=priority,
                suggested_queries=queries,
                source_text_excerpt=str(row.get("excerpt", ""))[:220],
                analysis_method=f"ollama:{settings.gap_analysis_model}",
            )
        )

    if not gaps:
        raise RuntimeError(first_error or "analysis_no_gaps")

    explicit_count = sum(1 for gap in gaps if gap.gap_type == GapType.EXPLICIT)
    implicit_count = sum(1 for gap in gaps if gap.gap_type == GapType.IMPLICIT)
    return GapMap(
        manuscript_path=manuscript_path,
        manuscript_fingerprint=fingerprint,
        gaps=gaps,
        section_count=int(extraction_meta.get("section_count", 0) or 0),
        char_count=int(extraction_meta.get("char_count", 0) or 0),
        explicit_count=explicit_count,
        implicit_count=implicit_count,
        analysis_method="ollama",
        analysis_model=settings.gap_analysis_model,
    )


def _call_ollama(*, prompt: str, model: str, base_url: str, timeout_seconds: int) -> str:
    """Backwards-compat shim used by reflection.py. Delegates to LLMClient."""
    from layers.llm_client import LLMClient, LLMProvider
    client = LLMClient(
        provider=LLMProvider.OLLAMA,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        temperature=0.1,
    )
    return client.complete(prompt=prompt)
