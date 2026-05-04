"""Shared dossier-render layer.

Single source of truth for rendering a per-gap dossier from the scored
``articles`` table. Both the markdown generator (``scripts/generate_dossiers.py``)
and the Library API (``/api/library/gaps/{gap_id}/dossier``) call into the
same helpers here so the two surfaces are guaranteed to match in structure.

Public API:

  * ``norm_title(title)`` — fuzzy-title normalization key.
  * ``chapter_slug(topic)`` — directory-safe slug for chapter folders.
  * ``src_label(source_id)`` — human-readable source label.
  * ``absolutize_url(url, source_id)`` — rewrite path-only URLs to absolute.
  * ``pick_primary(rows)`` — choose the best copy from a fuzzy-title group.
  * ``dedupe_within_gap(rows)`` — group fuzzy duplicates within a gap.
  * ``build_cross_gap_index(conn)`` — title → list of gap_ids (score>=1).
  * ``assemble_dossier(conn, gap_id, *, cross_gap_idx=None)`` —
        high-level structured dossier builder used by both the markdown
        generator and the API endpoint.

The data shape returned by ``assemble_dossier`` is documented in the
docstring of that function and mirrored in ``contracts.LibraryDossier``.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Source priority + labels
# ---------------------------------------------------------------------------

# Lower number = higher priority when picking the "primary" copy from a
# fuzzy-title group. Reuses the legacy ranking established in the markdown
# dossier generator.
SOURCE_PRIORITY: Dict[str, int] = {
    "ebsco_api": 0,
    "proquest_us_newsstream": 1,
    "proquest_international_newsstream": 2,
    "proquest_historical_newspapers": 3,
    "hathitrust_fulltext": 4,
    "sec_edgar": 5,
}

_SOURCE_LABELS: Dict[str, str] = {
    "ebsco_api":                          "EBSCO",
    "proquest_us_newsstream":             "ProQuest US News",
    "proquest_international_newsstream":  "ProQuest Intl News",
    "proquest_historical_newspapers":     "ProQuest Historical",
    "hathitrust_fulltext":                "HathiTrust",
    "sec_edgar":                          "SEC EDGAR",
}


def src_label(source_id: str) -> str:
    """Render source_id more readably."""
    return _SOURCE_LABELS.get(source_id, source_id)


# ---------------------------------------------------------------------------
# Title normalization
# ---------------------------------------------------------------------------

_NORM_RE = re.compile(r"[^a-z0-9 ]")


def norm_title(t: Optional[str]) -> str:
    """Normalize a title for fuzzy duplicate detection.

    Lowercase, drop non-alphanumerics, collapse whitespace, truncate.
    Two-letter+ titles only — empty / single-char results return ''."""
    if not t:
        return ""
    s = _NORM_RE.sub(" ", t.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200] if len(s) >= 4 else ""


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def chapter_slug(topic: Optional[str]) -> str:
    """Slugify a chapter/topic string for use as a directory name."""
    if not topic:
        return "00_uncategorized"
    s = _SLUG_RE.sub("_", topic.lower()).strip("_")
    return s[:80] or "00_uncategorized"


# ---------------------------------------------------------------------------
# URL absolutization
# ---------------------------------------------------------------------------

# Some seed records (notably EBSCO) store path-only URLs. The host depends
# on the source, so we keep the table local rather than hard-coding it
# into individual rendering helpers.
_SOURCE_HOSTS: Dict[str, str] = {
    "ebsco_api":           "https://research.ebsco.com",
    "hathitrust_fulltext": "https://babel.hathitrust.org",
}


def absolutize_url(url: Optional[str], source_id: str) -> str:
    """Return an absolute URL, prepending the source-specific host when needed.

    Empty input returns ``""`` rather than ``None`` so callers can string-
    concatenate without nullability checks. Already-absolute URLs are
    returned untouched. Path-only URLs are absolutized using the host
    table; unknown sources with path-only URLs fall back to the raw value.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("/"):
        host = _SOURCE_HOSTS.get(source_id, "")
        if host:
            return host + raw
    return raw


