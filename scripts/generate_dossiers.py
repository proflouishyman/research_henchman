#!/usr/bin/env python3
"""Generate per-gap research dossiers from the scored article index.

Output layout (under ``data/dossiers/`` by default):

    data/dossiers/
    ├── INDEX.md                    — chapter list, gap counts, source totals
    ├── 00_corpus_readings.md       — titles cited in 10+ gaps corpus-wide
    ├── 01_introduction/
    │   ├── 00_chapter_readings.md  — titles cited in 3+ gaps in this chapter
    │   ├── AUTO-01-G1.md           — one dossier per gap
    │   ├── AUTO-02-G1.md
    │   └── ...
    └── <next_chapter>/

Per-gap dossier sections (in order):
    Tier 3  — cite-worthy primary sources
    Tier 2  — adjacent context
    Tier 1  — tangential mentions (one-line entries)
    Tier 0  — search false positives (collapsed for transparency)
    Unscored — rows the LLM hasn't seen yet (transparent)

Each entry includes the LLM's WHY text, source(s) the row was found in
(merged across cross-source dupes within the gap), pub date, URL, and
local PDF path when available. When a title also appears in other gaps,
those gap_ids are listed inline so the user can spot thematic links.

Usage:
    python3 scripts/generate_dossiers.py
    python3 scripts/generate_dossiers.py --run-id run_27f86e44394442
    python3 scripts/generate_dossiers.py --gap AUTO-01-G1   # one gap
    python3 scripts/generate_dossiers.py --output-dir /tmp/preview
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Allow running this script directly (``python3 scripts/generate_dossiers.py``)
# by ensuring the repo root is on sys.path before importing project layers.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Reuse the shared dossier-render layer so the markdown writer, the API
# endpoint, and any future surface produce the same per-gap structure.
# Local re-imports preserve the script's existing public symbol names
# (older callers may dot-into ``generate_dossiers.norm_title`` etc.).
from layers.dossier_render import (
    SOURCE_PRIORITY,
    absolutize_url,
    build_cross_gap_index,
    chapter_slug,
    dedupe_within_gap,
    fetch_gap_rows,
    norm_title,
    pick_primary,
    render_url_or_pdf,
    src_label,
)

SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_DB   = PROJECT_ROOT / "data/article_index.sqlite"
DEFAULT_OUT  = PROJECT_ROOT / "data/dossiers"

CORPUS_READING_MIN_GAPS  = 10  # title must appear in this many gaps to be corpus-wide
CHAPTER_READING_MIN_GAPS = 3   # title must appear in this many gaps within a chapter


# ---------------------------------------------------------------------------
# Markdown-only helpers (formatting, escapes)
# ---------------------------------------------------------------------------


def md_escape(s: str) -> str:
    """Escape characters that have meaning in markdown headings/lists."""
    if not s:
        return ""
    return s.replace("\n", " ").replace("|", "\\|").strip()


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_entry(
    entry: Dict[str, Any],
    *,
    other_gaps: List[str],
    self_gap: str,
    short: bool = False,
) -> str:
    """Return a markdown chunk for one consolidated entry.

    *short=True* yields a single-line entry (used for tier 1 and tier 0
    sections to keep the dossier scannable).
    """
    p = entry["primary"]
    title = md_escape(p["title"] or "(untitled)")
    pub = (p["pub_date"] or "").strip()
    auth = (p["authors"] or "").strip()
    why = (p["relevance_why"] or "").strip()
    sources = entry["sources"]
    primary_src = src_label(p["source_id"])
    extra_srcs = [src_label(s) for s in sources if s != p["source_id"]]
    src_block = primary_src
    if extra_srcs:
        src_block += f" (also in: {', '.join(extra_srcs)})"
    link = render_url_or_pdf(p)

    others_in_gaps = [g for g in other_gaps if g != self_gap]
    cross_gap_str = ""
    if others_in_gaps:
        if len(others_in_gaps) <= 8:
            cross_gap_str = ", ".join(others_in_gaps)
        else:
            cross_gap_str = f"{', '.join(others_in_gaps[:8])} … (+{len(others_in_gaps)-8} more)"

    if short:
        # Single-line; useful for tier 1 / tier 0 noise.
        head = f"- [{p['relevance_score']}] **{title}**"
        meta_bits = []
        if auth:    meta_bits.append(auth[:60])
        if pub:     meta_bits.append(pub[:20])
        meta_bits.append(primary_src)
        head += f" — {' · '.join(meta_bits)}"
        if why:
            head += f" — _{why}_"
        head += f" — {link}"
        return head + "\n"

    # Full entry — tier 2 and tier 3.
    lines: List[str] = []
    head = f"### {title}"
    if pub or auth:
        bits = []
        if auth: bits.append(auth)
        if pub:  bits.append(pub)
        head = f"### {title}  ({' · '.join(bits)})"
    lines.append(head)
    lines.append(f"- **Source**: {src_block}")
    if why:
        lines.append(f"- **Why**: {why}")
    lines.append(f"- **Link**: {link}")
    if cross_gap_str:
        lines.append(f"- **Also relevant to**: {cross_gap_str}")
    return "\n".join(lines) + "\n"


def render_gap_dossier(
    gap_id: str,
    rows: List[sqlite3.Row],
    cross_gap_idx: Dict[str, List[str]],
) -> str:
    """Render a single gap's dossier as a markdown string."""
    if not rows:
        return f"# Gap {gap_id}\n\n*(no rows in index)*\n"

    sample = rows[0]
    topic = sample["gap_topic"] or "(no chapter)"
    claim = (sample["gap_research_question"] or "").strip()

    consolidated = dedupe_within_gap(rows)

    by_score: Dict[Optional[int], List[Dict[str, Any]]] = defaultdict(list)
    for entry in consolidated:
        s = entry["primary"]["relevance_score"]
        by_score[s].append(entry)

    # Sort within each tier: by source priority (so top-tier ranks EBSCO first),
    # then by pub_date desc (so newer is shown first).
    def sort_key(e: Dict[str, Any]) -> tuple:
        p = e["primary"]
        src_rank = SOURCE_PRIORITY.get(p["source_id"], 99)
        # Pub date is freeform text — fall back to a string sort, which is
        # roughly "more recent first" if dates are 4-digit years.
        date = (p["pub_date"] or "").strip()
        return (src_rank, -(int(re.search(r"\d{4}", date).group()) if re.search(r"\d{4}", date) else 0))
    for s in by_score:
        by_score[s].sort(key=sort_key)

    out: List[str] = []
    out.append(f"# Gap {gap_id} — {md_escape(topic)}\n")
    if claim:
        out.append(f"> {claim[:600]}\n")
    out.append("")

    summary = (
        f"**Summary**: {sum(len(v) for v in by_score.values())} consolidated entries "
        f"from {len(rows)} raw rows. "
        f"Tier 3: {len(by_score.get(3,[]))} · "
        f"Tier 2: {len(by_score.get(2,[]))} · "
        f"Tier 1: {len(by_score.get(1,[]))} · "
        f"Tier 0: {len(by_score.get(0,[]))} · "
        f"Unscored: {len(by_score.get(None,[]))}"
    )
    out.append(summary)
    out.append("")

    def emit_tier(score: int, heading: str, short: bool):
        items = by_score.get(score, [])
        if not items:
            return
        out.append(f"## {heading} _(score {score} — {len(items)} entries)_\n")
        if short:
            out.append("<details>\n<summary>Show entries</summary>\n")
        for entry in items:
            other_gaps = cross_gap_idx.get(entry["norm"], [])
            out.append(render_entry(entry, other_gaps=other_gaps,
                                    self_gap=gap_id, short=short))
        if short:
            out.append("</details>\n")
        out.append("")

    emit_tier(3, "Tier 3 — cite-worthy primary sources", short=False)
    emit_tier(2, "Tier 2 — adjacent context",            short=False)
    emit_tier(1, "Tier 1 — tangential mentions",         short=True)
    emit_tier(0, "Tier 0 — search false positives",      short=True)

    if by_score.get(None):
        out.append(f"## Unscored ({len(by_score[None])} entries) — LLM hasn't seen these yet\n")
        out.append("<details>\n<summary>Show unscored</summary>\n")
        for entry in by_score[None]:
            other_gaps = cross_gap_idx.get(entry["norm"], [])
            out.append(render_entry(entry, other_gaps=other_gaps,
                                    self_gap=gap_id, short=True))
        out.append("</details>\n")

    return "\n".join(out)


