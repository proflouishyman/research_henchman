"""Server-side manuscript structure parser with on-disk cache.

Walks a .docx file via python-docx, producing a flat list of paragraph
records with heading context, footnote counts, bracketed TODOs, and char
offsets. The result is cached as JSON keyed by the file's (mtime, size) so
re-parsing only happens when the docx changes.

Public API:
  parse_manuscript(docx_path)         -> List[dict]  (cached)
  paragraph_gap_links(paras, conn)    -> Dict[str, List[str]]
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

# Lazy import so tests that mock this module don't need python-docx installed
# in unusual environments.
try:
    import docx as _docx_module
    from docx.oxml.ns import qn as _qn
    _DOCX_AVAILABLE = True
except ImportError:  # pragma: no cover
    _DOCX_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cache directory relative to the data root.
_CACHE_SUBDIR = ".manuscript_cache"

# Regex for the Pass-B bracketed TODO pattern — matches [text] where the
# inner text is at least 2 characters. Matches the pattern in gap_detector.py.
_BRACKET_RE = re.compile(r"\[([^\]]{2,})\]")

# Heading style name prefix from python-docx (e.g. "Heading 1", "Heading 2").
_HEADING_PREFIX = "Heading "


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_heading(para: Any) -> bool:
    """True when the paragraph's style is a Word Heading style."""
    return para.style.name.startswith(_HEADING_PREFIX)


def _heading_level(para: Any) -> int:
    """Return heading level (1-based) or 0 if not a heading."""
    name = para.style.name
    if name.startswith(_HEADING_PREFIX):
        try:
            return int(name[len(_HEADING_PREFIX):])
        except ValueError:
            return 1  # fallback: treat unknown heading style as level 1
    return 0


def _footnote_count(para: Any) -> int:
    """Count <w:footnoteReference> elements in a paragraph's XML subtree."""
    if not _DOCX_AVAILABLE:
        return 0
    try:
        refs = para._element.findall(".//" + _qn("w:footnoteReference"))
        return len(refs)
    except Exception:
        return 0


def _para_id(chapter_idx: int, section_idx: int, para_idx: int, text: str) -> str:
    """Stable SHA1 identifier for a paragraph.

    Uses (chapter_index, section_index, para_index, first 80 chars) so the
    id is stable across unchanged paragraphs even if later paragraphs shift.
    """
    key = f"{chapter_idx}:{section_idx}:{para_idx}:{text[:80]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _cache_path(docx_path: Path) -> Path:
    """Return the on-disk cache JSON path for this docx."""
    data_root = docx_path.resolve().parents[1]  # data/manuscript_exports/<dir>/<file>
    # Walk up to find the data directory (parent of manuscript_exports).
    # More robustly: cache next to the repo's data dir.
    repo_root = Path(__file__).resolve().parent.parent
    cache_dir = repo_root / "data" / _CACHE_SUBDIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", docx_path.name)
    return cache_dir / f"{safe_name}.json"