# ---------------------------------------------------------------------------
# Markdown link helper (used by the markdown writer; kept here so its
# behavior matches what the API encodes in DossierEntry.pdf_path / .url)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def render_url_or_pdf(primary: sqlite3.Row) -> str:
    """Return a markdown link string preferring local PDF over remote URL."""
    pdf_path = (primary["pdf_path"] or "").strip()
    url = absolutize_url((primary["url"] or "").strip(), primary["source_id"])
    if pdf_path:
        try:
            p = Path(pdf_path)
            if p.is_absolute():
                p = p.relative_to(_PROJECT_ROOT)
            return f"[PDF]({p}) · [URL]({url})" if url else f"[PDF]({p})"
        except Exception:
            return f"[PDF]({pdf_path}) · [URL]({url})" if url else f"[PDF]({pdf_path})"
    if url:
        return f"[URL]({url})"
    return "(no link)"


# ---------------------------------------------------------------------------
# Primary picker + within-gap dedupe
# ---------------------------------------------------------------------------

def pick_primary(rows: List[sqlite3.Row]) -> Tuple[sqlite3.Row, List[sqlite3.Row]]:
    """From a list of fuzzy-title-equivalent rows in the same gap, pick the
    primary (best) copy. Returns ``(primary, [other_copies])``.

    Ranking, lower-is-better:
      1. has PDF on disk
      2. has DOI
      3. source priority (EBSCO first, HathiTrust last)
      4. has abstract
      5. articles.id (deterministic tie-break)
    """
    def rank(r: sqlite3.Row) -> tuple:
        has_pdf = 0 if r["pdf_path"] else 1
        has_doi = 0 if (r["doi"] or "").strip() else 1
        has_abs = 0 if (r["abstract"] or "").strip() else 1
        src     = SOURCE_PRIORITY.get(r["source_id"], 99)
        return (has_pdf, has_doi, src, has_abs, r["id"])
    sorted_rows = sorted(rows, key=rank)
    return sorted_rows[0], sorted_rows[1:]


