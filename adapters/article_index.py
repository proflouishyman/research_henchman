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

Ingest passes:
  1. Markdown walk — for source dirs that have a ``fetched/`` subdirectory, walks
     ``fetched/*.md`` files (EBSCO, JSTOR, Project MUSE).
  2. Seed JSON walk — for source dirs that have NO ``fetched/`` subdirectory (e.g.
     ProQuest sources that write JSON-only records), reads ``*.json`` files directly.
     Records with ``link_type == "provider_search"`` are skipped (those are EBSCO
     search-parameter records, not real articles). The md_path and pdf_path are
     stored as NULL for JSON-only records.
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
    -- Availability / source-specific metadata (Phase 1)
    access                TEXT,             -- HathiTrust/IA: "Full view" / "Limited (search-only)"
    hathi_id              TEXT,             -- HathiTrust stable identifier e.g. "mdp.49015001020396"
    subject               TEXT,             -- Subject classification from source
    language              TEXT,             -- Language of the item
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
CREATE INDEX IF NOT EXISTS idx_access    ON articles(access)       WHERE source_id='hathitrust_fulltext';
"""

# Migration DDL: add new columns to existing databases that predate Phase 1.
# These are no-ops when the columns already exist (SQLite ignores ADD COLUMN
# for existing columns only when using IF NOT EXISTS, which it doesn't support
# natively — we catch OperationalError instead in open_index).
_MIGRATION_DDL = [
    "ALTER TABLE articles ADD COLUMN access    TEXT",
    "ALTER TABLE articles ADD COLUMN hathi_id  TEXT",
    "ALTER TABLE articles ADD COLUMN subject   TEXT",
    "ALTER TABLE articles ADD COLUMN language  TEXT",
]


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
# Seed JSON article ingestion (ProQuest and other JSON-only sources)
# ---------------------------------------------------------------------------

# Records with this link_type are search-parameter metadata, not real articles.
# They appear in EBSCO seed JSON files and must never be indexed as articles.
_SKIP_LINK_TYPES = {"provider_search"}

# Title value used by EBSCO seed records — not a real article title.
_EBSCO_SEED_TITLE = "ebsco_api search results"


def _ingest_seed_json(
    conn: sqlite3.Connection,
    src_dir: Path,
    *,
    run_id: str,
    gap_id: str,
    source_id: str,
    research_question: Optional[str],
    gap_topic: Optional[str],
) -> int:
    """Index article records from JSON seed files in *src_dir*.

    Called for source directories that have no ``fetched/`` subdirectory — these
    are "JSON-only" sources such as ProQuest collections where the pull script
    writes metadata directly as JSON without creating ``fetched/*.md`` files.

    Each JSON file may contain a list of record dicts or a single record dict.
    Records with ``link_type == "provider_search"`` (EBSCO search-parameter
    records) and records without a meaningful title are skipped.

    The ``bquery_original`` / ``bquery_normalized`` fields are populated from
    the record itself when present (rare), otherwise stored as NULL. The
    ``query`` field in ProQuest records is stored as-is in ``bquery_original``
    when no dedicated bquery field exists.

    Returns the number of new rows inserted.
    """
    inserted = 0

    for json_file in sorted(src_dir.glob("*.json")):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue

        # Normalise to a list of dicts — some files contain a single dict.
        records = payload if isinstance(payload, list) else [payload]

        for rec in records:
            if not isinstance(rec, dict):
                continue

            # Skip search-parameter records (EBSCO provider_search seeds).
            link_type = rec.get("link_type", "")
            if link_type in _SKIP_LINK_TYPES:
                continue

            title = (rec.get("title") or "").strip()
            if not title or title == _EBSCO_SEED_TITLE:
                continue

            # --- Field extraction ---

            # URL: prefer detail_url (ProQuest canonical link), fall back to url
            url = (rec.get("detail_url") or rec.get("url") or "").strip() or None

            # DOI: empty string → NULL
            doi = (rec.get("doi") or "").strip() or None

            authors = (rec.get("authors") or "").strip() or None
            journal = (rec.get("journal") or "").strip() or None
            pub_date = (rec.get("pub_date") or "").strip() or None
            abstract = (rec.get("abstract") or "").strip() or None

            # Phase 1: availability and source-specific metadata fields.
            # HathiTrust and Internet Archive records carry these; other
            # sources leave them NULL.
            access   = (rec.get("access") or "").strip() or None
            hathi_id = (rec.get("hathi_id") or "").strip() or None
            subject  = (rec.get("subject") or "").strip() or None
            language = (rec.get("language") or "").strip() or None

            # bquery context: use dedicated fields when present; otherwise
            # fall back to the ``query`` field (ProQuest records store the
            # Boolean search string there).
            bquery_original: Optional[str] = None
            bquery_normalized: Optional[str] = None

            if rec.get("bquery_original"):
                bquery_original = str(rec["bquery_original"])
            elif rec.get("query"):
                # ProQuest records: store the query string as bquery_original
                # so FTS-context queries can still surface this source.
                bquery_original = str(rec["query"])

            if rec.get("bquery_normalized"):
                val = rec["bquery_normalized"]
                if isinstance(val, list):
                    bquery_normalized = json.dumps(val)
                else:
                    bquery_normalized = str(val)

            try:
                conn.execute(
                    """
                    INSERT INTO articles (
                        doi, title, authors, journal, pub_date, abstract,
                        url, pdf_path, md_path,
                        run_id, gap_id, source_id,
                        bquery_original, bquery_normalized,
                        gap_research_question, gap_topic,
                        access, hathi_id, subject, language
                    ) VALUES (
                        :doi, :title, :authors, :journal, :pub_date, :abstract,
                        :url, NULL, NULL,
                        :run_id, :gap_id, :source_id,
                        :bquery_original, :bquery_normalized,
                        :gap_research_question, :gap_topic,
                        :access, :hathi_id, :subject, :language
                    )
                    """,
                    {
                        "doi": doi,
                        "title": title,
                        "authors": authors,
                        "journal": journal,
                        "pub_date": pub_date,
                        "abstract": abstract,
                        "url": url,
                        "run_id": run_id,
                        "gap_id": gap_id,
                        "source_id": source_id,
                        "bquery_original": bquery_original,
                        "bquery_normalized": bquery_normalized,
                        "gap_research_question": research_question,
                        "gap_topic": gap_topic,
                        "access": access,
                        "hathi_id": hathi_id,
                        "subject": subject,
                        "language": language,
                    },
                )
                inserted += 1
            except sqlite3.IntegrityError:
                # Duplicate row (UNIQUE on run_id, gap_id, source_id, title) — skip.
                pass

    return inserted


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

    Phase 1 migration: adds the access/hathi_id/subject/language columns to
    databases created before the Phase 1 schema upgrade. Each ALTER TABLE is
    attempted individually; OperationalError means the column already exists
    and is silently ignored.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Enable WAL for better concurrency (reader doesn't block writer)
    conn.execute("PRAGMA journal_mode=WAL")
    # Phase 1 migration: add new columns BEFORE running the full DDL script.
    # The DDL's CREATE TABLE IF NOT EXISTS uses the new columns in its schema,
    # so for existing DBs we must ADD them first (for new DBs the ALTER TABLEs
    # are no-ops because the CREATE TABLE already includes the columns).
    for stmt in _MIGRATION_DDL:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already present — idempotent
    conn.commit()
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
    """Walk *pull_root* and index all article records (markdown files + JSON seeds).

    Two passes are performed for each source directory:

    1. **Markdown walk** — if ``<gap>/<source>/fetched/`` exists, index every
       ``*.md`` file in it (EBSCO, JSTOR, Project MUSE).  pdf_path and md_path
       are populated with repo-relative paths when the files exist on disk.

    2. **Seed JSON walk** — if ``<gap>/<source>/fetched/`` does NOT exist (i.e.
       a JSON-only source such as ProQuest), index article records directly from
       ``*.json`` files in ``<gap>/<source>/``.  pdf_path and md_path are NULL.
       Records with ``link_type == "provider_search"`` are skipped.

    The two passes are mutually exclusive per source directory, so EBSCO .md
    files always take priority over any JSON records for the same source.

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
    repo_root:
        Override the project root used for computing relative file paths
        (defaults to the repo root inferred from this module's location).

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
                # No fetched/ subdirectory — this is a JSON-only source (e.g. ProQuest).
                # Index article records directly from the seed JSON files.
                inserted += _ingest_seed_json(
                    conn,
                    src_dir,
                    run_id=run_id,
                    gap_id=gap_id,
                    source_id=source_id,
                    research_question=research_question,
                    gap_topic=gap_topic,
                )
                continue

            # --- Markdown walk (EBSCO, JSTOR, Project MUSE, etc.) ---

            # Collect seed context (bquery fields) from JSON files in this dir.
            # Only used for the markdown pass; JSON-only sources embed their own context.
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


# ---------------------------------------------------------------------------
# User marks (star + read) — v3 DB-backed replacement for localStorage
# ---------------------------------------------------------------------------

_MARKS_DDL = """
CREATE TABLE IF NOT EXISTS user_marks (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id),
    starred    INTEGER NOT NULL DEFAULT 0,
    read       INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def ensure_marks_schema(conn: sqlite3.Connection) -> None:
    """Create the user_marks table if it doesn't already exist.

    Idempotent — safe to call repeatedly.
    """
    conn.executescript(_MARKS_DDL)
    conn.commit()


def set_mark(
    conn: sqlite3.Connection,
    article_id: int,
    *,
    starred: Optional[bool] = None,
    read: Optional[bool] = None,
    note: Optional[str] = None,
) -> None:
    """Upsert a mark for an article.

    Only the supplied keyword args are updated; existing values are
    preserved for omitted fields. Rows with starred=False and read=False
    and no note are deleted to keep the table clean.
    """
    ensure_marks_schema(conn)

    existing = conn.execute(
        "SELECT starred, read, note FROM user_marks WHERE article_id = ?",
        (article_id,),
    ).fetchone()

    cur_starred = bool(existing["starred"]) if existing else False
    cur_read = bool(existing["read"]) if existing else False
    cur_note = (existing["note"] or "") if existing else ""

    new_starred = starred if starred is not None else cur_starred
    new_read = read if read is not None else cur_read
    new_note = note if note is not None else cur_note

    # Prune rows that are completely empty (no star, no read, no note).
    if not new_starred and not new_read and not (new_note or "").strip():
        conn.execute("DELETE FROM user_marks WHERE article_id = ?", (article_id,))
    else:
        conn.execute(
            """INSERT INTO user_marks (article_id, starred, read, note, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(article_id) DO UPDATE SET
                   starred    = excluded.starred,
                   read       = excluded.read,
                   note       = excluded.note,
                   updated_at = excluded.updated_at""",
            (article_id, int(new_starred), int(new_read), new_note or None),
        )
    conn.commit()


def get_marks(
    conn: sqlite3.Connection,
    article_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """Fetch marks for the given article ids.

    Returns {article_id: {starred, read, note, updated_at}} for any ids
    that have marks; ids without marks are absent from the result.
    """
    if not article_ids:
        return {}
    ensure_marks_schema(conn)
    placeholders = ",".join("?" for _ in article_ids)
    rows = conn.execute(
        f"SELECT article_id, starred, read, note, updated_at "
        f"FROM user_marks WHERE article_id IN ({placeholders})",
        article_ids,
    ).fetchall()
    return {
        int(r["article_id"]): {
            "starred":    bool(r["starred"]),
            "read":       bool(r["read"]),
            "note":       r["note"] or "",
            "updated_at": r["updated_at"] or "",
        }
        for r in rows
    }


def list_starred(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Return all starred marks sorted by updated_at desc."""
    ensure_marks_schema(conn)
    rows = conn.execute(
        "SELECT article_id, starred, read, note, updated_at "
        "FROM user_marks WHERE starred = 1 ORDER BY updated_at DESC"
    ).fetchall()
    return [
        {
            "article_id": int(r["article_id"]),
            "starred":    True,
            "read":       bool(r["read"]),
            "note":       r["note"] or "",
            "updated_at": r["updated_at"] or "",
        }
        for r in rows
    ]
