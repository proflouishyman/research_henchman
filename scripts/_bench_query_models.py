#!/usr/bin/env python3
"""One-off benchmark: run normalize_seed_queries' LLM prompt against multiple
local Ollama models on a small set of low-yield gaps. Generates a markdown
report comparing original bquery → 3 variants per model + timing.

NOT a production tool — written for the 2026-05-01 model-selection experiment.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so settings.from_env() picks up everything as the real script does.
env_path = ROOT / ".env"
if env_path.exists():
    import os as _os
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            _os.environ.setdefault(k.strip(), v.strip())

from config import OrchestratorSettings  # noqa: E402
from layers.llm_client import make_llm_client  # noqa: E402
from scripts.normalize_seed_queries import _SYSTEM_PROMPT_TEMPLATE, _parse_numbered_list  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GAPS = [
    "AUTO-103-G1",
    "AUTO-111-G1",
    "AUTO-119-G1",
    "AUTO-120-G1",
    "AUTO-126-G1",
]

MODELS = [
    "qwen2.5:7b",       # current default — small + fast
    "llama3.1:8b",      # similar size class, different architecture
    "gpt-oss:20b",      # medium
    "qwen3.5:27b",      # medium-large, newer family
    "llama3.3:latest",  # ~70B — largest local, will be slow
]

VARIANTS_N = 3
DATA_ROOT = ROOT / "data" / "pull_outputs" / "run_27f86e44394442"
ONE_RECORD_PER_GAP = True   # to keep total wall-clock manageable


def _gap_seed_records(gap_id: str) -> list[dict]:
    """Return all bquery-bearing records under <gap>/ebsco_api/."""
    out = []
    src_dir = DATA_ROOT / gap_id / "ebsco_api"
    if not src_dir.exists():
        return out
    for f in sorted(src_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            data = [data]
        for rec in data:
            if isinstance(rec, dict) and rec.get("url"):
                # bquery is embedded in the URL as ?bquery=…
                import urllib.parse
                qs = urllib.parse.urlparse(rec["url"]).query
                bquery = (urllib.parse.parse_qs(qs).get("bquery") or [""])[0]
                if bquery:
                    out.append({"file": f.name, "bquery": bquery, "title": rec.get("title", "")})
    return out


def _bench_one(model: str, bquery: str) -> dict:
    """Send one bquery through one model; return parsed variants + timing."""
    settings = OrchestratorSettings.from_env()
    client = make_llm_client(settings, model=model, timeout_seconds=120, temperature=0.3)
    prompt_sys = _SYSTEM_PROMPT_TEMPLATE.format(n=VARIANTS_N)
    user_msg = f"Generate {VARIANTS_N} distinct search queries for this research gap:\n{bquery}"

    t0 = time.monotonic()
    try:
        response = client.complete(system=prompt_sys, prompt=user_msg, temperature=0.3)
        elapsed = time.monotonic() - t0
        variants = _parse_numbered_list(response, VARIANTS_N)
        return {"variants": variants, "raw": response, "elapsed_s": elapsed, "error": None}
    except Exception as exc:
        elapsed = time.monotonic() - t0
        return {"variants": [], "raw": "", "elapsed_s": elapsed, "error": str(exc)[:200]}


def main() -> None:
    rows = []  # (gap_id, file, original_bquery, model, elapsed, variants, error)
    print(f"Running {len(MODELS)} models × {len(GAPS)} gaps × 1 record/gap = "
          f"{len(MODELS) * len(GAPS)} LLM calls")
    print()

    for gap_id in GAPS:
        records = _gap_seed_records(gap_id)
        if ONE_RECORD_PER_GAP:
            records = records[:1]
        for rec in records:
            print(f"=== {gap_id} :: {rec['file']} ===")
            print(f"    bquery: {rec['bquery'][:80]}")
            for model in MODELS:
                print(f"    [{model}] ...", end="", flush=True)
                result = _bench_one(model, rec["bquery"])
                print(f" {result['elapsed_s']:.1f}s "
                      f"({len(result['variants'])} variants"
                      f"{', err: ' + result['error'][:30] if result['error'] else ''})")
                rows.append({
                    "gap_id": gap_id,
                    "file": rec["file"],
                    "bquery_original": rec["bquery"],
                    "model": model,
                    "elapsed_s": result["elapsed_s"],
                    "variants": result["variants"],
                    "raw": result["raw"],
                    "error": result["error"],
                })
            print()

    # ---- write report ----
    report_path = ROOT / "logs" / "model_bench_report.md"
    lines = ["# LLM model comparison — query normalization", ""]
    lines.append(f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Records benchmarked:** {len(GAPS)} gaps × 1 record each")
    lines.append(f"**Variants requested per call:** {VARIANTS_N}")
    lines.append(f"**Temperature:** 0.3")
    lines.append("")

    # Timing summary
    lines.append("## Timing summary (mean seconds per call)")
    lines.append("")
    lines.append("| Model | Mean | Median | Min | Max |")
    lines.append("|---|---|---|---|---|")
    from statistics import mean, median
    by_model: dict[str, list[float]] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r["elapsed_s"])
    for m in MODELS:
        ts = by_model.get(m, [])
        if not ts:
            lines.append(f"| {m} | — | — | — | — |")
            continue
        lines.append(f"| `{m}` | {mean(ts):.1f}s | {median(ts):.1f}s | {min(ts):.1f}s | {max(ts):.1f}s |")
    lines.append("")

    # Per-record blocks
    by_record: dict[tuple, list[dict]] = {}
    for r in rows:
        by_record.setdefault((r["gap_id"], r["file"]), []).append(r)

    for (gap_id, fname), entries in by_record.items():
        original = entries[0]["bquery_original"]
        lines.append(f"## {gap_id} :: `{fname}`")
        lines.append("")
        lines.append(f"**Original bquery:** `{original}`")
        lines.append("")
        for entry in entries:
            lines.append(f"### `{entry['model']}` ({entry['elapsed_s']:.1f}s)")
            lines.append("")
            if entry["error"]:
                lines.append(f"**ERROR:** `{entry['error']}`")
                lines.append("")
                continue
            if not entry["variants"]:
                lines.append("_(no variants parsed)_")
                lines.append("")
                lines.append("**Raw response:**")
                lines.append("```")
                lines.append(entry["raw"][:500])
                lines.append("```")
                lines.append("")
                continue
            for i, v in enumerate(entry["variants"], 1):
                lines.append(f"{i}. `{v}`")
            lines.append("")
        lines.append("---")
        lines.append("")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to: {report_path}")


if __name__ == "__main__":
    main()