def dedupe_within_gap(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """Group fuzzy-title-equivalent rows within the gap.

    Returns a list of consolidated entries, one per unique normalized
    title (or one per row if its title fails the norm filter).

    Each entry dict contains:
      * ``norm``    — normalized title key (or ``""`` for standalone rows)
      * ``primary`` — the chosen primary sqlite3.Row
      * ``others``  — the dropped copies (other sqlite3.Rows)
      * ``sources`` — sorted list of all source_ids in the group
    """
    groups: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    standalone: List[sqlite3.Row] = []
    for r in rows:
        n = norm_title(r["title"])
        if n:
            groups[n].append(r)
        else:
            standalone.append(r)

    out: List[Dict[str, Any]] = []
    for n, grp in groups.items():
        primary, others = pick_primary(grp)
        all_sources = sorted({r["source_id"] for r in grp})
        out.append({
            "norm":     n,
            "primary":  primary,
            "others":   others,
            "sources":  all_sources,
        })
    for r in standalone:
        out.append({
            "norm":     "",
            "primary":  r,
            "others":   [],
            "sources":  [r["source_id"]],
        })
    return out


# ---------------------------------------------------------------------------
# Cross-gap index
# ---------------------------------------------------------------------------

def build_cross_gap_index(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    """Map ``norm_title(title)`` → sorted list of gap_ids that have a scored
    row for this title (relevance_score >= 1).

    Tier-0 rows are *not* included — those are the LLM's noise calls and
    surfacing them in cross-gap notes inflates the appearance of overlap.
    """
    rows = conn.execute(
        """SELECT title, gap_id, relevance_score
             FROM articles
            WHERE relevance_score IS NOT NULL AND relevance_score >= 1
              AND title IS NOT NULL"""
    ).fetchall()
    idx: Dict[str, set] = defaultdict(set)
    for r in rows:
        n = norm_title(r["title"])
        if n:
            idx[n].add(r["gap_id"])
    return {k: sorted(v) for k, v in idx.items()}


# ---------------------------------------------------------------------------
# Gap row fetcher + structured dossier builder
# ---------------------------------------------------------------------------

def fetch_gap_rows(conn: sqlite3.Connection, gap_id: str) -> List[sqlite3.Row]:
    """Return all article rows for a gap, with the columns the dossier needs."""
    return conn.execute(
        """SELECT id, title, authors, journal, pub_date, abstract, doi,
                  url, pdf_path, source_id, gap_id, gap_topic,
                  gap_research_question,
                  relevance_score, relevance_why
             FROM articles WHERE gap_id = ?""",
        (gap_id,),
    ).fetchall()


def _entry_to_primitive(
    entry: Dict[str, Any],
    *,
    self_gap: str,
    cross_gap_refs: List[str],
) -> Dict[str, Any]:
    """Convert an internal consolidated entry into the API DossierEntry dict.

    ``cross_gap_refs`` is the list of *other* gap_ids that contain this
    fuzzy-title at score >= 1; the caller has already filtered out
    ``self_gap``.
    """
    p = entry["primary"]
    primary_src = str(p["source_id"] or "")
    also_in = [s for s in entry["sources"] if s and s != primary_src]
    return {
        "id":              int(p["id"]),
        "title":           (p["title"] or "").strip(),
        "authors":         (p["authors"] or "").strip(),
        "pub_date":        (p["pub_date"] or "").strip(),
        "journal":         (p["journal"] or "").strip(),
        "abstract":        (p["abstract"] or "").strip(),
        "doi":             (p["doi"] or "").strip(),
        "url":             absolutize_url(p["url"], primary_src),
        "pdf_path":        (p["pdf_path"] or "").strip(),
        "source_id":       primary_src,
        "also_in_sources": sorted(also_in),
        "relevance_score": int(p["relevance_score"]) if p["relevance_score"] is not None else None,
        "relevance_why":   (p["relevance_why"] or "").strip(),
        "cross_gap_refs":  list(cross_gap_refs),
    }


def _gap_tree_row(conn: sqlite3.Connection, gap_id: str) -> Optional[sqlite3.Row]:
    """Return the gap_tree row for *gap_id*, or None if the table/row
    doesn't exist. Tolerant — the API reads gap_tree but the markdown
    generator only needs the article rows."""
    try:
        row = conn.execute(
            "SELECT * FROM gap_tree WHERE gap_id = ?",
            (gap_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row


def assemble_dossier(
    conn: sqlite3.Connection,
    gap_id: str,
    *,
    cross_gap_idx: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Build the structured dossier for one gap.

    Returns a dict shaped like::

        {
          "gap": {
            "gap_id":            str,
            "chapter":           str,                # gap_tree.chapter (preferred) or articles.gap_topic
            "claim_text":        str,                # gap_tree.claim_text
            "research_question": str,                # gap_tree.research_question
            "evidence_target":   int,                # gap_tree.evidence_target
            "tier":              int | None,         # gap_tree.tier
            "gap_type":          str,                # gap_tree.gap_type
            "status":            str,                # gap_tree.status
            "detector_pass":     str | None,
            "rationale":         str,
            "heading_path":      str,
          },
          "summary": {
            "total_rows":     int,                   # raw articles row count
            "consolidated":   int,                   # post-dedupe count
            "tier_counts":    {"3": int, "2": int, "1": int, "0": int, "unscored": int}
          },
          "tiers": {
            "3":         [DossierEntry, ...],
            "2":         [...],
            "1":         [...],
            "0":         [...],
            "unscored":  [...]
          }
        }

    *cross_gap_idx* may be supplied by callers that batch-render many
    dossiers (saves rebuilding the corpus-wide index per gap). When
    omitted it is computed on demand.
    """
    rows = fetch_gap_rows(conn, gap_id)
    consolidated = dedupe_within_gap(rows)

    # Per-tier buckets keyed by string ("0"-"3" or "unscored") so the
    # JSON response is dict-friendly.
    tiers: Dict[str, List[Dict[str, Any]]] = {
        "3": [], "2": [], "1": [], "0": [], "unscored": [],
    }
    if cross_gap_idx is None:
        cross_gap_idx = build_cross_gap_index(conn)

    for entry in consolidated:
        score = entry["primary"]["relevance_score"]
        bucket = "unscored" if score is None else str(int(score))
        if bucket not in tiers:
            # Defensive: legacy rows could carry an unexpected score; bucket
            # them under "unscored" rather than dropping silently.
            bucket = "unscored"
        # Cross-gap refs: gaps OTHER than this one where the same fuzzy
        # title appears at score >= 1.
        norm = entry["norm"]
        other_gaps = [g for g in cross_gap_idx.get(norm, []) if g != gap_id] if norm else []
        tiers[bucket].append(_entry_to_primitive(entry, self_gap=gap_id, cross_gap_refs=other_gaps))

    # Order each tier: source priority then pub-date desc (matches markdown).
    def _sort_key(e: Dict[str, Any]) -> tuple:
        src_rank = SOURCE_PRIORITY.get(e.get("source_id", ""), 99)
        date = (e.get("pub_date") or "").strip()
        m = re.search(r"\d{4}", date)
        year = -int(m.group()) if m else 0
        return (src_rank, year)
    for bucket in tiers:
        tiers[bucket].sort(key=_sort_key)

    # Counts
    tier_counts = {b: len(v) for b, v in tiers.items()}
    summary = {
        "total_rows":   len(rows),
        "consolidated": len(consolidated),
        "tier_counts":  tier_counts,
    }

    # Gap context: prefer gap_tree row (richer); fall back to anything we
    # can derive from the article rows so the markdown writer keeps working
    # against legacy AUTO-NN-G1 gaps that aren't in gap_tree.
    gt = _gap_tree_row(conn, gap_id)
    if gt is not None:
        gap_block: Dict[str, Any] = {
            "gap_id":            str(gt["gap_id"]),
            "chapter":           (gt["chapter"] or "").strip(),
            "claim_text":        (gt["claim_text"] or "").strip(),
            "research_question": (gt["research_question"] or "").strip(),
            "evidence_target":   int(gt["evidence_target"]) if gt["evidence_target"] is not None else 0,
            "tier":              int(gt["tier"]) if gt["tier"] is not None else None,
            "gap_type":          (gt["gap_type"] or "").strip(),
            "status":            (gt["status"] or "").strip(),
            "detector_pass":     (gt["detector_pass"] or "").strip(),
            "rationale":         (gt["rationale"] or "").strip(),
            "heading_path":      (gt["heading_path"] or "").strip(),
        }
    else:
        sample = rows[0] if rows else None
        gap_block = {
            "gap_id":            gap_id,
            "chapter":           (sample["gap_topic"] or "").strip() if sample else "",
            "claim_text":        "",
            "research_question": (sample["gap_research_question"] or "").strip() if sample else "",
            "evidence_target":   0,
            "tier":              None,
            "gap_type":          "",
            "status":            "",
            "detector_pass":     "",
            "rationale":         "",
            "heading_path":      "",
        }

    return {
        "gap":     gap_block,
        "summary": summary,
        "tiers":   tiers,
    }


# ---------------------------------------------------------------------------
# Iteration helper for tier ordering
# ---------------------------------------------------------------------------

# Canonical iteration order for tier buckets used by both the markdown
# generator and the API consumer. Tier 3 first (most cite-worthy) down to
# Tier 0, with "unscored" last.
TIER_ORDER: Tuple[str, ...] = ("3", "2", "1", "0", "unscored")
