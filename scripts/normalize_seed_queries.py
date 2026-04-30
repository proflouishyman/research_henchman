#!/usr/bin/env python3
"""Normalize EBSCO seed query strings to proper Boolean search syntax using an LLM.

Raw ``bquery`` values in seed JSON records were generated upstream and are
poorly formed for EBSCO's academic-database search syntax — multi-word
concepts are not quoted, synonyms are missing, and ``+`` characters are
literal punctuation rather than Boolean operators.  This script rewrites
each record's ``bquery`` into a well-formed EBSCO Boolean query and stores
the result in a new ``bquery_normalized`` field, leaving the original
``bquery`` intact for diff / rollback.

The ``adapters/document_fetch.py`` consumer reads ``bquery_normalized`` in
preference to ``bquery`` when constructing EBSCO search URLs (see
``_splice_normalized_bquery``).

Usage:
    python scripts/normalize_seed_queries.py --run-id run_27f86e44394442
    python scripts/normalize_seed_queries.py --run-id run_abc --gap-id AUTO-01-G1
    python scripts/normalize_seed_queries.py --run-id run_abc --dry-run --limit 5
    python scripts/normalize_seed_queries.py --run-id run_abc --force
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


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert in EBSCO academic-database search syntax. Your task is to
rewrite a raw, poorly-formed query string into a well-structured Boolean
query that maximises recall in EBSCO's Academic Search Ultimate and Business
Source Ultimate databases.

Rules:
1. Group synonyms / related terms with OR inside parentheses.
2. Quote multi-word phrases with double-quotes: "online retail".
3. Use Boolean AND (uppercase) to connect major concept groups.
4. Use trailing truncation * for common word stems where it helps recall:
   e.g. retail* matches retailer, retailers, retailing.
5. Remove bare punctuation like standalone + characters; use AND instead.
6. Avoid stop-words (the, a, an, of, in, …) at the top level.
7. Aim for 2–4 AND-connected concept groups.
8. Output ONLY the final normalized query — no explanation, no prefix, no
   quotes around the whole query. Maximum ~200 characters.
9. Prefer recall over precision: we want articles to appear, not zero hits.

Examples (raw → normalized):

Amazon + e-commerce revolution archives
→ ("Amazon" OR "Amazon.com") AND ("e-commerce" OR "online retail") AND (history OR evolution OR revolution)

everything store definition
→ ("everything store" OR "Jeff Bezos") AND (Amazon OR retail OR commerce)

impact of e-commerce on retail
→ "e-commerce" AND retail* AND (impact OR effect OR transformation)

China retail market size 2019 online shopping
→ China AND ("e-commerce" OR "online shopping") AND (market* OR consumer*) AND (2019 OR 2020)

Jeff Bezos leadership management style Amazon
→ "Jeff Bezos" AND (leadership OR management OR strategy) AND Amazon
"""


def _normalize_query(client: LLMClient, raw_bquery: str) -> str:
    """Send *raw_bquery* to the LLM and return the normalized EBSCO query.

    The system prompt defines the transformation goal and provides worked
    examples so the model can generalise.  We ask the model to return only
    the rewritten query; we strip any accidental leading/trailing whitespace
    or markdown fences.
    """
    response = client.complete(
        system=_SYSTEM_PROMPT,
        prompt=f"Normalize this query:\n{raw_bquery}",
        temperature=0.1,
    )
    # Strip markdown code fences in case the model adds them despite instructions.
    normalized = response.strip()
    for fence in ("```", "`"):
        if normalized.startswith(fence):
            normalized = normalized.lstrip("`").strip()
        if normalized.endswith(fence):
            normalized = normalized.rstrip("`").strip()
    # Truncate to 200 chars to stay within EBSCO's practical limit.
    return normalized[:200].strip()


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

def _process_file(
    json_path: Path,
    client: LLMClient,
    *,
    force: bool,
    dry_run: bool,
    limit_remaining: Optional[int],
) -> int:
    """Normalize bquery fields in *json_path*.

    Returns the number of records actually normalized (0 if all skipped).
    Updates the file in-place (unless ``dry_run`` is True).
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

        already_done = bool(rec.get("bquery_normalized", "").strip())
        if already_done and not force:
            print(f"  [SKIP] {json_path.name} — bquery_normalized already set")
            continue

        if limit_remaining is not None and limit_remaining <= 0:
            break

        print(f"  [IN ] {bquery!r}")

        if dry_run:
            # Simulate what the LLM would do without writing anything.
            fake = f'(DRY RUN — would normalize: {bquery!r})'
            print(f"  [OUT] {fake}")
            count += 1
            if limit_remaining is not None:
                limit_remaining -= 1
            continue

        try:
            normalized = _normalize_query(client, bquery)
        except Exception as exc:
            print(f"  [ERR] LLM call failed for {json_path.name}: {exc}")
            continue

        print(f"  [OUT] {normalized!r}")
        # Preserve the original for rollback; add normalized alongside.
        rec["bquery_original"] = bquery
        rec["bquery_normalized"] = normalized
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
        )
        total_normalized += n
        if limit_remaining is not None:
            limit_remaining -= n

    print(f"\nDone. Records normalized: {total_normalized}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
