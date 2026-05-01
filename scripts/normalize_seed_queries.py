#!/usr/bin/env python3
"""Normalize EBSCO seed query strings to proper Boolean search syntax using an LLM.

Raw ``bquery`` values in seed JSON records were generated upstream and are
poorly formed for EBSCO's academic-database search syntax — multi-word
concepts are not quoted, synonyms are missing, and ``+`` characters are
literal punctuation rather than Boolean operators.  This script rewrites
each record's ``bquery`` into N distinct Boolean-query variants that each
target the same research gap from a different vocabulary / synonym angle,
and stores the result as a list in ``bquery_normalized`` (leaving the
original ``bquery`` intact for diff / rollback).

Multiple variants increase retrieval coverage: each variant targets the
same gap from a different vocabulary angle so more articles are surfaced
in aggregate than any single query would find.

The ``adapters/document_fetch.py`` consumer reads ``bquery_normalized`` and
issues a separate browser fetch for each variant, writing all results into
the same gap directory.

Usage:
    python scripts/normalize_seed_queries.py --run-id run_27f86e44394442
    python scripts/normalize_seed_queries.py --run-id run_abc --gap-id AUTO-01-G1
    python scripts/normalize_seed_queries.py --run-id run_abc --dry-run --limit 5
    python scripts/normalize_seed_queries.py --run-id run_abc --force
    python scripts/normalize_seed_queries.py --run-id run_abc --variants 5
    python scripts/normalize_seed_queries.py --run-id run_abc --model qwen2.5:14b

Environment variables (same as the rest of the pipeline):
    ORCH_LLM_PROVIDER   — ollama | claude | openai  (default: ollama)
    ORCH_LLM_MODEL      — model name               (default: qwen2.5:7b)
    ORCH_DATA_ROOT      — root of data/             (default: <repo>/data)
    ORCH_OLLAMA_BASE_URL — Ollama base URL          (default: http://127.0.0.1:11434)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Bootstrap: add project root to path so project modules are importable when
# the script is run directly (e.g. ``python scripts/normalize_seed_queries.py``).
# ---------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env before importing project modules so credentials / provider
# settings are available without the user having to export them manually.
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, v = _line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from config import OrchestratorSettings  # noqa: E402
from layers.llm_client import make_llm_client, LLMClient  # noqa: E402

# Default and maximum number of query variants to generate per record.
DEFAULT_VARIANTS = 3
MAX_VARIANTS     = 10

# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert in EBSCO academic-database search syntax. Your task is to
generate {n} DISTINCT Boolean search queries for the same research gap, each
targeting the gap from a different vocabulary or concept-combination angle so
that together they maximise recall in EBSCO's Academic Search Ultimate and
Business Source Ultimate databases.

Rules for each query:
1. Group synonyms / related terms with OR inside parentheses.
2. Quote multi-word phrases with double-quotes: "online retail".
3. Use Boolean AND (uppercase) to connect major concept groups.
4. Use trailing truncation * for common word stems where it helps recall:
   e.g. retail* matches retailer, retailers, retailing.
5. Remove bare punctuation like standalone + characters; use AND instead.
6. Avoid stop-words (the, a, an, of, in, …) at the top level.
7. Aim for 2–4 AND-connected concept groups per query.
8. Each query must use DIFFERENT vocabulary / angles — not minor rewrites.
   Vary by: synonyms, adjacent concepts, time-period framing, proper nouns
   vs generic terms, industry angle vs academic angle, etc.
9. Maximum ~200 characters per query.
10. Prefer recall over precision: we want articles to appear, not zero hits.

Output format: a numbered list, one query per line, no other text.
  1. <first query>
  2. <second query>
  ...

Examples of what "different angles" means for a gap about "Amazon's role in
transforming retail":

  DIRECT angle — core terms:
    ("Amazon" OR "Amazon.com") AND (retail* AND (transformation OR disruption))

  ADJACENT angle — competitor/market framing:
    "e-commerce" AND ("brick and mortar" OR "physical store") AND (decline OR shift)

  HISTORICAL angle — time-period vocabulary:
    ("online shopping" OR "internet retail") AND (history OR evolution OR 1990s OR 2000s)

Examples for a gap about "effects of algorithmic pricing on competition":

  DIRECT:
    "algorithmic pricing" AND (competition OR antitrust OR market power)

  MECHANISM angle — focusing on how:
    ("dynamic pricing" OR "automated pricing") AND (collusion OR coordination)

  SECTOR angle — applied to a domain:
    (airline* OR hotel* OR e-commerce) AND ("price algorithm*" OR "yield management") AND competit*
"""

# Pattern that strips a leading "1.", "1)", or "1:" style marker from a line.
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+[.):\s]+\s*")


