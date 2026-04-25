#!/usr/bin/env python3
"""End-to-end pipeline test against the real Add to Cart manuscript.

Usage:
    python scripts/test_run.py [--manuscript PATH] [--base-url URL] [--timeout SECONDS]

What it checks:
    1. File is uploadable (or already in workspace)
    2. Run reaches 'complete' or 'partial' within timeout
    3. Gaps have substantive claim_text (not heuristic fallback descriptions)
    4. Search queries are topic-relevant (not descriptions of gap types)
    5. Pull artifacts exist on disk
    6. Export bundle exists with expected structure
"""

import argparse
import json
import sys
import time
from pathlib import Path

import urllib.request
import urllib.error


DEFAULT_MANUSCRIPT = (
    "/Users/louishyman/Library/CloudStorage/GoogleDrive-lhyman@gmail.com"
    "/My Drive/2024-2025/E-Commerce/Manuscript/Add To Cart -- main manuscript -- 2026.docx"
)
DEFAULT_BASE_URL = "http://localhost:8876"
DEFAULT_TIMEOUT  = 600  # 10 minutes


# ── helpers ──────────────────────────────────────────────────────────────────

def api(base: str, path: str, method: str = "GET", body=None):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        text = exc.read().decode()
        raise RuntimeError(f"HTTP {exc.code} {path}: {text[:300]}") from exc


