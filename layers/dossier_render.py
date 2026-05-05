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
    # Phase 4: Internet Archive — lower priority than HathiTrust since IA
    # records lack abstracts but higher than unknown sources.
    "internet_archive":          6,
    "internet_archive_ia_html":  6,
}

_SOURCE_LABELS: Dict[str, str] = {
    "ebsco_api":                          "EBSCO",
    "proquest_us_newsstream":             "ProQuest US News",
    "proquest_international_newsstream":  "ProQuest Intl News",
    "proquest_historical_newspapers":     "ProQuest Historical",
    "hathitrust_fulltext":                "HathiTrust",
    "sec_edgar":                          "SEC EDGAR",
    # Phase 4
    "internet_archive":                   "Internet Archive",
    "internet_archive_ia_html":           "Internet Archive",
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
# Phase 2: Cross-gap candidates (AUTO-* PDFs for new CP/IP/TODO gaps)
# ---------------------------------------------------------------------------

def find_cross_gap_candidates(
    conn: sqlite3.Connection,
    gap_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Find AUTO-* gap articles relevant to *gap_id* via FTS5 search.

    Queries the FTS index using the requesting gap's claim_text / research
    question as the search terms, restricted to AUTO-* gaps with PDF and
    relevance_score >= 1. Returns up to *limit* candidates (capped at 10 in
    the caller for dossier rendering) annotated with ``from_gap_id``.

    Falls back to an empty list if FTS is unavailable, the gap has no claim
    text, or there are no AUTO-* results.
    """
    # Fetch the claim text for this gap to build the FTS query.
    claim = ""
    try:
        gt = conn.execute(
            "SELECT claim_text, research_question FROM gap_tree WHERE gap_id = ?",
            (gap_id,),
        ).fetchone()
        if gt:
            claim = (gt["claim_text"] or gt["research_question"] or "").strip()
    except sqlite3.OperationalError:
        return []

    if not claim:
        return []

    # Build a safe FTS5 OR query from the claim tokens.
    # We use OR rather than AND (implicit with space in FTS5) so that any
    # matching token contributes — requiring ALL claim words would exclude
    # most results since claim and article vocabularies rarely overlap
    # fully. Short stopwords (≤ 3 chars) are dropped to improve precision.
    _STOPWORDS = {"the", "and", "for", "was", "are", "its", "has", "had",
                  "but", "not", "all", "by", "in", "of", "to", "a", "an"}

    def _to_fts(text: str) -> str:
        tokens = []
        for tok in text.split():
            cleaned = "".join(c for c in tok if c not in '"*+-^():')
            cleaned = cleaned.replace('"', '""')
            # Skip very short tokens and common stopwords.
            if len(cleaned) <= 3 or cleaned.lower() in _STOPWORDS:
                continue
            tokens.append(f'"{cleaned}"')
        unique_tokens = list(dict.fromkeys(tokens))[:12]
        return " OR ".join(unique_tokens)

    fts_query = _to_fts(claim)
    if not fts_query:
        return []

    try:
        rows = conn.execute(
            """SELECT a.id, a.title, a.authors, a.journal, a.pub_date,
                      a.abstract, a.doi, a.url, a.pdf_path, a.source_id,
                      a.gap_id AS from_gap_id, a.relevance_score, a.relevance_why,
                      a.access, a.hathi_id, a.subject, a.language
                 FROM articles a
                 JOIN articles_fts ON articles_fts.rowid = a.id
                WHERE articles_fts MATCH ?
                  AND a.gap_id LIKE 'AUTO-%'
                  AND a.pdf_path IS NOT NULL AND a.pdf_path != ''
                  AND (a.relevance_score IS NULL OR a.relevance_score >= 1)
                ORDER BY
                    a.relevance_score DESC,
                    CASE WHEN a.pdf_path IS NOT NULL THEN 0 ELSE 1 END ASC,
                    bm25(articles_fts) ASC
                LIMIT ?""",
            (fts_query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        from_gap = str(r["from_gap_id"] or "")
        out.append({
            "id":              int(r["id"]),
            "title":           (r["title"] or "").strip(),
            "authors":         (r["authors"] or "").strip(),
            "pub_date":        (r["pub_date"] or "").strip(),
            "journal":         (r["journal"] or "").strip(),
            "abstract":        (r["abstract"] or "").strip(),
            "doi":             (r["doi"] or "").strip(),
            "url":             absolutize_url(r["url"], str(r["source_id"] or "")),
            "pdf_path":        (r["pdf_path"] or "").strip(),
            "source_id":       str(r["source_id"] or ""),
            "also_in_sources": [],
            "relevance_score": int(r["relevance_score"]) if r["relevance_score"] is not None else None,
            "relevance_why":   (r["relevance_why"] or "").strip(),
            "cross_gap_refs":  [],
            "from_gap_id":     from_gap,
            # Phase 1 availability fields
            "access":    (r["access"] or "").strip() or None,
            "hathi_id":  (r["hathi_id"] or "").strip() or None,
            "subject":   (r["subject"] or "").strip() or None,
            "language":  (r["language"] or "").strip() or None,
        })
    return out


# ---------------------------------------------------------------------------
# Gap row fetcher + structured dossier builder
# ---------------------------------------------------------------------------

def _articles_columns(conn: sqlite3.Connection) -> set:
    """Return the set of column names currently in the articles table."""
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}
    except sqlite3.OperationalError:
        return set()


def fetch_gap_rows(conn: sqlite3.Connection, gap_id: str) -> List[sqlite3.Row]:
    """Return all article rows for a gap, with the columns the dossier needs.

    Phase 1: includes access, hathi_id, subject, language for availability
    badge when those columns exist (they are absent in hand-crafted test DBs
    that predate the migration). Falls back gracefully to the base column set.
    """
    cols = _articles_columns(conn)
    # Phase 1 columns — only select them if present in the DB.
    phase1_extras = ", ".join(
        c for c in ("access", "hathi_id", "subject", "language") if c in cols
    )
    # Scoring columns also added via migration by score_relevance.py.
    score_cols = ", ".join(
        c for c in ("relevance_score", "relevance_why") if c in cols
    )
    extra_sel = ""
    if score_cols:
        extra_sel += f", {score_cols}"
    if phase1_extras:
        extra_sel += f", {phase1_extras}"

    return conn.execute(
        f"""SELECT id, title, authors, journal, pub_date, abstract, doi,
                  url, pdf_path, source_id, gap_id, gap_topic,
                  gap_research_question{extra_sel}
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

    # Phase 1: include availability / source-specific metadata fields.
    # These are present on HathiTrust and Internet Archive rows; NULL for others.
    def _col(name: str) -> Optional[str]:
        """Safely fetch a column that may not exist in older row objects."""
        try:
            val = p[name]
            return (val or "").strip() or None
        except (IndexError, sqlite3.OperationalError):
            return None

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
        # Phase 1 availability fields (optional — None when not applicable).
        "access":    _col("access"),
        "hathi_id":  _col("hathi_id"),
        "subject":   _col("subject"),
        "language":  _col("language"),
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

    # Per-tier buckets keyed by string ("0"-"3", "unscored", "related") so the
    # JSON response is dict-friendly. "related" is the Phase 2 cross-link bucket.
    tiers: Dict[str, List[Dict[str, Any]]] = {
        "3": [], "2": [], "1": [], "0": [], "unscored": [], "related": [],
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

    # Phase 2: populate the "related" bucket with AUTO-* cross-linked PDFs.
    # Only run for new-style gaps (CP/IP/TODO*) that might lack EBSCO content.
    # Cap at 10 entries so the UI doesn't overflow.
    _is_new_gap = any(gap_id.startswith(pfx) for pfx in ("CP", "IP", "TODO", "RG"))
    if _is_new_gap:
        cross_candidates = find_cross_gap_candidates(conn, gap_id, limit=20)
        tiers["related"] = cross_candidates[:10]

    # Order each tier: source priority then pub-date desc (matches markdown).
    def _sort_key(e: Dict[str, Any]) -> tuple:
        src_rank = SOURCE_PRIORITY.get(e.get("source_id", ""), 99)
        date = (e.get("pub_date") or "").strip()
        m = re.search(r"\d{4}", date)
        year = -int(m.group()) if m else 0
        return (src_rank, year)
    for bucket in tiers:
        if bucket != "related":  # related is pre-sorted by FTS rank
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
# Tier 0, with "unscored" last. "related" is the Phase 2 cross-link bucket.
TIER_ORDER: Tuple[str, ...] = ("3", "2", "1", "0", "unscored", "related")
