"""Gap tree: a hierarchical, multi-pass detector schema for manuscript gaps.

This is a *separate* table from ``articles`` and from the legacy
``layers/analysis.py`` ``AUTO-NN-GN`` heuristic detector. It models gaps as
nodes in a tree (top-level claim → sub-claims → sub-sub-claims) so that the
multi-pass detector waves (Pass A intro-promise, Pass B explicit TODO,
Pass F company-profile, Pass C/D/E in later waves) can each contribute
their own subtrees without clobbering each other.

ID conventions (v1):
  - Pass A intro-promise top-level nodes: ``IP1``, ``IP2``, …
  - Pass B explicit-TODO top-level nodes: ``TODO1``, ``TODO2``, …
  - Pass F company-profile top-level nodes: ``CP1``, ``CP2``, …
  - Children added in v3 will use letter suffixes (``IP1.A``, ``IP1.A.1``).
  - The ``AUTO-NN-GN`` prefix is reserved for the legacy detector and must
    never be reused here.

Schema notes:
  - Same SQLite database as ``articles`` (default ``data/article_index.sqlite``).
  - ``parent_gap_id`` is a self-FK; top-level rows have NULL parent.
  - ``status`` defaults to ``pending`` — the manual-review CLI flips it to
    ``approved`` / ``rejected`` based on the reviewed markdown file.
  - ``detector_pass`` is the wave letter (``A``, ``B``, …) for diagnostic
    queries like ``count_by_pass``.

Public API:
  - ``ensure_gap_tree_schema(conn)`` — idempotent DDL.
  - ``insert_node(conn, **fields)`` — typed insert helper.
  - ``list_nodes(conn, *, tier=None, gap_type=None, status=None,
                  parent_gap_id=None)`` — query helper.
  - ``count_by_pass(conn)`` — diagnostic.

This module is read/write — it owns the ``gap_tree`` table. It does not
modify ``articles`` or any legacy artifacts.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS gap_tree (
    gap_id              TEXT PRIMARY KEY,
    parent_gap_id       TEXT REFERENCES gap_tree(gap_id),
    depth               INTEGER NOT NULL,
    tier                INTEGER NOT NULL,
    gap_type            TEXT NOT NULL,
    chapter             TEXT,
    heading_path        TEXT,
    claim_text          TEXT,
    research_question   TEXT,
    source_locator      TEXT,
    evidence_target     INTEGER NOT NULL,
    detector_pass       TEXT,
    status              TEXT DEFAULT 'pending',
    rationale           TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gt_parent ON gap_tree(parent_gap_id);
CREATE INDEX IF NOT EXISTS idx_gt_tier   ON gap_tree(tier);
CREATE INDEX IF NOT EXISTS idx_gt_status ON gap_tree(status);
CREATE INDEX IF NOT EXISTS idx_gt_pass   ON gap_tree(detector_pass);
"""


