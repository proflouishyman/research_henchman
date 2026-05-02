#!/usr/bin/env python3
"""LLM-driven relevance scoring for the article corpus.

For every (gap × source) row in the article index, ask a local LLM:
  1. How relevant is this source to this gap? (0-3 score)
  2. WHY is it relevant? (1-2 sentence research-assistant explanation)

The pass populates three new columns on the ``articles`` table:
  - relevance_score INTEGER (0-3)
  - relevance_why   TEXT
  - scored_at       TEXT (ISO timestamp)

The scoring is gap-aware — the same article cited from a different
gap gets a fresh score because relevance is gap-specific. This is the
distinction between a foundational reading (high score in many gaps)
and an OCR false positive (zero score across most gaps).

Resume semantics: only rows where ``relevance_score IS NULL`` are
sent to the LLM. Re-running picks up exactly where the prior run
stopped, so it's safe to interrupt with Ctrl-C or kill.

Score scale (the LLM is shown this exact text):
  3 = directly addresses the gap; cite-worthy primary source
  2 = covers adjacent context; useful for setting/comparison
  1 = mentions topic but tangentially; light support
  0 = not relevant; search false positive (drop from dossier)

Usage:
  python3 scripts/score_relevance.py                    # score all unscored
  python3 scripts/score_relevance.py --gap AUTO-01-G1   # one gap only
  python3 scripts/score_relevance.py --limit 50         # just 50 rows
  python3 scripts/score_relevance.py --batch 8          # 8 sources/LLM call
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env for ORCH_*
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, v = _line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from config import OrchestratorSettings  # noqa: E402
from layers.llm_client import make_llm_client  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data/article_index.sqlite"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def ensure_columns(conn: sqlite3.Connection) -> None:
    """Add relevance columns if missing. Idempotent — safe to re-run."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "relevance_score" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN relevance_score INTEGER")
    if "relevance_why" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN relevance_why TEXT")
    if "scored_at" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN scored_at TEXT")
    # Index supports the resume query (`WHERE relevance_score IS NULL`)
    # and per-gap dossier sorting.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_relevance_score "
        "ON articles(relevance_score) WHERE relevance_score IS NOT NULL"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------


SCORE_SYSTEM_PROMPT = """\
You are a senior research assistant for a manuscript on the history of
e-commerce. You evaluate whether each source is relevant to a specific
research gap (a claim from the manuscript that needs supporting sources).

For EACH source, output:
  1. A score from 0 to 3:
       3 = directly addresses this gap; cite-worthy primary source
       2 = covers adjacent context; useful for setting or comparison
       1 = mentions the topic but tangential; light support only
       0 = not relevant; search false positive (e.g. unrelated trade
           journal, off-topic newspaper article, OCR coincidence)
  2. A WHY: one short, specific sentence explaining the relevance —
     reference the source's topic, period, or argument when you can.
     This is what a research assistant would say to the author when
     handing over the source. Be specific, not generic.

Output STRICT JSON: a list of objects, one per source, in the same order
as the input. Each object has keys "n" (the source number), "score"
(integer 0-3), and "why" (string, one sentence).

Example output:
[
  {"n": 1, "score": 3, "why": "Brad Stone's 2013 biography is the canonical narrative of Amazon's founding and Bezos's early strategy."},
  {"n": 2, "score": 1, "why": "Book review only — references the Stone biography but adds no original argument."},
  {"n": 3, "score": 0, "why": "1932 trade journal about tire manufacturing; OCR false positive on 'mail order'."}
]
"""


