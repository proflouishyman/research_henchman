#!/usr/bin/env python3
"""Build the gap_tree from manuscript via Pass A (intro promises) and/or Pass B (bracketed TODOs).

Wave 1 of the multi-pass detector overhaul. NO pulls, scoring, or dossier
work happens here — this script only:

  1. Runs the requested detector passes against a .docx manuscript.
  2. Inserts new top-level gap_tree rows in ``data/article_index.sqlite``.
  3. Writes a human-readable markdown review file the user can edit (each
     gap gets a header, its claim_text, the LLM-derived research_question,
     and a status checkbox to approve or reject).

Resume semantics: a gap_id that already exists in the DB is skipped. To
force re-extraction, drop the relevant rows manually and re-run.

Usage:
  python3 scripts/build_gap_tree.py --pass A
  python3 scripts/build_gap_tree.py --pass B
  python3 scripts/build_gap_tree.py --pass A,B
  python3 scripts/build_gap_tree.py --review-file data/intro_promises_review.md

Defaults:
  --db            data/article_index.sqlite
  --manuscript    data/manuscript_exports/manuscript/manuscript.docx
  --model         qwen3.6:35b-a3b-mlx-bf16
  --formatter     llama3.1:8b
  --review-file   data/intro_promises_review.md

Out of scope for this wave: Pass C/D/E, articles-table modification, any
pulling/scoring/rendering, migration of legacy AUTO-NN-G1 rows.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env so ORCH_* settings (LLM provider, base URL, etc.) are available.
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, v = _line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from adapters.gap_tree import (  # noqa: E402
    count_by_pass,
    ensure_gap_tree_schema,
    gap_exists,
    insert_node,
    list_nodes,
)
from config import OrchestratorSettings  # noqa: E402
from layers.gap_detector import detect_pass_a, detect_pass_b  # noqa: E402
from layers.llm_client import make_llm_client  # noqa: E402

import sqlite3  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "article_index.sqlite"
DEFAULT_MANUSCRIPT = (
    PROJECT_ROOT
    / "data"
    / "manuscript_exports"
    / "manuscript"
    / "manuscript.docx"
)
DEFAULT_REVIEW_FILE = PROJECT_ROOT / "data" / "intro_promises_review.md"


# ---------------------------------------------------------------------------
# Pass runners
# ---------------------------------------------------------------------------

def _run_pass_a(
    *,
    conn: sqlite3.Connection,
    manuscript: Path,
    llm: Any,
    formatter_llm: Any,
) -> Dict[str, int]:
    print(f"[Pass A] extracting intro promises from {manuscript.name} …", flush=True)
    nodes = detect_pass_a(manuscript, llm, formatter_llm=formatter_llm)
    inserted = 0
    skipped = 0
    for node in nodes:
        gid = node["gap_id"]
        if gap_exists(conn, gid):
            skipped += 1
            continue
        ok = insert_node(conn, **node)
        if ok:
            inserted += 1
        else:
            skipped += 1
    print(f"[Pass A] {inserted} inserted, {skipped} skipped (already existed)", flush=True)
    return {"inserted": inserted, "skipped": skipped, "total": len(nodes)}


def _run_pass_b(
    *,
    conn: sqlite3.Connection,
    manuscript: Path,
    llm: Any,
) -> Dict[str, int]:
    print(f"[Pass B] extracting bracketed TODOs from {manuscript.name} …", flush=True)
    # `conn` is passed so detect_pass_b can resume — skipping LLM calls for
    # TODOs whose research_question is already populated in the DB.
    nodes = detect_pass_b(manuscript, llm=llm, conn=conn)
    inserted = 0
    skipped = 0
    for node in nodes:
        gid = node["gap_id"]
        if gap_exists(conn, gid):
            skipped += 1
            continue
        ok = insert_node(conn, **node)
        if ok:
            inserted += 1
        else:
            skipped += 1
    print(f"[Pass B] {inserted} inserted, {skipped} skipped (already existed)", flush=True)
    return {"inserted": inserted, "skipped": skipped, "total": len(nodes)}


# ---------------------------------------------------------------------------
# Review file writer
# ---------------------------------------------------------------------------

def write_review_file(
    *,
    conn: sqlite3.Connection,
    review_path: Path,
    passes_run: List[str],
) -> int:
    """Write a markdown file the user can edit to approve/reject pending gaps.

    Each pass becomes a ``## Pass X — …`` section. Each gap inside gets a
    ``### gap_id (chapter)`` heading with claim_text, research_question,
    and an approve/reject checkbox pair. Returns the number of gaps written.
    """
    lines: List[str] = []
    lines.append("# Gap Tree Manual Review")
    lines.append("")
    lines.append(
        "Edit this file in place. For each gap, mark either approve or reject "
        "by changing `[ ]` to `[x]`. A future CLI will sync the chosen status "
        "back into the SQLite ``gap_tree`` table."
    )
    lines.append("")

    pass_titles = {
        "A": "Pass A — Intro promises (`gap_type=intro_promise`)",
        "B": "Pass B — Bracketed TODOs (`gap_type=explicit_todo`)",
    }
    pass_filters = {"A": "A", "B": "B"}

    total_written = 0
    for letter in passes_run:
        title = pass_titles.get(letter, f"Pass {letter}")
        rows = list_nodes(conn, detector_pass=pass_filters[letter])
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"_{len(rows)} gap(s) currently in this pass._")
        lines.append("")
        if not rows:
            lines.append("_(no gaps emitted by this pass)_")
            lines.append("")
            continue
        for row in rows:
            gid = row["gap_id"]
            chap = row["chapter"] or "(unknown)"
            tier = row["tier"]
            ev = row["evidence_target"]
            claim = (row["claim_text"] or "").strip()
            rq = (row["research_question"] or "").strip()
            status = (row["status"] or "pending").strip()
            rationale = (row["rationale"] or "").strip()

            lines.append(f"### {gid} — {chap}")
            lines.append("")
            lines.append(f"- **Tier:** {tier}    **Evidence target:** {ev}    **Status:** {status}")
            if rationale:
                lines.append(f"- **Rationale:** {rationale}")
            lines.append("")
            lines.append("**Claim text:**")
            lines.append("")
            lines.append(f"> {claim}" if claim else "> _(empty)_")
            lines.append("")
            lines.append("**Research question:**")
            lines.append("")
            lines.append(f"> {rq}" if rq else "> _(empty)_")
            lines.append("")
            lines.append("- [ ] approve")
            lines.append("- [ ] reject")
            lines.append("")
            total_written += 1

    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text("\n".join(lines), encoding="utf-8")
    return total_written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_passes(arg: str) -> List[str]:
    parts = [p.strip().upper() for p in (arg or "").split(",") if p.strip()]
    valid = ["A", "B"]
    out = [p for p in parts if p in valid]
    if not out:
        raise SystemExit(f"--pass must be a comma-separated list of {valid}; got {arg!r}")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite path.")
    p.add_argument(
        "--manuscript", default=str(DEFAULT_MANUSCRIPT),
        help="Path to the .docx manuscript.",
    )
    p.add_argument(
        "--pass", dest="passes", default="A,B",
        help="Comma-separated list of passes to run (A,B). Default: A,B.",
    )
    p.add_argument(
        "--model", default="qwen3.6:35b-a3b-mlx-bf16",
        help="Primary LLM model (default qwen3.6:35b-a3b-mlx-bf16).",
    )
    p.add_argument(
        "--formatter-model", default="llama3.1:8b",
        help="Small fast model used for JSON repair on parse failures. "
             "Default llama3.1:8b. Set to '' to disable.",
    )
    p.add_argument(
        "--review-file", default=str(DEFAULT_REVIEW_FILE),
        help="Path to write the manual-review markdown file.",
    )
    args = p.parse_args()

    passes = _parse_passes(args.passes)

    db_path = Path(args.db)
    manuscript = Path(args.manuscript)
    review_path = Path(args.review_file)

    if not manuscript.exists():
        raise SystemExit(f"manuscript not found: {manuscript}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_gap_tree_schema(conn)

    settings = OrchestratorSettings.from_env()
    llm = make_llm_client(
        settings, model=args.model,
        timeout_seconds=900, temperature=0.1,
    )
    formatter_llm: Optional[Any] = None
    if args.formatter_model.strip():
        formatter_llm = make_llm_client(
            settings, model=args.formatter_model,
            timeout_seconds=120, temperature=0.0,
        )

    print(f"DB: {db_path}", flush=True)
    print(f"Manuscript: {manuscript}", flush=True)
    print(f"Passes: {','.join(passes)}", flush=True)
    print(f"Primary model: {args.model}", flush=True)
    print(f"Formatter model: {args.formatter_model or 'disabled'}", flush=True)
    print("", flush=True)

    summary: Dict[str, Dict[str, int]] = {}
    if "A" in passes:
        summary["A"] = _run_pass_a(
            conn=conn, manuscript=manuscript,
            llm=llm, formatter_llm=formatter_llm,
        )
    if "B" in passes:
        summary["B"] = _run_pass_b(conn=conn, manuscript=manuscript, llm=llm)

    # Write the manual-review markdown file.
    written = write_review_file(conn=conn, review_path=review_path, passes_run=passes)

    # Final summary.
    print("", flush=True)
    if "A" in summary:
        s = summary["A"]
        print(f"Pass A: {s['inserted']} new IP gaps inserted, "
              f"{s['skipped']} skipped (already exist)", flush=True)
    if "B" in summary:
        s = summary["B"]
        print(f"Pass B: {s['inserted']} new TODO gaps inserted, "
              f"{s['skipped']} skipped (already exist)", flush=True)
    print(f"Review file: {review_path}", flush=True)

    tier1_pending = conn.execute(
        "SELECT COUNT(*) FROM gap_tree WHERE tier = 1 AND status = 'pending'"
    ).fetchone()[0]
    print(f"Total tier-1 gaps awaiting approval: {tier1_pending}", flush=True)

    counts = count_by_pass(conn)
    print(f"DB pass counts: {counts}", flush=True)
    print(f"Review entries written: {written}", flush=True)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
