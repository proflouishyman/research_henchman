"""Library / writing-companion API router.

Endpoints under ``/api/library/`` expose the gap_tree and per-gap article
dossiers from the SQLite article index. The frontend ``/write`` route tree
consumes these to render the dossier browser.

The dossier shape is owned by ``layers.dossier_render.assemble_dossier``;
this router is a thin pagination + filtering layer that joins gap_tree
rows with article counts and forwards to that helper.

Wave 2 additions: corpus search via FTS5 (``GET /articles/search``) and a
main-characters dashboard for company-profile gaps (``GET /characters``).
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from layers.dossier_render import (
    TIER_ORDER,
    absolutize_url,
    assemble_dossier,
    build_cross_gap_index,
    chapter_slug,
)


router = APIRouter(prefix="/api/library", tags=["library"])


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

# The article index lives at ``<data_root>/article_index.sqlite``. We open
# a fresh connection per request — SQLite is cheap to open, and the
# library endpoints are read-only so connection lifetime doesn't matter
# for cache coherence.

_DB_FILENAME = "article_index.sqlite"


def _resolve_db_path() -> Path:
    """Return the path to the article-index SQLite DB.

    Mirrors ``main._settings()`` workspace logic without importing it
    (avoids a circular dependency through main.py during test discovery).
    Honors ``ORCH_DATA_ROOT`` then falls back to ``<repo>/data``.
    """
    import os
    repo_root = Path(__file__).resolve().parent.parent
    data_root = os.getenv("ORCH_DATA_ROOT") or str(repo_root / "data")
    return Path(data_root) / _DB_FILENAME


def _open_conn() -> sqlite3.Connection:
    """Open a read-only-ish connection to the article-index DB.

    Uses ``Row`` factory so consumers can index by column name. The
    actual write protection lives in the API layer (we just don't issue
    write SQL here).
    """
    db_path = _resolve_db_path()
    if not db_path.exists():
        raise HTTPException(status_code=503, detail=f"article index not found at {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _open_conn_rw() -> sqlite3.Connection:
    """Open a read-write connection to the article-index DB.

    Creates the DB file if it doesn't exist (needed for the first marks
    write before any articles are ingested in a fresh test environment).
    """
    db_path = _resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Helpers — gap_tree row + article counts
# ---------------------------------------------------------------------------

def _gap_tree_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Coerce a ``gap_tree`` row to a plain dict with stable string fields.

    NULL columns become empty strings (or ``None`` for ``tier``). The
    counts fields are added by ``_attach_counts``.
    """
    return {
        "gap_id":            str(row["gap_id"] or ""),
        "parent_gap_id":     row["parent_gap_id"],
        "depth":             int(row["depth"] or 0),
        "tier":              int(row["tier"]) if row["tier"] is not None else None,
        "gap_type":          str(row["gap_type"] or ""),
        "chapter":           str(row["chapter"] or ""),
        "heading_path":      str(row["heading_path"] or ""),
        "claim_text":        str(row["claim_text"] or ""),
        "research_question": str(row["research_question"] or ""),
        "source_locator":    str(row["source_locator"] or ""),
        "evidence_target":   int(row["evidence_target"] or 0),
        "detector_pass":     str(row["detector_pass"] or ""),
        "status":            str(row["status"] or ""),
        "rationale":         str(row["rationale"] or ""),
        "created_at":        str(row["created_at"] or ""),
        "total_rows":        0,
        "tier_counts":       {"3": 0, "2": 0, "1": 0, "0": 0, "unscored": 0},
    }


def _all_article_counts_by_gap(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """One-shot pull of (gap_id, score) → count.

    Returns ``{gap_id: {"total_rows": N, "tier_counts": {...}}}``. We do
    this in one pass over ``articles`` so that listing gaps doesn't issue
    an N+1 query.
    """
    out: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"total_rows": 0, "tier_counts": {"3": 0, "2": 0, "1": 0, "0": 0, "unscored": 0}}
    )
    rows = conn.execute(
        """SELECT gap_id, relevance_score, COUNT(*) AS n
             FROM articles
            GROUP BY gap_id, relevance_score"""
    ).fetchall()
    for r in rows:
        gap_id = str(r["gap_id"] or "")
        if not gap_id:
            continue
        n = int(r["n"] or 0)
        out[gap_id]["total_rows"] += n
        score = r["relevance_score"]
        bucket = "unscored" if score is None else str(int(score))
        if bucket not in out[gap_id]["tier_counts"]:
            bucket = "unscored"
        out[gap_id]["tier_counts"][bucket] += n
    return out


