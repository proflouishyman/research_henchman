"""Article index: SQLite + FTS5 searchable database of fetched article metadata.

Provides a read-only, additive index over ``data/pull_outputs/<run_id>/`` directories.
Does not write to or modify any pull_output files — purely additive.

Key functions:
  open_index(db_path)            — open/create the SQLite DB (creates schema if new)
  ingest_pull_output(conn, ...)  — walk a run dir and insert article rows
  dedupe_by_doi(conn, ...)       — mark duplicate DOI rows via canonical_id FK
  gap_context_for(run_id, gap_id, runs_json_path)
                                 — return (claim_text, chapter) for a gap from runs.json

Schema notes:
  - UNIQUE(run_id, gap_id, source_id, title) prevents duplicate inserts (idempotent).
  - DOI dedup sets canonical_id on secondary rows; no rows are deleted.
  - FTS5 virtual table is kept in sync via INSERT/DELETE triggers.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS articles (
    id                    INTEGER PRIMARY KEY,
    doi                   TEXT,
    canonical_id          INTEGER,           -- FK → articles.id; set for duplicate rows
    title                 TEXT NOT NULL,
    authors               TEXT,
    journal               TEXT,
    pub_date              TEXT,
    abstract              TEXT,
    url                   TEXT,
    pdf_path              TEXT,              -- relative to repo root; NULL if no PDF
    md_path               TEXT,             -- relative to repo root
    -- Provenance
    run_id                TEXT NOT NULL,
    gap_id                TEXT NOT NULL,
    source_id             TEXT NOT NULL,
    database_name         TEXT,             -- e.g. "Academic Search Ultimate"
    -- Search context (from seed JSON)
    bquery_original       TEXT,
    bquery_normalized     TEXT,             -- JSON array serialised as text when list
    variant_index         INTEGER,          -- 1-based; 0 or NULL = no variant
    -- Gap context (the research question that led to this article being pulled)
    gap_research_question TEXT,
    gap_topic             TEXT,
    -- Bookkeeping
    indexed_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, gap_id, source_id, title)
);

-- FTS5 virtual table (content-backed — rows shadow articles table via triggers)
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title,
    authors,
    abstract,
    journal,
    gap_research_question,
    content='articles',
    content_rowid='id',
    tokenize='porter'
);

-- Keep FTS in sync: INSERT
CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, authors, abstract, journal, gap_research_question)
    VALUES (
        new.id,
        COALESCE(new.title, ''),
        COALESCE(new.authors, ''),
        COALESCE(new.abstract, ''),
        COALESCE(new.journal, ''),
        COALESCE(new.gap_research_question, '')
    );
END;

-- Keep FTS in sync: DELETE
CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, authors, abstract, journal, gap_research_question)
    VALUES (
        'delete',
        old.id,
        COALESCE(old.title, ''),
        COALESCE(old.authors, ''),
        COALESCE(old.abstract, ''),
        COALESCE(old.journal, ''),
        COALESCE(old.gap_research_question, '')
    );
END;

-- Keep FTS in sync: UPDATE
CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, authors, abstract, journal, gap_research_question)
    VALUES (
        'delete',
        old.id,
        COALESCE(old.title, ''),
        COALESCE(old.authors, ''),
        COALESCE(old.abstract, ''),
        COALESCE(old.journal, ''),
        COALESCE(old.gap_research_question, '')
    );
    INSERT INTO articles_fts(rowid, title, authors, abstract, journal, gap_research_question)
    VALUES (
        new.id,
        COALESCE(new.title, ''),
        COALESCE(new.authors, ''),
        COALESCE(new.abstract, ''),
        COALESCE(new.journal, ''),
        COALESCE(new.gap_research_question, '')
    );
END;

CREATE INDEX IF NOT EXISTS idx_source    ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_gap       ON articles(gap_id);
CREATE INDEX IF NOT EXISTS idx_run       ON articles(run_id);
CREATE INDEX IF NOT EXISTS idx_doi       ON articles(doi)          WHERE doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_canonical ON articles(canonical_id) WHERE canonical_id IS NOT NULL;
"""


# ---------------------------------------------------------------------------
# Markdown field parser
# ---------------------------------------------------------------------------

# Pattern matches: **Label:** value  (with optional trailing whitespace)
_MD_FIELD_RE = re.compile(r"^\*\*(?P<key>[^*]+?):\*\*\s*(?P<value>.*?)\s*$")


