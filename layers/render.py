"""Layer 6: generate charts from data pull artifacts.

Each gap that has numeric data from BLS, FRED, or EBSCO gets one or more
PNG charts written alongside the JSON artifacts.  Charts are named
``chart_<source>_<slug>.png`` so artifact_export picks them up automatically
when it copies the source directory tree.

Supported source types and chart styles:
  bls          — line chart (CPI / time-series data points)
  fred         — horizontal timeline bars (series observation spans)
  ebsco_api    — publication-year histogram (when pub_date present)
  world_bank   — bar chart of indicator count (metadata fallback)
  bea / census / ilostat / oecd — skipped (metadata only, no numeric data)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from contracts import GapPullResult, RenderResult
from config import OrchestratorSettings


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_gap_result(
    pull_result: GapPullResult,
    settings: OrchestratorSettings,
    run_id: str,
) -> RenderResult:
    """Generate charts for all data-bearing source results in one gap."""

    if pull_result.status == "unresolvable":
        return RenderResult(
            gap_id=pull_result.gap_id,
            run_id=run_id,
            skipped=True,
            skip_reason="no documents pulled",
        )

    chart_paths: List[str] = []
    errors: List[str] = []

    for source_result in pull_result.results:
        if source_result.status == "failed":
            continue
        run_dir = Path(str(source_result.run_dir or ""))
        if not run_dir.is_dir():
            continue
        source_id = str(source_result.source_id or "")
        try:
            new_charts = _render_source_dir(source_id, run_dir, pull_result.gap_id)
            chart_paths.extend(str(p) for p in new_charts)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source_id}: {exc!s:.120}")

    return RenderResult(
        gap_id=pull_result.gap_id,
        run_id=run_id,
        charts_generated=len(chart_paths),
        chart_paths=chart_paths,
        error="; ".join(errors) if errors else "",
    )


# ---------------------------------------------------------------------------
# Per-source dispatch
# ---------------------------------------------------------------------------

_SKIP_SOURCES = {"bea", "census", "ilostat", "oecd"}


def _render_source_dir(source_id: str, run_dir: Path, gap_id: str) -> List[Path]:
    """Load all JSON artifacts in a source dir and generate charts. Returns paths."""
    if source_id in _SKIP_SOURCES:
        return []

    records = _load_records(run_dir)
    if not records:
        return []

    if source_id == "bls":
        return _chart_bls(records, run_dir, gap_id)
    if source_id == "fred":
        return _chart_fred(records, run_dir, gap_id)
    if source_id in {"ebsco_api", "ebscohost"}:
        return _chart_ebsco(records, run_dir, gap_id)
    if source_id == "world_bank":
        return _chart_world_bank(records, run_dir, gap_id)
    return []


def _load_records(run_dir: Path) -> List[Dict[str, Any]]:
    """Merge all JSON records from every *.json file in run_dir."""
    all_records: List[Dict[str, Any]] = []
    for path in sorted(run_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            rows = payload if isinstance(payload, list) else [payload]
            all_records.extend(r for r in rows if isinstance(r, dict))
        except Exception:
            continue
    return all_records


# ---------------------------------------------------------------------------
# BLS — line chart of CPI/time-series data points
# ---------------------------------------------------------------------------

def _chart_bls(records: List[Dict[str, Any]], out_dir: Path, gap_id: str) -> List[Path]:
    points = _bls_time_series(records)
    if not points:
        return []

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dates = [p[0] for p in points]
    values = [p[1] for p in points]

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(dates, values, marker="o", markersize=3, linewidth=1.5, color="#1f77b4")
    ax.set_title(f"BLS Time Series — {gap_id}", fontsize=11, pad=8)
    ax.set_xlabel("Date")
    ax.set_ylabel("Value")
    ax.tick_params(axis="x", rotation=45)
    plt.tight_layout()

    out_path = out_dir / "chart_bls_timeseries.png"
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return [out_path]


def _bls_time_series(records: List[Dict[str, Any]]) -> List[Tuple[str, float]]:
    """Extract (date_label, value) pairs from BLS time-series records, sorted."""
    points: List[Tuple[str, float]] = []
    for r in records:
        year = str(r.get("year", "")).strip()
        period = str(r.get("period", "")).strip()  # e.g. "M01"
        raw_val = str(r.get("value", "")).strip()
        if not (year and period and raw_val):
            continue
        try:
            val = float(raw_val)
        except ValueError:
            continue
        month = period.lstrip("M").zfill(2) if period.startswith("M") else "01"
        points.append((f"{year}-{month}", val))
    # Sort chronologically and deduplicate
    seen: set = set()
    unique = []
    for label, val in sorted(points):
        if label not in seen:
            seen.add(label)
            unique.append((label, val))
    return unique


# ---------------------------------------------------------------------------
# FRED — horizontal timeline bars per series
# ---------------------------------------------------------------------------

def _chart_fred(records: List[Dict[str, Any]], out_dir: Path, gap_id: str) -> List[Path]:
    series = _fred_series(records)
    if not series:
        return []

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    # Cap at 15 series for readability
    series = series[:15]
    n = len(series)
    fig_height = max(3.0, 0.5 * n + 1.5)
    fig, ax = plt.subplots(figsize=(10, fig_height))

    cmap = plt.get_cmap("Blues")
    max_pop = max((s.get("popularity", 0) for s in series), default=1) or 1

    for i, s in enumerate(series):
        start_yr = _year_from_iso(s.get("observation_start", ""))
        end_yr = _year_from_iso(s.get("observation_end", ""))
        if start_yr is None or end_yr is None:
            continue
        color = cmap(0.3 + 0.6 * s.get("popularity", 0) / max_pop)
        ax.barh(i, end_yr - start_yr, left=start_yr, height=0.6, color=color, alpha=0.85)
        label = (s.get("title") or s.get("id") or "")[:45]
        ax.text(start_yr, i, f"  {label}", va="center", fontsize=7.5)

    ax.set_yticks([])
    ax.set_xlabel("Year")
    ax.set_title(f"FRED Series Coverage — {gap_id}", fontsize=11, pad=8)
    patch = mpatches.Patch(color=cmap(0.7), label="Higher popularity →")
    ax.legend(handles=[patch], fontsize=8, loc="lower right")
    plt.tight_layout()

    out_path = out_dir / "chart_fred_series.png"
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return [out_path]


def _fred_series(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter FRED records to those with observation span data, sorted by popularity."""
    valid = [r for r in records if r.get("observation_start") and r.get("observation_end")]
    return sorted(valid, key=lambda r: int(r.get("popularity", 0)), reverse=True)