def _article_counts_for_gap(conn: sqlite3.Connection, gap_id: str) -> Dict[str, Any]:
    """Article counts for a single gap (used by the single-gap endpoints)."""
    out = {"total_rows": 0, "tier_counts": {"3": 0, "2": 0, "1": 0, "0": 0, "unscored": 0}}
    rows = conn.execute(
        """SELECT relevance_score, COUNT(*) AS n
             FROM articles
            WHERE gap_id = ?
            GROUP BY relevance_score""",
        (gap_id,),
    ).fetchall()
    for r in rows:
        n = int(r["n"] or 0)
        out["total_rows"] += n
        score = r["relevance_score"]
        bucket = "unscored" if score is None else str(int(score))
        if bucket not in out["tier_counts"]:
            bucket = "unscored"
        out["tier_counts"][bucket] += n
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/gaps")
def api_library_gaps(
    chapter: str = Query(default=""),
    gap_type: str = Query(default=""),  # CSV
    tier: Optional[int] = Query(default=None),
    status: str = Query(default=""),
    detector_pass: str = Query(default=""),
    parent_gap_id: str = Query(default=""),
) -> Dict[str, Any]:
    """List gap_tree rows with article counts attached.

    Filters are ANDed; empty filters are wildcards. ``gap_type`` accepts a
    comma-separated list (e.g. ``intro_promise,company_profile``).
    ``parent_gap_id`` accepts ``"<root>"`` to find top-level gaps.
    """
    conn = _open_conn()
    try:
        clauses: List[str] = []
        params: List[Any] = []
        if chapter.strip():
            clauses.append("chapter = ?")
            params.append(chapter.strip())
        gtypes = [g.strip() for g in gap_type.split(",") if g.strip()]
        if gtypes:
            clauses.append("gap_type IN (" + ",".join("?" for _ in gtypes) + ")")
            params.extend(gtypes)
        if tier is not None:
            clauses.append("tier = ?")
            params.append(int(tier))
        if status.strip():
            clauses.append("status = ?")
            params.append(status.strip())
        if detector_pass.strip():
            clauses.append("detector_pass = ?")
            params.append(detector_pass.strip())
        if parent_gap_id.strip():
            if parent_gap_id.strip() == "<root>":
                clauses.append("parent_gap_id IS NULL")
            else:
                clauses.append("parent_gap_id = ?")
                params.append(parent_gap_id.strip())

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            cursor = conn.execute(
                f"SELECT * FROM gap_tree {where} ORDER BY rowid ASC",
                params,
            )
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail=f"gap_tree table missing: {exc}") from exc

        rows = cursor.fetchall()
        counts = _all_article_counts_by_gap(conn)
        out: List[Dict[str, Any]] = []
        for r in rows:
            gap = _gap_tree_row_to_dict(r)
            c = counts.get(gap["gap_id"])
            if c:
                gap["total_rows"] = c["total_rows"]
                gap["tier_counts"] = c["tier_counts"]
            out.append(gap)

        return {"gaps": out}
    finally:
        conn.close()