def _parse_markdown_article(text: str) -> Dict[str, str]:
    """Extract structured fields from an article markdown file.

    The markdown format written by ``_write_ebsco_records`` and ``save_abstract``
    in ``adapters/document_fetch.py`` is:

        # <Title>

        **Authors:** <value>
        **Source:** <value>          (EBSCO: "Journal, date, volume, issue, page")
        **Date:** <value>
        **Database:** <value>        (optional)
        **URL:** <value>             (optional)
        **PDF:** <value>             (optional)
        **DOI:** <value>             (optional, written by save_abstract)

        ## Abstract

        <abstract text (may be multi-line)>

    Returns a dict with keys: title, authors, journal, pub_date, database,
    url, pdf_url, doi, abstract.
    """
    lines = text.splitlines()
    result: Dict[str, str] = {}
    abstract_lines: List[str] = []
    in_abstract = False

    for line in lines:
        if in_abstract:
            # Skip the FTS-index sentinel "_(not available)_"
            if line.strip() not in ("_(not available)_", "_(no abstract)_"):
                abstract_lines.append(line)
            continue

        # Title: first H1 heading
        if line.startswith("# ") and "title" not in result:
            result["title"] = line[2:].strip()
            continue

        # Section header signals start of abstract body
        if line.strip() == "## Abstract":
            in_abstract = True
            continue

        # Structured field lines
        m = _MD_FIELD_RE.match(line)
        if m:
            key = m.group("key").strip().lower()
            value = m.group("value").strip()
            if key == "authors":
                result["authors"] = value
            elif key == "source":
                # Source contains journal + volume/issue/page info — store raw
                result["journal"] = value
            elif key in ("published in",):
                # JSTOR / MUSE use "Published in:" instead of "Source:"
                result["journal"] = value
            elif key == "date":
                result["pub_date"] = value
            elif key == "database":
                result["database_name"] = value
            elif key == "url":
                result["url"] = value
            elif key in ("pdf", "pdf_url"):
                result["pdf_url"] = value
            elif key == "doi":
                doi_val = value.strip()
                if doi_val and doi_val != "—":
                    result["doi"] = doi_val

    # Combine abstract lines, strip leading/trailing blanks
    abstract = "\n".join(abstract_lines).strip()
    if abstract:
        result["abstract"] = abstract

    return result


# ---------------------------------------------------------------------------
# Seed JSON context extraction
# ---------------------------------------------------------------------------

def _extract_seed_context(src_dir: Path) -> Dict[str, str]:
    """Collect bquery_original and bquery_normalized from seed JSON files.

    A gap/source directory may contain multiple seed JSON files (one per query
    variant).  We collect the first non-empty values found; if multiple seeds
    exist they typically share the same gap-level query but the caller can
    associate per-file context by examining file names.

    Returns dict with: bquery_original, bquery_normalized (serialised list or
    bare string → always stored as JSON text).
    """
    context: Dict[str, str] = {}
    for json_file in sorted(src_dir.glob("*.json")):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        records = payload if isinstance(payload, list) else [payload]
        for rec in records:
            if not isinstance(rec, dict):
                continue
            if not context.get("bquery_original") and rec.get("bquery_original"):
                context["bquery_original"] = str(rec["bquery_original"])
            if not context.get("bquery_normalized") and rec.get("bquery_normalized"):
                val = rec["bquery_normalized"]
                if isinstance(val, list):
                    context["bquery_normalized"] = json.dumps(val)
                else:
                    context["bquery_normalized"] = str(val)
    return context


# ---------------------------------------------------------------------------
# Gap context loader (reads runs.json)
# ---------------------------------------------------------------------------