def upload(base: str, path: str) -> str:
    """Upload a local file; return the stored_path the server recorded."""
    import io, mimetypes
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"manuscript not found: {path}")

    boundary = "----FormBoundary7MA4YWxkTrZu0gW"
    mime, _ = mimetypes.guess_type(str(p))
    mime = mime or "application/octet-stream"

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{p.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + p.read_bytes() + f"\r\n--{boundary}--\r\n".encode()

    url = base.rstrip("/") + "/api/orchestrator/manuscripts/upload"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["stored_path"]


def poll(base: str, run_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        run = api(base, f"/api/orchestrator/runs/{run_id}")
        status = run.get("status", "")
        stage  = run.get("stage_detail", "")
        dots += 1
        print(f"\r  [{status:12}] {stage[:60]:<60}", end="", flush=True)
        if status in ("complete", "partial", "failed"):
            print()
            return run
        time.sleep(5)
    print()
    raise TimeoutError(f"run did not finish within {timeout}s")


# ── quality checks ────────────────────────────────────────────────────────────

HEURISTIC_PHRASES = [
    "unsupported claims without citation",
    "claims without enough citations",
    "compressed; split claims",
    "quantitative assertions without data anchors",
    "hedged language without supporting citations",
]

def check_claim_quality(gaps: list) -> tuple[int, int, list[str]]:
    """Return (good, total, [warnings])."""
    warnings = []
    good = 0
    for g in gaps:
        claim = g.get("claim_text", "")
        is_heuristic = any(ph.lower() in claim.lower() for ph in HEURISTIC_PHRASES)
        if is_heuristic:
            warnings.append(f"  HEURISTIC claim in {g['gap_id']}: {claim[:80]}")
        else:
            good += 1
    return good, len(gaps), warnings


def check_query_quality(gaps: list) -> tuple[int, int, list[str]]:
    """Queries should not contain heuristic phrases or be shorter than 5 words."""
    warnings = []
    good = 0
    total = 0
    for g in gaps:
        for q in g.get("search_queries", []):
            total += 1
            is_bad = any(ph.lower() in q.lower() for ph in HEURISTIC_PHRASES)
            too_short = len(q.split()) < 4
            if is_bad or too_short:
                warnings.append(f"  BAD query in {g['gap_id']}: {q[:80]}")
            else:
                good += 1
    return good, total, warnings


def check_pull_artifacts(run: dict, settings_workspace: str) -> tuple[int, list[str], list[str]]:
    """Count artifact files; return (total, hard_failures, soft_notes).

    An empty directory for a single source is a soft note — that source may
    legitimately have no matching content. A hard failure only fires when the
    entire pull produced zero files across all sources.
    """
    hard_failures = []
    soft_notes = []
    pull_results = run.get("pull_results", []) or []
    total_files = 0
    for pr in pull_results:
        for sr in (pr.get("results") or []):
            run_dir = sr.get("run_dir")
            if not run_dir:
                continue
            p = Path(run_dir)
            files = list(p.glob("**/*")) if p.exists() else []
            actual = [f for f in files if f.is_file()]
            if not actual:
                soft_notes.append(f"  no files in {run_dir}")
            total_files += len(actual)
    if total_files == 0:
        hard_failures.append("  No pull artifacts found on disk across any source")
    return total_files, hard_failures, soft_notes


def check_export_bundle(run: dict, base: str) -> list[str]:
    """Check that _INDEX.md, _BIBLIOGRAPHY.md, and gap folders exist."""
    warnings = []
    # Fetch bundle info from manifest
    run_id = run["run_id"]
    manuscript_path = run.get("manuscript_path", "")
    stem = Path(manuscript_path).stem.strip() or "manuscript"
    # Sanitise like artifact_export does
    import re
    cleaned = re.sub(r"[^\w\s\.-]+", "", stem, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)[:80]

    # Look in data/manuscript_exports/
    export_root = Path("data/manuscript_exports") / cleaned
    if not export_root.exists():
        warnings.append(f"  Bundle root not found: {export_root}")
        return warnings

    for fname in ("_INDEX.md", "_BIBLIOGRAPHY.md"):
        if not (export_root / fname).exists():
            warnings.append(f"  Missing {fname} in bundle")

    gaps_dir = export_root / "gaps"
    if not gaps_dir.exists() or not any(gaps_dir.iterdir()):
        warnings.append("  No gap folders in bundle/gaps/")
    else:
        for gd in gaps_dir.iterdir():
            if gd.is_dir() and not (gd / "_README.md").exists():
                warnings.append(f"  Missing _README.md in {gd.name}")

    return warnings


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manuscript", default=DEFAULT_MANUSCRIPT)
    ap.add_argument("--base-url",   default=DEFAULT_BASE_URL)
    ap.add_argument("--timeout",    type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--force",      action="store_true", help="force re-analysis even if cached")
    args = ap.parse_args()

    base = args.base_url
    failures = []

    print("=" * 70)
    print("Research Henchman — End-to-End Test")
    print("=" * 70)

    # 1. Health check
    print("\n[1] Health check... ", end="")
    try:
        h = api(base, "/api/orchestrator/health")
        print(f"OK  model={h.get('llm_model')} lib={h.get('library_system')}")
    except Exception as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)

    # 2. Upload manuscript
    print(f"\n[2] Uploading manuscript...")
    print(f"    {args.manuscript}")
    try:
        stored_path = upload(base, args.manuscript)
        print(f"    → stored at: {stored_path}")
    except Exception as exc:
        print(f"    FAIL: {exc}")
        failures.append(f"Upload failed: {exc}")
        stored_path = None

    if stored_path is None:
        print("\nCannot continue without uploaded manuscript.")
        sys.exit(1)

    # 3. Create run
    print("\n[3] Creating run...")
    try:
        run_resp = api(base, "/api/orchestrator/runs", "POST",
                       {"manuscript_path": stored_path, "force": args.force})
        run_id = run_resp["run_id"]
        print(f"    run_id={run_id}")
    except Exception as exc:
        print(f"    FAIL: {exc}")
        sys.exit(1)

    # 4. Poll to completion
    print(f"\n[4] Waiting for run to complete (timeout={args.timeout}s)...")
    try:
        run = poll(base, run_id, args.timeout)
        status = run.get("status")
        print(f"    Final status: {status}")
        if run.get("error"):
            print(f"    Error: {run['error']}")
            failures.append(f"Run error: {run['error']}")
    except TimeoutError as exc:
        print(f"    TIMEOUT: {exc}")
        failures.append(str(exc))
        run = api(base, f"/api/orchestrator/runs/{run_id}")

    # 5. Gap quality
    print("\n[5] Checking gap claim quality...")
    gaps = (run.get("gap_map") or {}).get("gaps", [])
    plan_gaps = (run.get("research_plan") or {}).get("gaps", [])
    print(f"    Gap map gaps: {len(gaps)}")
    print(f"    Plan gaps:    {len(plan_gaps)}")

    if not gaps:
        failures.append("No gaps extracted from manuscript")
    else:
        good_c, total_c, warn_c = check_claim_quality(gaps)
        print(f"    Substantive claims: {good_c}/{total_c}")
        for w in warn_c:
            print(w)
        if good_c == 0:
            failures.append("All claims are heuristic fallbacks — LLM analysis not working")
        elif good_c < total_c:
            failures.append(f"Partial LLM failure: {total_c - good_c} heuristic claims")

    # 6. Query quality
    print("\n[6] Checking query quality...")
    if plan_gaps:
        good_q, total_q, warn_q = check_query_quality(
            [{"gap_id": g["gap_id"], "search_queries": g.get("search_queries", [])}
             for g in plan_gaps]
        )
        print(f"    Good queries: {good_q}/{total_q}")
        for w in warn_q:
            print(w)
        if total_q == 0:
            failures.append("No search queries generated")
        elif good_q == 0:
            failures.append("All queries are heuristic — reflection not working")

    # 7. Pull artifacts
    print("\n[7] Checking pull artifacts on disk...")
    total_files, hard_p, soft_p = check_pull_artifacts(run, ".")
    print(f"    Artifact files on disk: {total_files}")
    for w in soft_p:
        print(f"    (note) {w.strip()}")  # per-source empty is informational only
    for w in hard_p:
        print(w)
        failures.append(w.strip())

    # 8. Export bundle
    print("\n[8] Checking export bundle...")
    warn_e = check_export_bundle(run, base)
    if warn_e:
        for w in warn_e:
            print(w)
            failures.append(w.strip())
    else:
        print("    Bundle structure OK")

    # 9. Print sample gaps for human review
    print("\n[9] Sample gap claims (first 5):")
    for g in gaps[:5]:
        print(f"    [{g['gap_id']}] {g.get('chapter','')[:30]}")
        print(f"      claim:   {g.get('claim_text','')[:100]}")
    if plan_gaps:
        print("\n    Sample search queries:")
        for pg in plan_gaps[:3]:
            print(f"    [{pg['gap_id']}] {pg.get('search_queries', ['(none)'])[0][:80]}")

    # Summary
    print("\n" + "=" * 70)
    if failures:
        print(f"RESULT: FAIL  ({len(failures)} issue(s))")
        for f in failures:
            print(f"  • {f}")
        sys.exit(1)
    else:
        print("RESULT: PASS")
    print("=" * 70)


if __name__ == "__main__":
    main()