@router.get("/gaps/{gap_id}")
def api_library_gap(gap_id: str) -> Dict[str, Any]:
    """Single gap_tree row + article counts."""
    conn = _open_conn()
    try:
        try:
            row = conn.execute(
                "SELECT * FROM gap_tree WHERE gap_id = ?",
                (gap_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail=f"gap_tree table missing: {exc}") from exc
        if not row:
            raise HTTPException(status_code=404, detail=f"gap not found: {gap_id}")
        gap = _gap_tree_row_to_dict(row)
        c = _article_counts_for_gap(conn, gap_id)
        gap["total_rows"] = c["total_rows"]
        gap["tier_counts"] = c["tier_counts"]
        return gap
    finally:
        conn.close()


@router.get("/gaps/{gap_id}/dossier")
def api_library_dossier(gap_id: str) -> Dict[str, Any]:
    """Build the structured per-gap dossier for the writing companion UI.

    Returns the shape documented in
    ``layers.dossier_render.assemble_dossier``. The cross_gap_refs on
    each entry are computed against the corpus-wide title index so the
    user can spot thematic overlap with neighbouring gaps.
    """
    conn = _open_conn()
    try:
        # Verify the gap exists in either gap_tree OR articles before
        # building (legacy AUTO-NN-G1 gaps live only in articles).
        in_gap_tree = False
        try:
            in_gap_tree = bool(conn.execute(
                "SELECT 1 FROM gap_tree WHERE gap_id = ? LIMIT 1",
                (gap_id,),
            ).fetchone())
        except sqlite3.OperationalError:
            pass
        if not in_gap_tree:
            in_articles = bool(conn.execute(
                "SELECT 1 FROM articles WHERE gap_id = ? LIMIT 1",
                (gap_id,),
            ).fetchone())
            if not in_articles:
                raise HTTPException(status_code=404, detail=f"gap not found: {gap_id}")

        cross_gap_idx = build_cross_gap_index(conn)
        return assemble_dossier(conn, gap_id, cross_gap_idx=cross_gap_idx)
    finally:
        conn.close()


@router.get("/index")
def api_library_index() -> Dict[str, Any]:
    """Chapter-grouped gap list + corpus stats for the sidebar / INDEX view.

    The grouping key is ``gap_tree.chapter``; gaps with empty chapter
    bucket under ``"(no chapter)"``. Within each chapter, gaps sort by
    tier ascending (high-tier first) then by gap_id.
    """
    conn = _open_conn()
    try:
        try:
            rows = conn.execute(
                "SELECT * FROM gap_tree ORDER BY rowid ASC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail=f"gap_tree table missing: {exc}") from exc

        counts = _all_article_counts_by_gap(conn)

        chapters_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            gap = _gap_tree_row_to_dict(r)
            c = counts.get(gap["gap_id"])
            if c:
                gap["total_rows"] = c["total_rows"]
                gap["tier_counts"] = c["tier_counts"]
            chapter = gap["chapter"] or "(no chapter)"
            chapters_map[chapter].append(gap)

        # Sort gaps inside each chapter: higher tier (lower number) first,
        # then by gap_id alphabetic for stability.
        def _gap_sort_key(g: Dict[str, Any]) -> tuple:
            tier = g.get("tier")
            tier_key = tier if tier is not None else 99
            return (int(tier_key), g.get("gap_id", ""))

        chapters_out: List[Dict[str, Any]] = []
        for chapter_title, gaps in chapters_map.items():
            gaps.sort(key=_gap_sort_key)
            chapters_out.append({
                "slug":      chapter_slug(chapter_title),
                "title":     chapter_title,
                "gap_count": len(gaps),
                "gaps":      gaps,
            })
        chapters_out.sort(key=lambda c: c["title"].lower())

        # Corpus stats
        corpus_total = int(conn.execute(
            "SELECT COUNT(*) FROM articles"
        ).fetchone()[0])
        corpus_scored = int(conn.execute(
            "SELECT COUNT(*) FROM articles WHERE relevance_score IS NOT NULL"
        ).fetchone()[0])
        sources = sorted(
            r[0] for r in conn.execute(
                "SELECT DISTINCT source_id FROM articles WHERE source_id IS NOT NULL"
            ).fetchall()
        )

        return {
            "chapters":           chapters_out,
            "corpus_total_rows":  corpus_total,
            "corpus_scored_rows": corpus_scored,
            "sources":            sources,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Wave 2 — Corpus search via FTS5
# ---------------------------------------------------------------------------

# Characters reserved by the FTS5 query language. We escape them by
# wrapping the entire user query in double quotes (FTS5 phrase mode) and
# doubling any embedded double quotes — this disables operator parsing
# while still allowing multi-word phrase matches. Per FTS5 docs, the
# "" sequence inside a quoted string represents a literal double quote.
_FTS5_RESERVED_CHARS = set('"*+-^():')


def _sanitize_fts_query(raw: str) -> str:
    """Escape user input for safe FTS5 ``MATCH`` evaluation.

    Strategy: tokenize on whitespace, drop any token that becomes empty
    after stripping reserved chars, and re-emit each token as a quoted
    phrase. This keeps FTS5 operator semantics out of user input while
    still preserving multi-word AND-of-tokens search behaviour (FTS5
    treats space-separated tokens as implicit AND).
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    out: List[str] = []
    for tok in raw.split():
        # Strip reserved chars rather than escaping — most users typing
        # them in a search box mean them as punctuation, not operators.
        cleaned = "".join(c for c in tok if c not in _FTS5_RESERVED_CHARS)
        if not cleaned:
            continue
        # Wrap in double quotes; doubling any embedded " just in case
        # (cleaned shouldn't contain ", but defense-in-depth).
        cleaned = cleaned.replace('"', '""')
        out.append(f'"{cleaned}"')
    return " ".join(out)


_YEAR_RE = re.compile(r"\b(\d{4})\b")


def _extract_year(pub_date: Optional[str]) -> Optional[int]:
    """Return the first 4-digit year found in a freeform pub_date, or None."""
    if not pub_date:
        return None
    m = _YEAR_RE.search(str(pub_date))
    return int(m.group(1)) if m else None


@router.get("/articles/search")
def api_library_search(
    q: str = Query(..., min_length=1, description="FTS5 search query"),
    source_id: str = Query(default="", description="CSV source filter"),
    score_min: int = Query(default=0, ge=0, le=3),
    gap_id: str = Query(default=""),
    year_from: Optional[int] = Query(default=None),
    year_to: Optional[int] = Query(default=None),
    has_pdf: Optional[bool] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    """Full-text search the corpus via SQLite FTS5.

    Returns ``{total, results: [DossierEntry & {snippet}]}`` where
    ``snippet`` is a 200-char excerpt with ``<mark>`` tags around hits
    (FTS5 ``snippet()``). Empty / sanitized-to-empty queries 400.
    """
    sanitized = _sanitize_fts_query(q)
    if not sanitized:
        raise HTTPException(status_code=400, detail="empty search query after sanitization")

    conn = _open_conn()
    try:
        clauses: List[str] = ["articles_fts MATCH ?"]
        params: List[Any] = [sanitized]

        sids = [s.strip() for s in source_id.split(",") if s.strip()]
        if sids:
            clauses.append("a.source_id IN (" + ",".join("?" for _ in sids) + ")")
            params.extend(sids)
        if score_min > 0:
            # ``relevance_score >= ?`` only matches scored rows (NULLs
            # excluded automatically by SQL comparison semantics).
            clauses.append("a.relevance_score >= ?")
            params.append(int(score_min))
        if gap_id.strip():
            clauses.append("a.gap_id = ?")
            params.append(gap_id.strip())
        if has_pdf is True:
            clauses.append("a.pdf_path IS NOT NULL AND a.pdf_path != ''")
        elif has_pdf is False:
            clauses.append("(a.pdf_path IS NULL OR a.pdf_path = '')")

        # Year filters use a regex extract on pub_date. SQLite has no
        # native regex, but we can approximate with substr/glob; the
        # cheapest correct path is a Python post-filter, since the FTS
        # MATCH already narrows the candidate pool sharply. We push the
        # MATCH + score/source/has_pdf filters into SQL and do year/CGR
        # post-filtering in Python.
        where = " AND ".join(clauses)

        # Count + page in two steps so we can return total alongside
        # the page of results. ``bm25(articles_fts)`` is ascending —
        # smaller is better.
        sql_total = (
            "SELECT COUNT(*) FROM articles a "
            "JOIN articles_fts ON articles_fts.rowid = a.id "
            f"WHERE {where}"
        )
        try:
            pre_year_total = int(conn.execute(sql_total, params).fetchone()[0])
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail=f"FTS unavailable: {exc}") from exc

        # If year filters are active, we have to scan the candidate set
        # to count post-filter; cap to a sane upper bound to avoid
        # pathological queries.
        year_active = year_from is not None or year_to is not None

        # Snippet column index: articles_fts columns are
        # (title, authors, abstract, journal, gap_research_question)
        # — column 2 (abstract) gives the most useful excerpt context.
        # The trailing 4 args of snippet() are: open_tag, close_tag,
        # ellipsis, max_tokens.
        sql_page = (
            "SELECT a.id, a.title, a.authors, a.pub_date, a.journal, "
            "       a.abstract, a.doi, a.url, a.pdf_path, a.source_id, "
            "       a.gap_id, a.relevance_score, a.relevance_why, "
            "       snippet(articles_fts, 2, '<mark>', '</mark>', '…', 32) AS snip "
            "  FROM articles a "
            "  JOIN articles_fts ON articles_fts.rowid = a.id "
            f" WHERE {where} "
            " ORDER BY bm25(articles_fts) ASC, a.id ASC "
            # When year filtering is active, fetch a wider window so we
            # have room to skip non-matching rows; otherwise honour limit
            # exactly with offset.
            f" LIMIT {limit + (10000 if year_active else 0)} OFFSET {0 if year_active else offset}"
        )
        rows = conn.execute(sql_page, params).fetchall()

        # Apply year post-filter + pagination if active.
        if year_active:
            filtered: List[sqlite3.Row] = []
            for r in rows:
                yr = _extract_year(r["pub_date"])
                if year_from is not None and (yr is None or yr < year_from):
                    continue
                if year_to is not None and (yr is None or yr > year_to):
                    continue
                filtered.append(r)
            total = len(filtered)
            page = filtered[offset : offset + limit]
        else:
            total = pre_year_total
            page = rows

        # Build cross-gap index once for the page (cheap — already
        # cached by SQLite for read-mostly workloads).
        cross_gap_idx = build_cross_gap_index(conn)
        from layers.dossier_render import norm_title

        results: List[Dict[str, Any]] = []
        for r in page:
            primary_src = str(r["source_id"] or "")
            n = norm_title(r["title"])
            other_gaps = [
                g for g in cross_gap_idx.get(n, [])
                if g != (r["gap_id"] or "")
            ] if n else []
            results.append({
                "id":              int(r["id"]),
                "title":           (r["title"] or "").strip(),
                "authors":         (r["authors"] or "").strip(),
                "pub_date":        (r["pub_date"] or "").strip(),
                "journal":         (r["journal"] or "").strip(),
                "abstract":        (r["abstract"] or "").strip(),
                "doi":             (r["doi"] or "").strip(),
                "url":             absolutize_url(r["url"], primary_src),
                "pdf_path":        (r["pdf_path"] or "").strip(),
                "source_id":       primary_src,
                "also_in_sources": [],  # search rows are individual; merge happens in dossier
                "relevance_score": int(r["relevance_score"]) if r["relevance_score"] is not None else None,
                "relevance_why":   (r["relevance_why"] or "").strip(),
                "cross_gap_refs":  other_gaps,
                "gap_id":          str(r["gap_id"] or ""),
                "snippet":         (r["snip"] or "").strip(),
            })

        return {"total": total, "results": results}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Wave 2 — Main characters (company_profile gaps) dashboard
# ---------------------------------------------------------------------------

@router.get("/characters")
def api_library_characters() -> Dict[str, Any]:
    """List company-profile gaps with histogram + top tier-3 titles.

    Returns ``{characters: [GapTreeRow & {top_tier3_titles, tier_histogram}]}``.
    Sorted by tier-3 count desc, then evidence_target desc, then gap_id.
    """
    conn = _open_conn()
    try:
        try:
            rows = conn.execute(
                """SELECT * FROM gap_tree
                    WHERE gap_type = 'company_profile'
                    ORDER BY rowid ASC"""
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail=f"gap_tree table missing: {exc}") from exc

        counts = _all_article_counts_by_gap(conn)
        out: List[Dict[str, Any]] = []
        for r in rows:
            gap = _gap_tree_row_to_dict(r)
            c = counts.get(gap["gap_id"])
            if c:
                gap["total_rows"] = c["total_rows"]
                gap["tier_counts"] = c["tier_counts"]

            # Top 3 tier-3 titles for the card preview. Order by source
            # priority (EBSCO first) then pub_date desc — same heuristic
            # the dossier uses, so the card preview matches what the
            # user sees inside the dossier.
            t3 = conn.execute(
                """SELECT title, source_id, pub_date
                     FROM articles
                    WHERE gap_id = ? AND relevance_score = 3
                    ORDER BY source_id ASC, pub_date DESC
                    LIMIT 3""",
                (gap["gap_id"],),
            ).fetchall()
            gap["top_tier3_titles"] = [
                (row["title"] or "").strip() for row in t3 if (row["title"] or "").strip()
            ]
            # ``tier_histogram`` is a friendly alias of tier_counts for
            # the frontend's chart component (matches the spec).
            gap["tier_histogram"] = dict(gap["tier_counts"])
            out.append(gap)

        # Sort: tier-3 count desc, then evidence_target desc, then gap_id.
        def _sort_key(g: Dict[str, Any]) -> tuple:
            t3 = int(g.get("tier_counts", {}).get("3", 0))
            return (-t3, -int(g.get("evidence_target", 0)), g.get("gap_id", ""))

        out.sort(key=_sort_key)
        return {"characters": out}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v3 — Manuscript reader endpoints
# ---------------------------------------------------------------------------

# Default manuscript path shipped with the project.
_DEFAULT_DOCX = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "manuscript_exports"
    / "manuscript"
    / "manuscript.docx"
)


def _resolve_docx(docx_param: Optional[str]) -> Path:
    """Resolve the docx path query param (or use the project default)."""
    if docx_param and docx_param.strip():
        p = Path(docx_param.strip())
        if not p.is_absolute():
            repo_root = Path(__file__).resolve().parent.parent
            p = repo_root / p
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"docx not found: {p}")
        return p
    if not _DEFAULT_DOCX.exists():
        raise HTTPException(status_code=404, detail="default manuscript not found")
    return _DEFAULT_DOCX


@router.get("/manuscript/structure")
def api_manuscript_structure(
    docx: Optional[str] = Query(default=None, description="Absolute path to .docx file"),
) -> Dict[str, Any]:
    """Return the cached manuscript structure grouped into chapters/sections/paragraphs.

    Each paragraph carries: para_id, text, is_heading, heading_level,
    footnote_count, bracketed_todos, gap_ids (gap_ids linked to this paragraph
    from gap_tree via heading/claim heuristics).

    Default docx is the project manuscript when ``docx`` param is omitted.
    """
    from layers.manuscript_parse import parse_manuscript, paragraph_gap_links, group_into_chapters

    docx_path = _resolve_docx(docx)
    paragraphs = parse_manuscript(docx_path)

    conn = _open_conn()
    try:
        gap_links = paragraph_gap_links(paragraphs, conn)
    finally:
        conn.close()

    chapters = group_into_chapters(paragraphs, gap_links)
    return {"chapters": chapters}


@router.get("/manuscript/paragraph/{para_id}")
def api_manuscript_paragraph(
    para_id: str,
    docx: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """Return a single paragraph with resolved gap_tree rows.

    Includes full gap_links and the raw gap_tree dicts for each linked gap.
    """
    from layers.manuscript_parse import parse_manuscript, paragraph_gap_links

    docx_path = _resolve_docx(docx)
    paragraphs = parse_manuscript(docx_path)

    # Find the requested paragraph.
    target = next((p for p in paragraphs if p["para_id"] == para_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"paragraph not found: {para_id}")

    conn = _open_conn()
    try:
        gap_links = paragraph_gap_links([target], conn)
        gap_ids = gap_links.get(para_id, [])

        # Resolve gap_tree rows.
        gap_rows: List[Dict[str, Any]] = []
        if gap_ids:
            try:
                placeholders = ",".join("?" for _ in gap_ids)
                rows = conn.execute(
                    f"SELECT * FROM gap_tree WHERE gap_id IN ({placeholders})",
                    gap_ids,
                ).fetchall()
                for r in rows:
                    gap_rows.append(_gap_tree_row_to_dict(r))
            except Exception:
                pass

        return {
            "para_id":         target["para_id"],
            "chapter":         target["chapter"],
            "heading_path":    target["heading_path"],
            "text":            target["text"],
            "is_heading":      target["is_heading"],
            "heading_level":   target["heading_level"],
            "footnote_count":  target["footnote_count"],
            "bracketed_todos": target["bracketed_todos"],
            "char_offset":     target["char_offset"],
            "gap_ids":         gap_ids,
            "gap_rows":        gap_rows,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v3 — User marks (star + read) endpoints
# ---------------------------------------------------------------------------

from pydantic import BaseModel as _BaseModel  # noqa: E402 (needed after router defs)


class _MarkUpsertInput(_BaseModel):
    article_id: int
    starred: Optional[bool] = None
    read: Optional[bool] = None
    note: Optional[str] = None


@router.post("/marks")
def api_marks_upsert(body: _MarkUpsertInput) -> Dict[str, Any]:
    """Upsert a star/read/note mark for an article.

    Only provided fields are updated; existing values for omitted fields are
    preserved. Returns the resulting mark row.
    """
    from adapters.article_index import set_mark, get_marks, ensure_marks_schema

    conn = _open_conn_rw()
    try:
        ensure_marks_schema(conn)
        set_mark(
            conn,
            body.article_id,
            starred=body.starred,
            read=body.read,
            note=body.note,
        )
        marks = get_marks(conn, [body.article_id])
        if body.article_id in marks:
            m = marks[body.article_id]
            return {"article_id": body.article_id, **m}
        # Row was deleted (all false + no note).
        return {"article_id": body.article_id, "starred": False, "read": False, "note": "", "updated_at": ""}
    finally:
        conn.close()


@router.get("/marks")
def api_marks_list(
    starred: Optional[bool] = Query(default=None),
    read: Optional[bool] = Query(default=None),
) -> Dict[str, Any]:
    """Bulk fetch marks.

    With no filters: returns all marks.
    With ``starred=true``: only starred rows.
    With ``read=true``: only read rows.
    Filters are ANDed.
    """
    from adapters.article_index import ensure_marks_schema

    conn = _open_conn_rw()
    try:
        ensure_marks_schema(conn)
        clauses: List[str] = []
        params: List[Any] = []
        if starred is not None:
            clauses.append("starred = ?")
            params.append(int(starred))
        if read is not None:
            clauses.append("read = ?")
            params.append(int(read))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT article_id, starred, read, note, updated_at FROM user_marks {where} ORDER BY updated_at DESC",
            params,
        ).fetchall()
        marks = [
            {
                "article_id": int(r["article_id"]),
                "starred":    bool(r["starred"]),
                "read":       bool(r["read"]),
                "note":       r["note"] or "",
                "updated_at": r["updated_at"] or "",
            }
            for r in rows
        ]
        return {"marks": marks}
    finally:
        conn.close()


class _ResolveGapsInput(_BaseModel):
    article_ids: List[int] = []


@router.post("/articles/resolve_gaps")
def api_resolve_gaps(body: _ResolveGapsInput) -> Dict[str, Any]:
    """Return the gap_ids associated with each article_id.

    Uses the articles.gap_id column (each article belongs to exactly one
    primary gap at ingest time). Returns {article_id (str): [gap_id]}.
    """
    if not body.article_ids:
        return {"mapping": {}}

    conn = _open_conn()
    try:
        placeholders = ",".join("?" for _ in body.article_ids)
        rows = conn.execute(
            f"SELECT id, gap_id FROM articles WHERE id IN ({placeholders})",
            body.article_ids,
        ).fetchall()
        mapping: Dict[str, List[str]] = {}
        for r in rows:
            aid = str(int(r["id"]))
            gid = str(r["gap_id"] or "")
            if gid:
                mapping.setdefault(aid, [])
                if gid not in mapping[aid]:
                    mapping[aid].append(gid)
        return {"mapping": mapping}
    finally:
        conn.close()