def render_chapter_readings(
    topic: str,
    gaps_in_chapter: List[str],
    cross_gap_idx: Dict[str, List[str]],
    title_index: Dict[str, sqlite3.Row],
    min_gaps: int,
) -> str:
    """Render the per-chapter core readings file."""
    gaps_set = set(gaps_in_chapter)
    qualified: List[Tuple[str, List[str]]] = []
    for norm, gap_list in cross_gap_idx.items():
        in_chapter = sorted(set(gap_list) & gaps_set)
        if len(in_chapter) >= min_gaps:
            qualified.append((norm, in_chapter))
    qualified.sort(key=lambda x: -len(x[1]))

    out: List[str] = []
    out.append(f"# Chapter core readings — {md_escape(topic)}\n")
    out.append(f"Titles relevant (score ≥ 1) to {min_gaps}+ gaps within this chapter. "
               "Read these first.\n")
    out.append(f"Chapter has {len(gaps_in_chapter)} gaps. Qualified core readings: {len(qualified)}.\n")
    if not qualified:
        out.append("*(no titles meet the threshold yet — re-run after scoring completes)*\n")
        return "\n".join(out)

    for norm, in_chapter in qualified:
        rep = title_index.get(norm)
        if not rep:
            continue
        title = md_escape(rep["title"] or "(untitled)")
        pub = (rep["pub_date"] or "").strip()
        auth = (rep["authors"] or "").strip()
        out.append(f"## {title}")
        bits = []
        if auth: bits.append(auth)
        if pub:  bits.append(pub)
        if bits: out.append(f"_{' · '.join(bits)}_\n")
        out.append(f"- **Cited in {len(in_chapter)} gaps**: {', '.join(in_chapter)}")
        link = render_url_or_pdf(rep)
        out.append(f"- **Link**: {link}\n")
    return "\n".join(out)