def build_user_prompt(gap_topic: str, gap_claim: str, batch: List[Dict[str, Any]]) -> str:
    """Format one batch of source records as the user-side LLM prompt.

    *batch* items are dicts with keys: n, title, authors, pub_date,
    source_id, abstract.
    """
    lines: List[str] = []
    lines.append(f"Manuscript chapter/topic: {gap_topic}")
    lines.append(f"Research gap (claim from the manuscript):")
    lines.append(f'  "{gap_claim.strip()}"')
    lines.append("")
    lines.append(f"Score these {len(batch)} candidate sources:")
    lines.append("")
    for item in batch:
        n = item["n"]
        title = (item.get("title") or "").strip()[:200]
        authors = (item.get("authors") or "").strip()[:100]
        pub_date = (item.get("pub_date") or "").strip()[:30]
        src = (item.get("source_id") or "").strip()
        abstract = (item.get("abstract") or "").strip()[:400]

        meta_bits = []
        if authors:  meta_bits.append(authors)
        if pub_date: meta_bits.append(pub_date)
        if src:      meta_bits.append(src)
        meta = " · ".join(meta_bits)

        lines.append(f"{n}. {title}")
        if meta:
            lines.append(f"   ({meta})")
        if abstract:
            lines.append(f"   abstract: {abstract}")
    lines.append("")
    lines.append("Respond with the JSON array only. No prose, no fences.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scoring loop
# ---------------------------------------------------------------------------


def fetch_gap_meta(conn: sqlite3.Connection, gap_id: str) -> Tuple[str, str]:
    """Return (gap_topic, gap_research_question) for a gap. Either may be empty."""
    row = conn.execute(
        "SELECT gap_topic, gap_research_question FROM articles "
        "WHERE gap_id = ? LIMIT 1",
        (gap_id,),
    ).fetchone()
    if not row:
        return "", ""
    return (row[0] or "").strip(), (row[1] or "").strip()


def fetch_unscored_for_gap(
    conn: sqlite3.Connection, gap_id: str, limit: Optional[int] = None,
) -> List[sqlite3.Row]:
    sql = (
        "SELECT id, title, authors, pub_date, source_id, abstract "
        "FROM articles WHERE gap_id = ? AND relevance_score IS NULL "
        "ORDER BY id ASC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (gap_id,)).fetchall()


JSON_REPAIR_SYSTEM_PROMPT = """\
You are a strict JSON formatter. The user will paste text that is supposed
to be a JSON array of objects. Some objects may have keys "n", "score",
and "why". Reformat the input as a clean JSON array exactly matching
this shape:

  [{"n": 1, "score": 0..3, "why": "one sentence"}, ...]

Rules:
- Output ONLY the JSON array. No prose, no fences, no commentary.
- Preserve every entry's "n", "score", and "why" — do not invent, drop,
  or merge entries.
- If a "score" is non-numeric, coerce to the nearest integer in 0..3.
- If a "why" is missing, leave it as an empty string.
- Quote every string. Escape inner quotes with backslash. Use double quotes.
"""


def repair_json_with_fallback(
    raw: str,
    formatter_llm: Optional[Any],
) -> Optional[List[Dict[str, Any]]]:
    """Try to coerce *raw* into a list-of-dicts JSON structure.

    1. json.loads as-is
    2. strip ``` fences and try again
    3. extract a [ ... ] fragment with regex and try
    4. send the whole raw string to *formatter_llm* with a strict-format
       system prompt and parse its output

    Returns the parsed list, or None if everything fails.
    """
    if not raw:
        return None

    def _try_parse(s: str) -> Optional[Any]:
        try:
            return json.loads(s)
        except Exception:
            return None

    # Step 1: as-is
    parsed = _try_parse(raw.strip())
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for k in ("scores", "results", "items", "data"):
            if isinstance(parsed.get(k), list):
                return parsed[k]

    # Step 2: strip ``` fences
    s = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    parsed = _try_parse(s.strip())
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for k in ("scores", "results", "items", "data"):
            if isinstance(parsed.get(k), list):
                return parsed[k]

    # Step 3: extract first [ ... ] fragment
    m = re.search(r"\[\s*[\{\[].*[\}\]]\s*\]", raw, re.DOTALL)
    if m:
        parsed = _try_parse(m.group(0))
        if isinstance(parsed, list):
            return parsed

    # Step 4: ask the small, fast formatter model to clean it up
    if formatter_llm is not None:
        try:
            cleaned = formatter_llm.complete(
                system=JSON_REPAIR_SYSTEM_PROMPT,
                prompt=f"Reformat this as the strict JSON array described:\n\n{raw[:6000]}",
                temperature=0.0,
            )
            cleaned = cleaned.strip()
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()
            parsed = _try_parse(cleaned)
            if isinstance(parsed, list):
                return parsed
            # Maybe the formatter returned an object wrapping the list
            if isinstance(parsed, dict):
                for k in ("scores", "results", "items", "data"):
                    if isinstance(parsed.get(k), list):
                        return parsed[k]
            # Last-ditch: extract first [...] from formatter's output
            m2 = re.search(r"\[\s*[\{\[].*[\}\]]\s*\]", cleaned, re.DOTALL)
            if m2:
                parsed = _try_parse(m2.group(0))
                if isinstance(parsed, list):
                    return parsed
        except Exception as exc:
            print(f"    [warn] formatter LLM failed: {exc!s:.100}", flush=True)
    return None


def score_batch(
    llm: Any,
    gap_topic: str,
    gap_claim: str,
    batch_rows: List[sqlite3.Row],
    formatter_llm: Optional[Any] = None,
) -> Dict[int, Tuple[int, str]]:
    """Send *batch_rows* to the LLM and return {row_id: (score, why)}.

    On primary model JSON-parse failure, falls back to *formatter_llm*
    (typically a small fast model like llama3.1:8b) for JSON repair —
    decouples primary-model quality from output-formatting reliability.
    Returns empty dict if both layers fail; the row stays NULL and the
    next run retries.
    """
    items = []
    id_for_n: Dict[int, int] = {}
    for n, row in enumerate(batch_rows, 1):
        items.append({
            "n":         n,
            "title":     row["title"],
            "authors":   row["authors"],
            "pub_date":  row["pub_date"],
            "source_id": row["source_id"],
            "abstract":  row["abstract"],
        })
        id_for_n[n] = row["id"]

    user_prompt = build_user_prompt(gap_topic, gap_claim, items)

    # Step 1: primary model with its own complete_json (fast path).
    parsed: Optional[Any] = None
    raw_response: Optional[str] = None
    try:
        parsed = llm.complete_json(
            system=SCORE_SYSTEM_PROMPT,
            prompt=user_prompt,
            temperature=0.1,
        )
    except Exception:
        # Fall through to raw-and-repair path. complete_json's failure
        # may have been a transient network/parse issue — try the raw
        # response and route through the repair fallback.
        try:
            raw_response = llm.complete(
                system=SCORE_SYSTEM_PROMPT,
                prompt=user_prompt,
                temperature=0.1,
            )
        except Exception as exc:
            print(f"    [warn] primary LLM call failed: {exc!s:.100}", flush=True)
            return {}

    # Step 2: if parsed is unusable, run the raw text through the repair fallback.
    if not isinstance(parsed, list):
        if isinstance(parsed, dict):
            # complete_json returned an unusable dict — re-fetch raw to repair.
            for k in ("scores", "results", "items", "data"):
                if isinstance(parsed.get(k), list):
                    parsed = parsed[k]; break
        if not isinstance(parsed, list):
            if raw_response is None:
                try:
                    raw_response = llm.complete(
                        system=SCORE_SYSTEM_PROMPT,
                        prompt=user_prompt,
                        temperature=0.1,
                    )
                except Exception:
                    raw_response = ""
            parsed = repair_json_with_fallback(raw_response or "", formatter_llm) or []
            if parsed:
                print(f"    [info] JSON repaired via formatter fallback "
                      f"({len(parsed)} entries)", flush=True)

    if not isinstance(parsed, list) or not parsed:
        print(f"    [warn] LLM batch failed (no parseable output)", flush=True)
        return {}

    out: Dict[int, Tuple[int, str]] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        n = entry.get("n") or entry.get("number") or entry.get("id")
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if n not in id_for_n:
            continue
        score = entry.get("score")
        try:
            score = int(score)
        except (TypeError, ValueError):
            continue
        if score < 0 or score > 3:
            continue
        why = (entry.get("why") or entry.get("reason") or "").strip()
        # Trim runaway whys; one sentence is the contract
        why = re.sub(r"\s+", " ", why)[:500]
        out[id_for_n[n]] = (score, why)
    return out


def write_scores(conn: sqlite3.Connection, scores: Dict[int, Tuple[int, str]]) -> int:
    """Write {row_id: (score, why)} to the DB. Returns rows updated."""
    if not scores:
        return 0
    now = datetime.utcnow().isoformat(timespec="seconds")
    rows = [(s, w, now, rid) for rid, (s, w) in scores.items()]
    conn.executemany(
        "UPDATE articles SET relevance_score=?, relevance_why=?, scored_at=? "
        "WHERE id=?",
        rows,
    )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite path.")
    p.add_argument("--gap", default=None,
                   help="Score only this gap_id. Default: every gap with unscored rows.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap rows per gap (for testing). Default: all unscored rows.")
    p.add_argument("--batch", type=int, default=8,
                   help="Sources per LLM call (default 8). "
                        "Higher = fewer calls but riskier parsing.")
    p.add_argument("--model", default="llama3.3:latest",
                   help="Primary LLM model used for scoring + WHY explanations. "
                        "Default llama3.3:latest (Meta 70B) — strongest local "
                        "reasoning available; slower but more accurate WHYs.")
    p.add_argument("--formatter-model", default="llama3.1:8b",
                   help="Small fast model used to repair primary model's "
                        "JSON output if it fails strict parsing. "
                        "Default llama3.1:8b. Set to '' to disable repair.")
    p.add_argument("--max-gaps", type=int, default=None,
                   help="Process at most this many gaps (for staged runs).")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_columns(conn)

    settings = OrchestratorSettings.from_env()
    # Primary model — generous timeout because llama3.3:70b's first call
    # loads the 42 GB model into memory (one-time ~60 s warm-up).
    llm = make_llm_client(settings, model=args.model,
                           timeout_seconds=900, temperature=0.1)
    # Formatter model — small, fast, called only on JSON-parse failures.
    formatter_llm = None
    if args.formatter_model.strip():
        formatter_llm = make_llm_client(
            settings, model=args.formatter_model,
            timeout_seconds=120, temperature=0.0,
        )

    # Discover work — gaps with at least one unscored row.
    if args.gap:
        gap_ids = [args.gap]
    else:
        gap_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT gap_id FROM articles "
            "WHERE relevance_score IS NULL ORDER BY gap_id"
        ).fetchall()]
    if args.max_gaps:
        gap_ids = gap_ids[:args.max_gaps]

    total_unscored = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE relevance_score IS NULL"
    ).fetchone()[0]
    total_scored = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE relevance_score IS NOT NULL"
    ).fetchone()[0]
    print(f"DB: {args.db}", flush=True)
    print(f"  already scored:  {total_scored}", flush=True)
    print(f"  unscored:        {total_unscored}", flush=True)
    print(f"  gaps to process: {len(gap_ids)} (model={args.model}, "
          f"formatter={args.formatter_model or 'disabled'}, batch={args.batch})", flush=True)
    print(flush=True)

    overall_scored = 0
    overall_failed = 0
    t_start = time.time()

    for gi, gap_id in enumerate(gap_ids, 1):
        gap_topic, gap_claim = fetch_gap_meta(conn, gap_id)
        if not gap_claim:
            print(f"[{gi}/{len(gap_ids)}] {gap_id}: no claim text in DB — skip", flush=True)
            continue
        unscored = fetch_unscored_for_gap(conn, gap_id, args.limit)
        if not unscored:
            continue

        print(f"[{gi}/{len(gap_ids)}] {gap_id} ({len(unscored)} rows) topic={gap_topic[:50]}", flush=True)

        gap_scored = 0
        gap_failed = 0
        for i in range(0, len(unscored), args.batch):
            batch = unscored[i : i + args.batch]
            scores = score_batch(llm, gap_topic, gap_claim, batch,
                                  formatter_llm=formatter_llm)
            n_written = write_scores(conn, scores)
            gap_scored += n_written
            gap_failed += (len(batch) - n_written)

        overall_scored += gap_scored
        overall_failed += gap_failed
        elapsed = time.time() - t_start
        rate = overall_scored / max(elapsed, 1)
        print(f"  ✓ {gap_scored}/{len(unscored)} scored "
              f"(overall: {overall_scored} scored, {overall_failed} parse-failed; "
              f"{rate:.1f} rows/s; elapsed {elapsed/60:.1f} min)", flush=True)

    elapsed = time.time() - t_start
    print(f"\n=== summary ===", flush=True)
    print(f"  rows scored this run: {overall_scored}", flush=True)
    print(f"  rows parse-failed:    {overall_failed} (will retry on next run)", flush=True)
    print(f"  elapsed:              {elapsed/60:.1f} min", flush=True)

    final_remaining = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE relevance_score IS NULL"
    ).fetchone()[0]
    print(f"  rows still unscored:  {final_remaining}", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
