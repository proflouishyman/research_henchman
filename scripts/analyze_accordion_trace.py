#!/usr/bin/env python3
"""Analyze accordion pull traces across ladder runs.

Reads app/data/events.json and app/data/runs.json and prints:
- ladder runs detected from query_ladder presence
- per gap×source attempt traces
- aggregate accordion move statistics
- route confidence distribution for ladder runs

Usage:
    python app/scripts/analyze_accordion_trace.py --data-root app/data
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ACCORDION_ACTIONS = {"lateral", "widen", "tighten", "accept", "exhausted"}
ACTIVE_PULL_EVENTS = ACCORDION_ACTIONS | {"progress", "warning"}


def load_json(path: Path) -> Any:
    """Read JSON payload from disk, returning empty shape on missing file."""

    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_runs(rows: Any) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Yield `(run_id, run_record)` for both dict and list run stores."""

    if isinstance(rows, dict):
        for run_id, run in rows.items():
            if isinstance(run, dict):
                rid = str(run.get("run_id", run_id))
                yield rid, run
        return
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                run_id = str(row.get("run_id", "")).strip()
                if run_id:
                    yield run_id, row


def is_ladder_run(run: Dict[str, Any]) -> bool:
    """Identify runs that used accordion ladder planning."""

    plan = run.get("research_plan") or {}
    gaps = plan.get("gaps") or []
    return any(bool((gap or {}).get("query_ladder")) for gap in gaps if isinstance(gap, dict))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default="app/data")
    args = parser.parse_args()

    root = Path(args.data_root)
    runs_raw = load_json(root / "runs.json")
    events_raw = load_json(root / "events.json")

    run_map = {run_id: run for run_id, run in _iter_runs(runs_raw)}
    all_events: List[Dict[str, Any]] = events_raw if isinstance(events_raw, list) else []

    ladder_run_ids = {
        run_id for run_id, run in run_map.items() if isinstance(run, dict) and is_ladder_run(run)
    }

    print(f"\nLadder runs found: {len(ladder_run_ids)}")
    for run_id in sorted(ladder_run_ids):
        run = run_map.get(run_id, {})
        status = run.get("status", "?")
        manuscript = run.get("manuscript_path", "?")
        print(f"  {run_id}  status={status}  manuscript={manuscript}")

    traces: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )

    for event in all_events:
        if not isinstance(event, dict):
            continue
        run_id = str(event.get("run_id", "")).strip()
        if run_id not in ladder_run_ids:
            continue
        if str(event.get("stage", "")).strip() != "pulling":
            continue
        action = str(event.get("status", "")).strip()
        if action not in ACTIVE_PULL_EVENTS:
            continue
        meta = event.get("meta") or {}
        if not isinstance(meta, dict):
            continue
        gap_id = str(meta.get("gap_id", "")).strip()
        source_id = str(meta.get("source_id", "")).strip()
        if not gap_id or not source_id:
            continue
        traces[run_id][gap_id][source_id].append(
            {
                "action": action,
                "rung": str(meta.get("rung", "")),
                "synonym_idx": meta.get("synonym_idx", ""),
                "doc_count": meta.get("doc_count", ""),
                "query": str(meta.get("query", ""))[:60],
                "ts": str(event.get("ts_utc", "")),
            }
        )

    print("\n--- Per-gap×source attempt traces ---")
    accept_rungs: List[str] = []
    attempts_per_gap_source: List[int] = []
    doc_counts_at_accept: List[int] = []
    lateral_fired = 0
    widen_fired = 0
    exhausted_count = 0

    for run_id in sorted(ladder_run_ids):
        print(f"\nRun: {run_id}")
        for gap_id, sources in sorted(traces[run_id].items()):
            for source_id, attempts in sorted(sources.items()):
                attempt_summary = " → ".join(
                    f"{row['action']}@{row['rung']}[syn={row['synonym_idx']}](docs={row['doc_count']})"
                    for row in attempts
                )
                final = attempts[-1] if attempts else {}
                final_action = final.get("action", "?")
                final_docs = final.get("doc_count", "?")
                final_rung = final.get("rung", "?")

                print(f"  {gap_id} × {source_id}")
                print(f"    attempts={len(attempts)} final={final_action}@{final_rung} docs={final_docs}")
                print(f"    trace: {attempt_summary}")

                attempts_per_gap_source.append(len(attempts))
                if final_action == "accept":
                    accept_rungs.append(str(final_rung))
                    try:
                        doc_counts_at_accept.append(int(final_docs))
                    except (TypeError, ValueError):
                        pass
                elif final_action == "exhausted":
                    exhausted_count += 1

                for row in attempts:
                    if row["action"] == "lateral":
                        lateral_fired += 1
                    elif row["action"] == "widen":
                        widen_fired += 1

    print("\n--- Aggregate statistics ---")
    total_gap_sources = sum(len(source_rows) for run_rows in traces.values() for source_rows in run_rows.values())
    print(f"Total gap×source traces:  {total_gap_sources}")
    print(f"Exhausted (no result):    {exhausted_count}")
    print(f"Lateral moves fired:      {lateral_fired}")
    print(f"Widen moves fired:        {widen_fired}")

    if accept_rungs:
        print(f"\nAccept rung distribution: {dict(Counter(accept_rungs))}")

    if doc_counts_at_accept:
        doc_counts_at_accept.sort()
        n = len(doc_counts_at_accept)
        print("\nDoc count at accept:")
        print(
            f"  min={doc_counts_at_accept[0]}  "
            f"p25={doc_counts_at_accept[n // 4]}  "
            f"median={doc_counts_at_accept[n // 2]}  "
            f"p75={doc_counts_at_accept[(3 * n) // 4]}  "
            f"max={doc_counts_at_accept[-1]}"
        )

    if attempts_per_gap_source:
        attempts_per_gap_source.sort()
        n = len(attempts_per_gap_source)
        print("\nAttempts per gap×source:")
        print(
            f"  min={attempts_per_gap_source[0]}  "
            f"median={attempts_per_gap_source[n // 2]}  "
            f"max={attempts_per_gap_source[-1]}"
        )

    print("\n--- Route confidence distribution (ladder runs) ---")
    confidences: List[float] = []
    for run_id in ladder_run_ids:
        run = run_map.get(run_id, {})
        plan = run.get("research_plan") or {}
        gaps = plan.get("gaps") or []
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            value = gap.get("route_confidence")
            if value is None:
                continue
            try:
                confidences.append(float(value))
            except (TypeError, ValueError):
                continue
    if confidences:
        confidences.sort()
        n = len(confidences)
        print(
            f"  n={n}  min={confidences[0]:.3f}  "
            f"p10={confidences[max(0, n // 10)]:.3f}  "
            f"p50={confidences[n // 2]:.3f}  "
            f"p90={confidences[min(n - 1, (9 * n) // 10)]:.3f}  "
            f"max={confidences[-1]:.3f}"
        )


if __name__ == "__main__":
    main()
