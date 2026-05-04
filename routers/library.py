"""Library / writing-companion API router.

Endpoints under ``/api/library/`` expose the gap_tree and per-gap article
dossiers from the SQLite article index. The frontend ``/write`` route tree
consumes these to render the dossier browser.

The dossier shape is owned by ``layers.dossier_render.assemble_dossier``;
this router is a thin pagination + filtering layer that joins gap_tree
rows with article counts and forwards to that helper.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from layers.dossier_render import (
    TIER_ORDER,
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