def _year_from_iso(iso: str) -> Optional[float]:
    m = re.match(r"(\d{4})", str(iso or ""))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# EBSCO — publication-year histogram
# ---------------------------------------------------------------------------

def _chart_ebsco(records: List[Dict[str, Any]], out_dir: Path, gap_id: str) -> List[Path]:
    year_quality = _ebsco_year_quality(records)
    if not year_quality:
        return []

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from collections import Counter

    quality_order = ["high", "medium", "seed"]
    colors = {"high": "#2ca02c", "medium": "#ff7f0e", "seed": "#aec7e8"}

    years_with_data = sorted({y for y, _ in year_quality})
    if len(years_with_data) < 2:
        return []

    fig, ax = plt.subplots(figsize=(9, 4))
    bottom = {y: 0 for y in years_with_data}
    for qlabel in quality_order:
        counts = Counter(y for y, q in year_quality if q == qlabel)
        vals = [counts.get(y, 0) for y in years_with_data]
        ax.bar(years_with_data, vals, bottom=[bottom[y] for y in years_with_data],
               label=qlabel, color=colors[qlabel], width=0.6)
        for y, v in zip(years_with_data, vals):
            bottom[y] += v

    ax.set_title(f"EBSCO Results by Publication Year — {gap_id}", fontsize=11, pad=8)
    ax.set_xlabel("Year")
    ax.set_ylabel("Documents")
    ax.legend(fontsize=8)
    ax.tick_params(axis="x", rotation=45)
    plt.tight_layout()

    out_path = out_dir / "chart_ebsco_years.png"
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return [out_path]


def _ebsco_year_quality(records: List[Dict[str, Any]]) -> List[Tuple[int, str]]:
    result = []
    for r in records:
        raw_year = str(r.get("pub_date", "") or r.get("year", "")).strip()
        m = re.match(r"(\d{4})", raw_year)
        if not m:
            continue
        yr = int(m.group(1))
        if not (1900 < yr < 2100):
            continue
        quality = str(r.get("quality_label", "seed")).strip().lower() or "seed"
        result.append((yr, quality))
    return result


# ---------------------------------------------------------------------------
# World Bank — simple bar chart of indicator count per topic
# ---------------------------------------------------------------------------

def _chart_world_bank(records: List[Dict[str, Any]], out_dir: Path, gap_id: str) -> List[Path]:
    from collections import Counter

    topic_counts: Counter = Counter()
    for r in records:
        for topic in r.get("topics") or []:
            name = str(topic.get("value", "Unknown")).strip().rstrip()
            if name:
                topic_counts[name] += 1

    if len(topic_counts) < 2:
        return []

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    top = topic_counts.most_common(12)
    labels = [t[0][:30] for t in top]
    counts = [t[1] for t in top]

    fig, ax = plt.subplots(figsize=(8, max(3, len(top) * 0.45)))
    ax.barh(range(len(labels)), counts, color="#1f77b4", alpha=0.8)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("Indicators")
    ax.set_title(f"World Bank Indicators by Topic — {gap_id}", fontsize=11, pad=8)
    ax.invert_yaxis()
    plt.tight_layout()

    out_path = out_dir / "chart_worldbank_topics.png"
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return [out_path]