def _cache_key(docx_path: Path) -> str:
    """String key derived from file mtime + size; used to detect staleness."""
    stat = docx_path.stat()
    return f"{int(stat.st_mtime)}:{stat.st_size}"


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def _do_parse(docx_path: Path) -> List[Dict[str, Any]]:
    """Walk the docx and produce the flat paragraph list (uncached).

    Each dict has fields: para_id, chapter, heading_path, text, is_heading,
    heading_level, footnote_count, bracketed_todos, char_offset.
    """
    if not _DOCX_AVAILABLE:
        raise RuntimeError("python-docx is required for manuscript parsing")

    doc = _docx_module.Document(str(docx_path))
    paragraphs = doc.paragraphs

    result: List[Dict[str, Any]] = []
    char_offset = 0

    # Heading context stack — tracks the nearest ancestor heading at each level.
    # heading_stack[level] = title text
    heading_stack: Dict[int, str] = {}

    # Positional counters for para_id stability.
    chapter_idx = -1      # increments on Heading 1 changes
    section_idx = -1      # increments on Heading 2 changes; resets per chapter
    para_idx = 0          # running index within current section

    for para in paragraphs:
        text = para.text or ""
        hl = _heading_level(para)
        is_hdg = hl > 0

        if is_hdg:
            # Update heading stack: clear all levels below the current one.
            keys_to_remove = [k for k in heading_stack if k >= hl]
            for k in keys_to_remove:
                del heading_stack[k]
            heading_stack[hl] = text.strip()

            # Update positional counters
            if hl == 1:
                chapter_idx += 1
                section_idx = -1
                para_idx = 0
            elif hl == 2:
                section_idx += 1
                para_idx = 0
            else:
                para_idx += 1
        else:
            para_idx += 1

        # Nearest chapter heading (level 1)
        chapter = heading_stack.get(1, "")

        # heading_path = all current heading levels joined with " > "
        heading_path = " > ".join(
            heading_stack[k]
            for k in sorted(heading_stack.keys())
            if heading_stack[k]
        )

        # Bracketed TODOs
        bracketed_todos = _BRACKET_RE.findall(text)

        record: Dict[str, Any] = {
            "para_id":        _para_id(chapter_idx, section_idx, para_idx, text),
            "chapter":        chapter,
            "heading_path":   heading_path,
            "text":           text,
            "is_heading":     is_hdg,
            "heading_level":  hl,
            "footnote_count": _footnote_count(para),
            "bracketed_todos": bracketed_todos,
            "char_offset":    char_offset,
        }
        result.append(record)
        char_offset += len(text) + 1  # +1 for implicit newline

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_manuscript(docx_path: Path) -> List[Dict[str, Any]]:
    """Return the parsed paragraph list, using on-disk cache when not stale.

    Cache is keyed by (mtime, size) of the docx — any save invalidates it.
    Returns a fresh list each call (callers may mutate without side effects).
    """
    docx_path = Path(docx_path).resolve()
    cache_file = _cache_path(docx_path)
    current_key = _cache_key(docx_path)

    # Try to load from cache.
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text("utf-8"))
            if cached.get("_cache_key") == current_key:
                return list(cached["paragraphs"])
        except Exception:
            pass  # Stale or corrupt cache — fall through to re-parse.

    paragraphs = _do_parse(docx_path)

    # Persist to cache.
    try:
        cache_file.write_text(
            json.dumps({"_cache_key": current_key, "paragraphs": paragraphs}, ensure_ascii=False),
            "utf-8",
        )
    except Exception:
        pass  # Cache write failure is non-fatal.

    return paragraphs