def _normalize_queries(client: LLMClient, raw_bquery: str, n: int) -> List[str]:
    """Send *raw_bquery* to the LLM and return *n* normalized EBSCO query variants.

    Parses the LLM's numbered-list response robustly — handles ``1. q``,
    ``1) q``, ``1: q``, blank lines, and trailing whitespace.  Each variant
    is truncated to 200 chars (EBSCO's practical limit).  Empty / all-whitespace
    strings and exact duplicates are removed before returning.

    If fewer than min(2, n) variants survive validation, a warning is printed
    but whatever was parsed is returned rather than raising an exception.
    """
    prompt = _SYSTEM_PROMPT_TEMPLATE.format(n=n)
    response = client.complete(
        system=prompt,
        prompt=f"Generate {n} distinct search queries for this research gap:\n{raw_bquery}",
        temperature=0.3,  # slightly higher than single-query to encourage variation
    )

    variants = _parse_numbered_list(response, n)

    # Warn but don't fail if we got fewer than expected.
    min_expected = min(2, n)
    if len(variants) < min_expected:
        print(
            f"  [WARN] Expected at least {min_expected} variants, got {len(variants)} "
            f"from LLM for query: {raw_bquery!r}"
        )

    return variants


def _parse_numbered_list(response: str, max_items: int) -> List[str]:
    """Parse an LLM numbered-list response into a deduplicated list of strings.

    Handles all common numbering styles (``1.``, ``1)``, ``1:``) and removes
    blank lines.  Strips markdown code fences.  Deduplicates while preserving
    order.  Truncates each entry to 200 chars.
    """
    # Strip any surrounding markdown code fences.
    text = response.strip()
    for fence in ("```", "`"):
        if text.startswith(fence):
            text = text.lstrip("`").strip()
        if text.endswith(fence):
            text = text.rstrip("`").strip()

    seen: set[str] = set()
    variants: List[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue  # skip blank lines

        # Strip leading "1.", "2)", "3:" etc.
        line = _NUMBERED_LINE_RE.sub("", line).strip()

        if not line:
            continue  # line was only the number prefix

        # Truncate to EBSCO's practical limit.
        # FIXME(2026-05-01) — known bug: this truncates mid-clause for queries
        # >200 chars (gpt-oss:20b commonly produces 250-350 char queries).
        # Cuts leave dangling quotes / unclosed parens; EBSCO falls back to
        # keyword-soup matching and surfaces irrelevant articles. Observed
        # during the 2026-05-01 low-yield recovery: spinal-cord-stimulation
        # papers showing up under UPS / Amazon gaps.
        # NEXT FIX: combine prompt constraint ("MAX 250 chars") with a
        # Boolean-safe truncation helper that walks back to the last
        # balanced ')' or last AND/OR boundary. Also add a regression test
        # for known-too-long inputs. See task #20 in conversation history.
        entry = line[:200].strip()

        if not entry:
            continue

        # Deduplicate — skip exact-match repeats.
        if entry in seen:
            continue
        seen.add(entry)
        variants.append(entry)

        if len(variants) >= max_items:
            break

    return variants


# ---------------------------------------------------------------------------
# File walking
# ---------------------------------------------------------------------------

def _iter_seed_json_files(
    pull_outputs_root: Path,
    run_id: str,
    gap_id: Optional[str],
    source: str,
) -> List[Path]:
    """Return all seed JSON files to process for this run/gap/source."""
    run_dir = pull_outputs_root / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    json_files: List[Path] = []
    for gap_dir in sorted(run_dir.iterdir()):
        if not gap_dir.is_dir():
            continue
        if gap_id and gap_dir.name != gap_id:
            continue
        src_dir = gap_dir / source
        if not src_dir.is_dir():
            continue
        for jf in sorted(src_dir.glob("*.json")):
            json_files.append(jf)

    return json_files


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def _migrate_bquery_normalized(rec: dict) -> None:
    """Migrate a legacy string ``bquery_normalized`` to a single-element list.

    The previous version of this script stored ``bquery_normalized`` as a
    bare string.  If we encounter that, wrap it in a list so the rest of the
    code (and all consumers) see a consistent ``List[str]`` shape.  This is
    done in-place on *rec* and counts as a modification that needs saving.
    """
    existing = rec.get("bquery_normalized")
    if isinstance(existing, str) and existing.strip():
        rec["bquery_normalized"] = [existing.strip()]


def _process_file(
    json_path: Path,
    client: LLMClient,
    *,
    force: bool,
    dry_run: bool,
    limit_remaining: Optional[int],
    variants: int = DEFAULT_VARIANTS,
) -> int:
    """Normalize bquery fields in *json_path*, generating *variants* query variants.

    Returns the number of records actually normalized (0 if all skipped).
    Updates the file in-place (unless ``dry_run`` is True).

    Idempotency: skips records where ``bquery_normalized`` is already a
    non-empty list of length >= *variants*, unless *force* is True.

    Migration: if ``bquery_normalized`` is a bare string (written by the
    previous single-query version), it is wrapped as ``[str]`` before
    checking idempotency — so re-running will extend an old record to the
    requested number of variants.
    """
    try:
        raw = json_path.read_text(encoding="utf-8", errors="ignore")
        payload = json.loads(raw)
    except Exception as exc:
        print(f"  [WARN] Could not read {json_path}: {exc}")
        return 0

    records = payload if isinstance(payload, list) else [payload]
    modified = False
    count = 0

    for rec in records:
        if not isinstance(rec, dict):
            continue
        bquery = str(rec.get("bquery", "") or rec.get("query", "")).strip()
        if not bquery:
            # No bquery field — nothing to normalize.
            continue

        # Migrate old-style string value to list before any idempotency check.
        _migrate_bquery_normalized(rec)

        existing = rec.get("bquery_normalized")
        already_done = (
            isinstance(existing, list)
            and len(existing) >= variants
        )
        if already_done and not force:
            print(f"  [SKIP] {json_path.name} — bquery_normalized already has {len(existing)} variants")
            continue

        if limit_remaining is not None and limit_remaining <= 0:
            break

        print(f"  [IN ] {bquery!r}  (requesting {variants} variants)")

        if dry_run:
            # Simulate without writing — migration still counts so callers
            # know the record would have been touched.
            fake_list = [f'(DRY RUN variant {i+1} for: {bquery!r})' for i in range(variants)]
            print(f"  [OUT] {fake_list}")
            count += 1
            if limit_remaining is not None:
                limit_remaining -= 1
            continue

        try:
            variant_list = _normalize_queries(client, bquery, variants)
        except Exception as exc:
            print(f"  [ERR] LLM call failed for {json_path.name}: {exc}")
            continue

        if not variant_list:
            print(f"  [WARN] LLM returned no usable variants for {json_path.name}; skipping.")
            continue

        print(f"  [OUT] {variant_list}")
        # Preserve the original for rollback; store variant list.
        rec["bquery_original"] = bquery
        rec["bquery_normalized"] = variant_list
        modified = True
        count += 1
        if limit_remaining is not None:
            limit_remaining -= 1

    if modified and not dry_run:
        # Write back with the same structure (list or single object) to avoid
        # breaking downstream consumers that read the file.
        out_payload = payload  # already mutated in-place above
        json_path.write_text(
            json.dumps(out_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  [SAVE] {json_path}")

    return count


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Normalize EBSCO seed bquery strings to Boolean syntax via LLM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--run-id", required=True,
        help="Pull-output run ID directory name (e.g. run_27f86e44394442).",
    )
    p.add_argument(
        "--gap-id", default=None,
        help="Process only this gap (e.g. AUTO-01-G1). Omit to process all gaps.",
    )
    p.add_argument(
        "--source", default="ebsco_api",
        help="Source sub-directory name to process (default: ebsco_api).",
    )
    p.add_argument(
        "--variants", type=int, default=DEFAULT_VARIANTS,
        help=(
            f"Number of distinct query variants to generate per record "
            f"(default: {DEFAULT_VARIANTS}, max: {MAX_VARIANTS})."
        ),
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Stop after normalizing N records (useful for testing).",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Re-normalize even if bquery_normalized is already present.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show LLM input/output but do not write changes to disk.",
    )
    p.add_argument(
        "--model", default=None,
        help="Override LLM model name (e.g. qwen2.5:14b). Default: ORCH_LLM_MODEL.",
    )
    p.add_argument(
        "--data-root", default=None,
        help="Override data root directory (default: ORCH_DATA_ROOT or <repo>/data).",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point.  Returns 0 on success, non-zero on error."""
    args = _build_arg_parser().parse_args(argv)

    # Clamp --variants to [1, MAX_VARIANTS].
    variants = max(1, min(args.variants, MAX_VARIANTS))
    if variants != args.variants:
        print(f"[WARN] --variants clamped to {variants} (was {args.variants})")

    settings = OrchestratorSettings.from_env()
    data_root = Path(args.data_root) if args.data_root else settings.data_root
    pull_outputs_root = data_root / "pull_outputs"

    # Build LLM client reusing the project's existing provider plumbing.
    # ORCH_LLM_PROVIDER controls which backend is used (ollama / claude / openai).
    # Local Ollama is preferred (cheaper, no network cost for batch work).
    client = make_llm_client(settings, model=args.model or None, timeout_seconds=60)

    print(
        f"LLM provider : {settings.llm_provider}  "
        f"model: {client.model}"
    )
    print(
        f"Run          : {args.run_id}  "
        f"gap: {args.gap_id or '(all)'}  "
        f"source: {args.source}"
    )
    print(f"Variants     : {variants} per record")
    if args.dry_run:
        print("Mode         : DRY RUN (no files will be written)")
    if args.limit:
        print(f"Limit        : {args.limit} records")
    print()

    try:
        json_files = _iter_seed_json_files(
            pull_outputs_root, args.run_id, args.gap_id, args.source
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not json_files:
        print("No seed JSON files found — nothing to do.")
        return 0

    limit_remaining = args.limit  # mutable counter
    total_normalized = 0

    for jf in json_files:
        print(f"File: {jf.relative_to(pull_outputs_root)}")
        if limit_remaining is not None and limit_remaining <= 0:
            print("  [LIMIT] Reached --limit; stopping.")
            break
        n = _process_file(
            jf, client,
            force=args.force,
            dry_run=args.dry_run,
            limit_remaining=limit_remaining,
            variants=variants,
        )
        total_normalized += n
        if limit_remaining is not None:
            limit_remaining -= n

    print(f"\nDone. Records normalized: {total_normalized}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