def gap_context_for(
    run_id: str,
    gap_id: str,
    runs_json_path: Optional[Path] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Return (research_question, topic/chapter) for a gap.

    Reads ``data/runs.json`` which the pipeline populates on every run.
    The ``claim_text`` field on the Gap object is the research question (the
    sentence in the manuscript that has no citation); the ``chapter`` field
    names the manuscript section — we use it as the "topic".

    Returns ``(None, None)`` if the run or gap is not found.
    """
    if runs_json_path is None:
        # Default: project_root/data/runs.json
        runs_json_path = Path(__file__).resolve().parent.parent / "data" / "runs.json"

    if not runs_json_path.exists():
        return None, None

    try:
        runs = json.loads(runs_json_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None, None

    run = runs.get(run_id)
    if not run:
        return None, None

    # Try gap_map first (raw gap analysis output), then research_plan (reflected)
    for plan_key in ("gap_map", "research_plan"):
        plan = run.get(plan_key) or {}
        for gap in plan.get("gaps") or []:
            if gap.get("gap_id") == gap_id:
                claim = gap.get("claim_text") or None
                chapter = gap.get("chapter") or None
                return claim, chapter

    return None, None


# ---------------------------------------------------------------------------
# DB open / schema creation
# ---------------------------------------------------------------------------

def open_index(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite article index at *db_path*.

    Creates the full schema (tables, FTS, triggers, indexes) if it doesn't
    already exist.  Safe to call on an existing DB — all DDL uses
    ``CREATE … IF NOT EXISTS``.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Enable WAL for better concurrency (reader doesn't block writer)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Source priority order for DOI dedup canonical selection (lower = more preferred)
_SOURCE_PRIORITY: Dict[str, int] = {  # noqa: F841 (used in DDL ORDER BY CASE)
    "ebsco_api": 1,
    "ebscohost": 2,
    "jstor": 3,
    "project_muse": 4,
}


def ingest_pull_output(
    conn: sqlite3.Connection,
    pull_root: Path,
    run_id: str,
    *,
    runs_json_path: Optional[Path] = None,
    gap_filter: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> int:
    """Walk *pull_root* and index all fetched article markdown files.

    Parameters
    ----------
    conn:
        Open SQLite connection (from ``open_index``).
    pull_root:
        Path to the run's pull_output directory
        (e.g. ``data/pull_outputs/run_27f86e44394442``).
    run_id:
        Identifier for the run (used as provenance in every row).
    runs_json_path:
        Override for ``data/runs.json`` (defaults to project-root location).
    gap_filter:
        When set, only index the named gap_id (for incremental / targeted runs).

    Returns the number of new rows inserted (existing rows silently skipped via
    the UNIQUE constraint).
    """
    if not pull_root.exists():
        return 0

    # Root used to compute relative paths for md_path and pdf_path.
    # In tests tmp_path is outside the repo root, so callers can override.
    _root = repo_root or _REPO_ROOT

    inserted = 0

    # Build a per-gap context cache so we only parse runs.json once per gap
    _gap_ctx_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

    def _gap_ctx(gap_id: str) -> Tuple[Optional[str], Optional[str]]:
        if gap_id not in _gap_ctx_cache:
            _gap_ctx_cache[gap_id] = gap_context_for(run_id, gap_id, runs_json_path)
        return _gap_ctx_cache[gap_id]

    for gap_dir in sorted(pull_root.iterdir()):
        if not gap_dir.is_dir():
            continue
        gap_id = gap_dir.name
        if gap_filter and gap_id != gap_filter:
            continue

        research_question, gap_topic = _gap_ctx(gap_id)

        for src_dir in sorted(gap_dir.iterdir()):
            if not src_dir.is_dir():
                continue
            source_id = src_dir.name
            # Skip the fetched/ subdirectory — it is a child of src_dir, not a source
            if source_id == "fetched":
                continue

            fetch_dir = src_dir / "fetched"
            if not fetch_dir.is_dir():
                continue

            # Collect seed context (bquery fields) from JSON files in this dir
            seed_ctx = _extract_seed_context(src_dir)
            bquery_original   = seed_ctx.get("bquery_original")
            bquery_normalized = seed_ctx.get("bquery_normalized")

            for md_file in sorted(fetch_dir.glob("*.md")):
                # Skip the raw HTML backup files stored as .html
                try:
                    text = md_file.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                fields = _parse_markdown_article(text)
                title = fields.get("title", "").strip()
                if not title:
                    continue

                # Compute relative paths from repo root (or caller-supplied root)
                try:
                    md_rel = str(md_file.relative_to(_root))
                except ValueError:
                    # Outside repo root (e.g. in tests with tmp_path) — use absolute
                    md_rel = str(md_file)
                pdf_path: Optional[str] = None
                pdf_candidate = fetch_dir / (md_file.stem + ".pdf")
                if pdf_candidate.exists():
                    try:
                        pdf_path = str(pdf_candidate.relative_to(_root))
                    except ValueError:
                        pdf_path = str(pdf_candidate)

                doi = fields.get("doi") or None
                # Normalise empty DOI strings
                if doi and not doi.strip():
                    doi = None

                try:
                    conn.execute(
                        """
                        INSERT INTO articles (
                            doi, title, authors, journal, pub_date, abstract,
                            url, pdf_path, md_path,
                            run_id, gap_id, source_id, database_name,
                            bquery_original, bquery_normalized,
                            gap_research_question, gap_topic
                        ) VALUES (
                            :doi, :title, :authors, :journal, :pub_date, :abstract,
                            :url, :pdf_path, :md_path,
                            :run_id, :gap_id, :source_id, :database_name,
                            :bquery_original, :bquery_normalized,
                            :gap_research_question, :gap_topic
                        )
                        """,
                        {
                            "doi": doi,
                            "title": title,
                            "authors": fields.get("authors") or None,
                            "journal": fields.get("journal") or None,
                            "pub_date": fields.get("pub_date") or None,
                            "abstract": fields.get("abstract") or None,
                            "url": fields.get("url") or None,
                            "pdf_path": pdf_path,
                            "md_path": md_rel,
                            "run_id": run_id,
                            "gap_id": gap_id,
                            "source_id": source_id,
                            "database_name": fields.get("database_name") or None,
                            "bquery_original": bquery_original,
                            "bquery_normalized": bquery_normalized,
                            "gap_research_question": research_question,
                            "gap_topic": gap_topic,
                        },
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    # Row already exists (UNIQUE constraint on run_id, gap_id, source_id, title)
                    pass

    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# DOI deduplication
# ---------------------------------------------------------------------------

def dedupe_by_doi(
    conn: sqlite3.Connection,
    run_id: Optional[str] = None,
) -> int:
    """Set canonical_id on duplicate DOI rows to point at the canonical row.

    For each DOI that appears in more than one row:
      - Picks the canonical row by: pdf_path IS NOT NULL first, then source
        priority (ebsco_api > jstor > project_muse), then earliest indexed_at.
      - Sets canonical_id = canonical.id on all other rows with that DOI.

    Does NOT delete any rows or modify any files on disk.

    Parameters
    ----------
    run_id:
        When supplied, only deduplicates within the given run. When None,
        deduplicates across all runs (cross-run DOI matching).

    Returns the number of rows that were marked as duplicates (had
    canonical_id set or updated).
    """
    where_clause = "WHERE doi IS NOT NULL"
    params: List = []
    if run_id:
        where_clause += " AND run_id = ?"
        params.append(run_id)

    # Find all DOIs with more than one row
    duplicated = conn.execute(
        f"""
        SELECT doi FROM articles {where_clause}
        GROUP BY doi HAVING COUNT(*) > 1
        """,
        params,
    ).fetchall()

    updated = 0
    for row in duplicated:
        doi = row[0]
        # Fetch all rows for this DOI, ordered by preference:
        # 1. Has PDF (pdf_path IS NOT NULL) → prefer
        # 2. Source priority (lower number = better)
        # 3. Earlier indexed_at
        order_params: List = [doi]
        if run_id:
            extra = " AND run_id = ?"
            order_params.append(run_id)
        else:
            extra = ""

        rows = conn.execute(
            f"""
            SELECT id, source_id, pdf_path, indexed_at
            FROM articles
            WHERE doi = ? {extra}
            ORDER BY
                CASE WHEN pdf_path IS NOT NULL THEN 0 ELSE 1 END ASC,
                CASE source_id
                    WHEN 'ebsco_api'    THEN 1
                    WHEN 'ebscohost'    THEN 2
                    WHEN 'jstor'        THEN 3
                    WHEN 'project_muse' THEN 4
                    ELSE 5
                END ASC,
                indexed_at ASC
            """,
            order_params,
        ).fetchall()

        if len(rows) < 2:
            continue

        canonical_id = rows[0]["id"]
        for dup_row in rows[1:]:
            dup_id = dup_row["id"]
            # Only update if not already pointing at the right canonical row
            if dup_row["id"] != canonical_id:
                conn.execute(
                    "UPDATE articles SET canonical_id = ? WHERE id = ? AND (canonical_id IS NULL OR canonical_id != ?)",
                    (canonical_id, dup_id, canonical_id),
                )
                updated += conn.total_changes

    conn.commit()
    return updated