def ensure_gap_tree_schema(conn: sqlite3.Connection) -> None:
    """Create the ``gap_tree`` table and its indexes if they don't exist.

    Idempotent — safe to call on an existing DB. All DDL uses
    ``CREATE … IF NOT EXISTS``.
    """
    conn.executescript(_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# Insert / query helpers
# ---------------------------------------------------------------------------

def insert_node(
    conn: sqlite3.Connection,
    *,
    gap_id: str,
    tier: int,
    gap_type: str,
    evidence_target: int,
    parent_gap_id: Optional[str] = None,
    depth: Optional[int] = None,
    chapter: Optional[str] = None,
    heading_path: Optional[str] = None,
    claim_text: Optional[str] = None,
    research_question: Optional[str] = None,
    source_locator: Optional[str] = None,
    detector_pass: Optional[str] = None,
    status: str = "pending",
    rationale: Optional[str] = None,
) -> bool:
    """Insert a single gap node. Returns True on insert, False if PK collides.

    *depth* is auto-computed from *parent_gap_id* when not supplied: a node
    with no parent has depth 0; otherwise depth = parent.depth + 1. If the
    parent is missing the call falls back to depth=0 to keep things permissive
    (callers should normally insert parents first, but tree imports may be
    bottom-up in some repair scenarios).
    """
    if depth is None:
        if parent_gap_id is None:
            depth = 0
        else:
            row = conn.execute(
                "SELECT depth FROM gap_tree WHERE gap_id = ?",
                (parent_gap_id,),
            ).fetchone()
            depth = (row[0] + 1) if row else 0

    try:
        conn.execute(
            """
            INSERT INTO gap_tree (
                gap_id, parent_gap_id, depth, tier, gap_type,
                chapter, heading_path, claim_text, research_question,
                source_locator, evidence_target, detector_pass,
                status, rationale
            ) VALUES (
                :gap_id, :parent_gap_id, :depth, :tier, :gap_type,
                :chapter, :heading_path, :claim_text, :research_question,
                :source_locator, :evidence_target, :detector_pass,
                :status, :rationale
            )
            """,
            {
                "gap_id": gap_id,
                "parent_gap_id": parent_gap_id,
                "depth": int(depth),
                "tier": int(tier),
                "gap_type": gap_type,
                "chapter": chapter,
                "heading_path": heading_path,
                "claim_text": claim_text,
                "research_question": research_question,
                "source_locator": source_locator,
                "evidence_target": int(evidence_target),
                "detector_pass": detector_pass,
                "status": status,
                "rationale": rationale,
            },
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Duplicate primary key — caller decides whether to update or skip.
        return False


def list_nodes(
    conn: sqlite3.Connection,
    *,
    tier: Optional[int] = None,
    gap_type: Optional[str] = None,
    status: Optional[str] = None,
    parent_gap_id: Optional[str] = None,
    detector_pass: Optional[str] = None,
) -> List[sqlite3.Row]:
    """Return gap_tree rows filtered by any combination of fields.

    All filters are ANDed; missing filters are wildcards. Parent filter
    accepts the sentinel value ``"<root>"`` to find top-level rows
    (parent_gap_id IS NULL). Returns rows in insertion order.
    """
    clauses: List[str] = []
    params: List[Any] = []
    if tier is not None:
        clauses.append("tier = ?")
        params.append(int(tier))
    if gap_type is not None:
        clauses.append("gap_type = ?")
        params.append(gap_type)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if detector_pass is not None:
        clauses.append("detector_pass = ?")
        params.append(detector_pass)
    if parent_gap_id is not None:
        if parent_gap_id == "<root>":
            clauses.append("parent_gap_id IS NULL")
        else:
            clauses.append("parent_gap_id = ?")
            params.append(parent_gap_id)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM gap_tree {where} ORDER BY rowid ASC"
    cursor = conn.execute(sql, params)
    return cursor.fetchall()


def count_by_pass(conn: sqlite3.Connection) -> Dict[str, int]:
    """Return ``{detector_pass: count}`` over all rows in the table.

    NULL detector_pass values are bucketed under ``"unknown"``.
    """
    rows = conn.execute(
        "SELECT COALESCE(detector_pass, 'unknown') AS p, COUNT(*) AS n "
        "FROM gap_tree GROUP BY p"
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


# ---------------------------------------------------------------------------
# Existence check (used by detector resume logic)
# ---------------------------------------------------------------------------

def gap_exists(conn: sqlite3.Connection, gap_id: str) -> bool:
    """Return True if a row with this gap_id is already in the table."""
    row = conn.execute(
        "SELECT 1 FROM gap_tree WHERE gap_id = ? LIMIT 1",
        (gap_id,),
    ).fetchone()
    return row is not None


def fetch_research_question(
    conn: sqlite3.Connection,
    gap_id: str,
) -> Optional[str]:
    """Return the research_question for a gap (or None if missing/empty)."""
    row = conn.execute(
        "SELECT research_question FROM gap_tree WHERE gap_id = ?",
        (gap_id,),
    ).fetchone()
    if not row:
        return None
    rq = row[0]
    return rq if (rq and str(rq).strip()) else None


def update_research_question(
    conn: sqlite3.Connection,
    gap_id: str,
    research_question: str,
) -> None:
    """Set the research_question on an existing row. No-op if row missing."""
    conn.execute(
        "UPDATE gap_tree SET research_question = ? WHERE gap_id = ?",
        (research_question, gap_id),
    )
    conn.commit()


def fetch_gap_type(conn: sqlite3.Connection, gap_id: str) -> Optional[str]:
    """Return the gap_type for a given gap_id, or None if missing."""
    row = conn.execute(
        "SELECT gap_type FROM gap_tree WHERE gap_id = ?",
        (gap_id,),
    ).fetchone()
    if not row:
        return None
    return row[0]


def update_gap_classification(
    conn: sqlite3.Connection,
    gap_id: str,
    *,
    gap_type: str,
    tier: int,
    status: Optional[str] = None,
    rationale: Optional[str] = None,
) -> None:
    """Reclassify an existing row's gap_type/tier (and optionally status/rationale).

    Used by Pass B's editorial-note classifier to demote bracketed
    ``editorial_todo`` items out of the ``research_gap`` lane after the
    initial regex pass has already inserted them. No-op if row missing.
    """
    sets = ["gap_type = ?", "tier = ?"]
    params: List[Any] = [gap_type, int(tier)]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if rationale is not None:
        sets.append("rationale = ?")
        params.append(rationale)
    params.append(gap_id)
    conn.execute(
        f"UPDATE gap_tree SET {', '.join(sets)} WHERE gap_id = ?",
        params,
    )
    conn.commit()