def render_corpus_readings(
    cross_gap_idx: Dict[str, List[str]],
    title_index: Dict[str, sqlite3.Row],
    min_gaps: int,
) -> str:
    """Render the corpus-wide core readings file."""
    qualified = [(n, gs) for n, gs in cross_gap_idx.items() if len(gs) >= min_gaps]
    qualified.sort(key=lambda x: -len(x[1]))

    out: List[str] = []
    out.append("# Corpus-wide core readings\n")
    out.append(f"Titles relevant (score ≥ 1) to {min_gaps}+ gaps across the whole "
               "manuscript. These are the foundational sources — read first.\n")
    if not qualified:
        out.append("*(no titles meet the threshold yet — re-run after scoring completes)*\n")
        return "\n".join(out)

    for norm, gaps in qualified:
        rep = title_index.get(norm)
        if not rep:
            continue
        title = md_escape(rep["title"] or "(untitled)")
        pub = (rep["pub_date"] or "").strip()
        auth = (rep["authors"] or "").strip()
        out.append(f"## {title}")
        bits = []
        if auth: bits.append(auth)
        if pub:  bits.append(pub)
        if bits: out.append(f"_{' · '.join(bits)}_\n")
        if len(gaps) <= 12:
            out.append(f"- **Cited in {len(gaps)} gaps**: {', '.join(gaps)}")
        else:
            out.append(f"- **Cited in {len(gaps)} gaps**: "
                       f"{', '.join(gaps[:12])} … (+{len(gaps)-12} more)")
        link = render_url_or_pdf(rep)
        out.append(f"- **Link**: {link}\n")
    return "\n".join(out)


