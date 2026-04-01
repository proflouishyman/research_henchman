"""Helpers for emitting clickable document links from adapter pulls.

These helpers provide deterministic, low-risk link rows when a source-specific
retrieval workflow is not yet implemented. Rows include:
- provider search URLs (always)
- best-effort local corpus document paths (when available)
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import Dict, List, Tuple


INDEX_REL_PATH = Path("codex/analysis_cache/source_index_summary.txt")
INDEX_LINE_RE = re.compile(r"^\s*\d+\s+words\s+\|\s+(.+)$")
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]{2,}")
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "there",
    "about",
    "without",
    "where",
    "which",
    "what",
    "were",
    "have",
    "has",
    "had",
    "chapter",
    "manuscript",
    "evidence",
}
GENERIC_QUERY_TERMS = {
    "section",
    "contains",
    "unsupported",
    "claims",
    "claim",
    "citation",
    "citations",
    "anchors",
    "assertions",
    "data",
    "numbers",
    "sources",
    "source",
    "references",
    "analysis",
    "argument",
    "historical",
    "narrative",
    "legal",
    "regulatory",
    "text",
}
DOC_EXTS = {".pdf", ".PDF", ".doc", ".docx", ".html", ".htm", ".txt", ".md"}


def provider_search_url(source_id: str, query: str) -> str:
    """Return provider-specific search URL for click-through."""

    encoded = urllib.parse.quote_plus(query.strip())
    source = (source_id or "").strip().lower()
    if source in {"ebsco_api", "ebscohost"}:
        return f"https://search.ebscohost.com/login.aspx?direct=true&bquery={encoded}"
    if source == "jstor":
        return f"https://www.jstor.org/action/doBasicSearch?Query={encoded}"
    if source == "project_muse":
        return f"https://muse.jhu.edu/search?action=search&query={encoded}"
    if source == "proquest_historical_newspapers":
        return f"https://www.proquest.com/search?queryTerm={encoded}"
    if source == "americas_historical_newspapers":
        return f"https://infoweb.newsbank.com/apps/readex/?p=EANX&q={encoded}"
    if source == "gale_primary_sources":
        return f"https://go.gale.com/ps/search?query={encoded}"
    if source == "statista":
        return f"https://www.statista.com/search/?q={encoded}"
    return f"https://duckduckgo.com/?q={encoded}"


def _workspace_root() -> Path:
    raw = os.environ.get("ORCH_WORKSPACE", "").strip()
    if raw:
        return Path(raw).resolve()
    return Path.cwd().resolve()


def _tokenize(text: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for token in TOKEN_RE.findall((text or "").lower()):
        if token in STOPWORDS or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _load_index_paths(root: Path, max_rows: int = 5000) -> List[Path]:
    """Load candidate document paths from prebuilt corpus summary index."""

    index_path = root / INDEX_REL_PATH
    if not index_path.exists():
        return []

    out: List[Path] = []
    count = 0
    for line in index_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if count >= max_rows:
            break
        match = INDEX_LINE_RE.match(line)
        if not match:
            continue
        rel = match.group(1).strip()
        if not rel:
            continue
        path = (root / rel).resolve()
        if not path.exists() or not path.is_file():
            continue
        if path.suffix not in DOC_EXTS:
            continue
        out.append(path)
        count += 1
    return out


def local_document_candidates(query: str, limit: int = 5) -> List[Tuple[Path, float]]:
    """Find best local corpus documents for a query using token overlap."""

    root = _workspace_root()
    tokens = _tokenize(query)
    if not tokens:
        return []
    informative = [tok for tok in tokens if tok not in GENERIC_QUERY_TERMS and len(tok) >= 4]
    # Do not manufacture high-quality local links from generic fallback prose.
    if len(informative) < 2:
        return []
    token_set = set(informative)

    scored: List[tuple[float, Path]] = []
    for path in _load_index_paths(root):
        context = " ".join(path.parts[-5:])
        doc_tokens = set(_tokenize(context))
        overlap = token_set.intersection(doc_tokens)
        if not overlap:
            continue
        score = float(len(overlap))
        score += (len(overlap) / max(1, len(token_set))) * 2.0
        if path.suffix.lower() == ".pdf":
            score += 0.5
        if "research" in context.lower():
            score += 0.25
        if score < 2.0:
            continue
        scored.append((score, path))

    scored.sort(key=lambda row: (row[0], str(row[1])), reverse=True)
    return [(path, score) for score, path in scored[:limit]]


def build_link_rows(source_id: str, query: str, gap_id: str, limit_local: int = 5) -> List[Dict[str, str]]:
    """Build normalized link rows ordered by evidence quality.

    Quality hierarchy:
    - `local_corpus` PDFs / full docs (highest)
    - other local corpus docs
    - provider search links (lowest; fallback seed)
    """

    rows: List[Dict[str, str]] = []

    for path, score in local_document_candidates(query, limit=limit_local):
        ext = path.suffix.lower()
        quality_rank = 100 if ext == ".pdf" else 88
        quality_rank += int(min(score, 8.0))
        rows.append(
            {
                "title": path.stem.replace("_", " "),
                "path": str(path),
                "query": query,
                "gap_id": gap_id,
                "link_type": "local_corpus",
                "quality_label": "high",
                "quality_rank": str(quality_rank),
            }
        )

    rows.append(
        {
            "title": f"{source_id} search results",
            "url": provider_search_url(source_id, query),
            "query": query,
            "gap_id": gap_id,
            "link_type": "provider_search",
            "quality_label": "seed",
            "quality_rank": "20",
        }
    )

    # Dedupe by link target while preserving order.
    out: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        key = row.get("url") or row.get("path") or row.get("title") or ""
        stable = str(key).strip().lower()
        if not stable or stable in seen:
            continue
        seen.add(stable)
        out.append(row)

    out.sort(
        key=lambda row: (
            int(str(row.get("quality_rank", "0")) or "0"),
            str(row.get("title", "")),
        ),
        reverse=True,
    )
    return out