def paragraph_gap_links(
    paragraphs: List[Dict[str, Any]],
    conn: sqlite3.Connection,
) -> Dict[str, List[str]]:
    """For each para_id, return the gap_ids that likely cover that paragraph.

    Three heuristics (union of matches):
      (a) heading_path of the paragraph is a substring of gap_tree.heading_path
          OR gap_tree.heading_path is a substring of the paragraph's heading_path.
      (b) The first 60 chars of any gap_tree.claim_text appear in the paragraph
          text (case-insensitive).
      (c) Any bracketed_todo in the paragraph matches a Pass-B gap_tree row by
          a 40-char prefix of claim_text (case-insensitive).

    Returns {para_id: [gap_id, ...]} — only paras with at least one match
    are included; unmatched paras are absent from the dict.
    """
    # Fetch all gap_tree rows once.
    try:
        rows = conn.execute(
            "SELECT gap_id, heading_path, claim_text, detector_pass FROM gap_tree"
        ).fetchall()
    except Exception:
        return {}

    # Pre-process gap data for matching.
    gap_data: List[Dict[str, str]] = []
    for r in rows:
        gap_data.append({
            "gap_id":       str(r["gap_id"] or ""),
            "heading_path": str(r["heading_path"] or "").strip().lower(),
            "claim_text":   str(r["claim_text"] or "").strip(),
            "detector_pass": str(r["detector_pass"] or ""),
        })

    result: Dict[str, List[str]] = {}

    for para in paragraphs:
        pid = para["para_id"]
        para_heading = para.get("heading_path", "").strip().lower()
        para_text_lower = para.get("text", "").lower()
        todos_lower = [t.lower() for t in para.get("bracketed_todos", [])]

        matched: List[str] = []
        seen: set = set()

        for gd in gap_data:
            gid = gd["gap_id"]
            if gid in seen:
                continue

            # (a) heading path overlap
            gap_hdg = gd["heading_path"]
            if para_heading and gap_hdg:
                if gap_hdg in para_heading or para_heading in gap_hdg:
                    matched.append(gid)
                    seen.add(gid)
                    continue

            # (b) claim text prefix in paragraph body
            claim = gd["claim_text"]
            if claim and len(claim) >= 10:
                prefix = claim[:60].lower()
                if prefix in para_text_lower:
                    matched.append(gid)
                    seen.add(gid)
                    continue

            # (c) bracketed TODO matches a Pass-B claim text
            if todos_lower and gd["detector_pass"] == "B":
                claim_prefix = claim[:40].lower()
                for todo in todos_lower:
                    if claim_prefix and (claim_prefix in todo or todo in claim_prefix):
                        matched.append(gid)
                        seen.add(gid)
                        break

        if matched:
            result[pid] = matched

    return result


# ---------------------------------------------------------------------------
# Structure grouping helper (used by the API endpoint)
# ---------------------------------------------------------------------------

def group_into_chapters(
    paragraphs: List[Dict[str, Any]],
    gap_links: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    """Group the flat paragraph list into a chapters/sections/paragraphs tree.

    Returns:
      [
        {
          "title": str,
          "sections": [
            {
              "heading": str,
              "paragraphs": [
                {para_id, text, footnote_count, bracketed_todos, gap_ids}
              ]
            }
          ]
        }
      ]

    Heading-level 1 paragraphs become chapter boundaries.
    Heading-level 2 paragraphs become section boundaries.
    Deeper headings are kept as section-level paragraphs (is_heading=True).
    """
    from layers.dossier_render import chapter_slug  # avoid circular at top

    chapters: List[Dict[str, Any]] = []
    current_chapter: Optional[Dict[str, Any]] = None
    current_section: Optional[Dict[str, Any]] = None

    def _ensure_chapter(title: str) -> None:
        nonlocal current_chapter, current_section
        current_chapter = {"title": title, "slug": chapter_slug(title), "sections": []}
        current_section = None
        chapters.append(current_chapter)

    def _ensure_section(heading: str) -> None:
        nonlocal current_section
        if current_chapter is None:
            _ensure_chapter("(preamble)")
        current_section = {"heading": heading, "paragraphs": []}
        current_chapter["sections"].append(current_section)  # type: ignore[index]

    def _add_para(para: Dict[str, Any]) -> None:
        nonlocal current_chapter, current_section
        if current_chapter is None:
            _ensure_chapter("(preamble)")
        if current_section is None:
            _ensure_section("")
        gids = gap_links.get(para["para_id"], [])
        current_section["paragraphs"].append({  # type: ignore[index]
            "para_id":         para["para_id"],
            "text":            para["text"],
            "is_heading":      para["is_heading"],
            "heading_level":   para["heading_level"],
            "footnote_count":  para["footnote_count"],
            "bracketed_todos": para["bracketed_todos"],
            "gap_ids":         gids,
        })

    for para in paragraphs:
        hl = para.get("heading_level", 0)
        if hl == 1:
            _ensure_chapter(para["text"].strip() or "(untitled chapter)")
        elif hl == 2:
            _ensure_section(para["text"].strip() or "(untitled section)")
            _add_para(para)
        else:
            _add_para(para)

    return chapters