def render_index(
    chapters: Dict[str, List[str]],
    chapter_dir: Dict[str, str],
    stats: Dict[str, Any],
) -> str:
    """Render the top-level INDEX.md."""
    out: List[str] = []
    out.append("# Manuscript dossiers\n")
    out.append(f"Generated {datetime.now().isoformat(timespec='seconds')} from "
               f"`data/article_index.sqlite`.\n")
    out.append(f"- Total rows in index: **{stats['total_rows']:,}**")
    out.append(f"- Rows scored: **{stats['scored_rows']:,}** "
               f"({100*stats['scored_rows']//max(stats['total_rows'],1)}%)")
    out.append(f"- Distinct gaps: **{stats['gap_count']}**")
    out.append(f"- Chapters: **{len(chapters)}**")
    out.append(f"- Sources: {', '.join(sorted(stats['sources']))}\n")
    out.append("## Foundational reading (corpus-wide)")
    out.append(f"[00_corpus_readings.md](00_corpus_readings.md) — "
               f"titles cited in {CORPUS_READING_MIN_GAPS}+ gaps.\n")
    out.append("## Chapters\n")
    for topic, gaps in sorted(chapters.items()):
        slug = chapter_dir[topic]
        out.append(f"### {md_escape(topic)} _({len(gaps)} gaps)_")
        out.append(f"- [Chapter core readings]({slug}/00_chapter_readings.md)")
        out.append("- Gap dossiers:")
        for g in gaps:
            out.append(f"  - [{g}]({slug}/{g}.md)")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--output-dir", default=str(DEFAULT_OUT))
    p.add_argument("--gap", default=None, help="Only render this gap (still writes other globals).")
    p.add_argument("--chapter", default=None, help="Only render this chapter (substring match on gap_topic).")
    p.add_argument("--include-zero", action="store_true",
                   help="Include Tier 0 entries inline (default: collapsed).")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    # Discover gaps + chapter membership.
    gaps_rows = conn.execute(
        "SELECT DISTINCT gap_id, gap_topic FROM articles ORDER BY gap_id"
    ).fetchall()
    chapters: Dict[str, List[str]] = defaultdict(list)
    for r in gaps_rows:
        topic = (r["gap_topic"] or "(no chapter)").strip()
        chapters[topic].append(r["gap_id"])
    chapter_dir = {t: chapter_slug(t) for t in chapters}

    # Cross-gap title index — needed for "Also relevant to" cross-links and
    # for the chapter/corpus core-reading files.
    cross_gap_idx = build_cross_gap_index(conn)

    # Pick a representative row for each normalized title (best metadata).
    title_index: Dict[str, sqlite3.Row] = {}
    for n in cross_gap_idx:
        # Prefer a high-priority source; query a few candidates and rank.
        # Order by source priority via CASE expression baked at select time.
        case_sql = "CASE source_id "
        for src, rank in SOURCE_PRIORITY.items():
            case_sql += f"WHEN '{src}' THEN {rank} "
        case_sql += "ELSE 99 END"
        rows = conn.execute(
            f"""SELECT id, title, authors, pub_date, url, pdf_path,
                       source_id, doi, abstract, gap_id, gap_topic,
                       gap_research_question, relevance_score, relevance_why,
                       {case_sql} as src_rank
                  FROM articles
                 WHERE LOWER(REPLACE(REPLACE(REPLACE(title,'.',''),',',''),':','')) LIKE ?
                 ORDER BY pdf_path IS NULL, src_rank ASC
                 LIMIT 1""",
            (f"%{n[:60]}%",),
        ).fetchone()
        if rows:
            title_index[n] = rows

    # Stats
    total_rows = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    scored_rows = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE relevance_score IS NOT NULL"
    ).fetchone()[0]
    sources = {r[0] for r in conn.execute(
        "SELECT DISTINCT source_id FROM articles"
    ).fetchall()}
    stats = {
        "total_rows":  total_rows,
        "scored_rows": scored_rows,
        "gap_count":   len(gaps_rows),
        "sources":     sources,
    }

    print(f"DB: {args.db}", flush=True)
    print(f"  total rows:   {total_rows:,}", flush=True)
    print(f"  scored:       {scored_rows:,} "
          f"({100*scored_rows//max(total_rows,1)}%)", flush=True)
    print(f"  gaps:         {len(gaps_rows)}", flush=True)
    print(f"  chapters:     {len(chapters)}", flush=True)
    print(f"  output dir:   {out_root}", flush=True)

    # Render each chapter.
    n_gaps_written = 0
    for topic, gaps_in_chap in chapters.items():
        if args.chapter and args.chapter.lower() not in topic.lower():
            continue
        slug = chapter_dir[topic]
        chap_dir = out_root / slug
        chap_dir.mkdir(parents=True, exist_ok=True)

        # Chapter core readings
        readings_md = render_chapter_readings(
            topic, gaps_in_chap, cross_gap_idx, title_index,
            CHAPTER_READING_MIN_GAPS,
        )
        (chap_dir / "00_chapter_readings.md").write_text(readings_md, encoding="utf-8")

        # Per-gap dossiers
        for gap_id in gaps_in_chap:
            if args.gap and gap_id != args.gap:
                continue
            rows = fetch_gap_rows(conn, gap_id)
            md = render_gap_dossier(gap_id, rows, cross_gap_idx)
            (chap_dir / f"{gap_id}.md").write_text(md, encoding="utf-8")
            n_gaps_written += 1

    # Corpus-wide core readings
    corpus_md = render_corpus_readings(cross_gap_idx, title_index, CORPUS_READING_MIN_GAPS)
    (out_root / "00_corpus_readings.md").write_text(corpus_md, encoding="utf-8")

    # Index
    index_md = render_index(chapters, chapter_dir, stats)
    (out_root / "INDEX.md").write_text(index_md, encoding="utf-8")

    print(f"\nWrote:", flush=True)
    print(f"  {n_gaps_written} gap dossiers", flush=True)
    print(f"  {len(chapters)} chapter core-reading files", flush=True)
    print(f"  1 corpus-wide core-reading file", flush=True)
    print(f"  1 INDEX.md", flush=True)
    print(f"\nOpen: {out_root}/INDEX.md", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
