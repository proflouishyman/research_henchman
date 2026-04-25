"""Layer 6 render / chart generation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts import GapPullResult, SourceResult, SourceType
from layers import render


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pull_result(source_id: str, records: list, tmp_path: Path) -> GapPullResult:
    gap_id = "AUTO-01-G1"
    src_dir = tmp_path / gap_id / source_id
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "test_query.json").write_text(json.dumps(records), encoding="utf-8")

    return GapPullResult(
        gap_id=gap_id,
        results=[
            SourceResult(
                source_id=source_id,
                source_type=SourceType.KEYED_API,
                query="test query",
                gap_id=gap_id,
                document_count=len(records),
                run_dir=str(src_dir),
                artifact_type="json_records",
                status="completed",
            )
        ],
        status="completed",
        total_documents=len(records),
    )


# ---------------------------------------------------------------------------
# BLS
# ---------------------------------------------------------------------------

BLS_RECORDS = [
    {"year": "2020", "period": "M01", "periodName": "January", "value": "258.0"},
    {"year": "2020", "period": "M06", "periodName": "June", "value": "257.5"},
    {"year": "2021", "period": "M01", "periodName": "January", "value": "261.6"},
    {"year": "2021", "period": "M06", "periodName": "June", "value": "271.7"},
    {"year": "2022", "period": "M01", "periodName": "January", "value": "281.1"},
]


def test_render_bls_produces_png(settings_factory, tmp_path):
    settings = settings_factory()
    pull_result = _make_pull_result("bls", BLS_RECORDS, tmp_path)

    result = render.render_gap_result(pull_result, settings, "run_test")

    assert result.charts_generated == 1
    assert result.error == ""
    chart_path = Path(result.chart_paths[0])
    assert chart_path.exists()
    assert chart_path.suffix == ".png"
    assert chart_path.stat().st_size > 1000


def test_render_bls_empty_records_skips_chart(settings_factory, tmp_path):
    settings = settings_factory()
    pull_result = _make_pull_result("bls", [], tmp_path)

    result = render.render_gap_result(pull_result, settings, "run_test")

    assert result.charts_generated == 0
    assert not result.chart_paths


# ---------------------------------------------------------------------------
# FRED
# ---------------------------------------------------------------------------

FRED_RECORDS = [
    {"id": "CPIAUCSL", "title": "CPI All Urban Consumers", "observation_start": "1947-01-01",
     "observation_end": "2024-01-01", "frequency": "Monthly", "units": "Index", "popularity": 91},
    {"id": "CPILFESL", "title": "CPI Less Food and Energy", "observation_start": "1957-01-01",
     "observation_end": "2024-01-01", "frequency": "Monthly", "units": "Index", "popularity": 72},
    {"id": "PCEPI", "title": "PCE Price Index", "observation_start": "1959-01-01",
     "observation_end": "2024-01-01", "frequency": "Monthly", "units": "Index", "popularity": 68},
]


def test_render_fred_produces_png(settings_factory, tmp_path):
    settings = settings_factory()
    pull_result = _make_pull_result("fred", FRED_RECORDS, tmp_path)

    result = render.render_gap_result(pull_result, settings, "run_test")

    assert result.charts_generated == 1
    assert Path(result.chart_paths[0]).exists()


def test_render_fred_skips_records_without_observation_span(settings_factory, tmp_path):
    settings = settings_factory()
    records = [{"id": "X", "title": "No dates"}]  # missing observation_start / end
    pull_result = _make_pull_result("fred", records, tmp_path)

    result = render.render_gap_result(pull_result, settings, "run_test")

    assert result.charts_generated == 0


# ---------------------------------------------------------------------------
# EBSCO
# ---------------------------------------------------------------------------

EBSCO_RECORDS = [
    {"title": "A", "pub_date": "2018", "quality_label": "high"},
    {"title": "B", "pub_date": "2019", "quality_label": "medium"},
    {"title": "C", "pub_date": "2019", "quality_label": "high"},
    {"title": "D", "pub_date": "2020", "quality_label": "seed"},
    {"title": "E", "pub_date": "2021", "quality_label": "medium"},
    {"title": "F", "pub_date": "2021", "quality_label": "high"},
]


def test_render_ebsco_produces_year_histogram(settings_factory, tmp_path):
    settings = settings_factory()
    pull_result = _make_pull_result("ebsco_api", EBSCO_RECORDS, tmp_path)

    result = render.render_gap_result(pull_result, settings, "run_test")

    assert result.charts_generated == 1
    assert "ebsco_years" in result.chart_paths[0]


def test_render_ebsco_seed_only_no_year_skips(settings_factory, tmp_path):
    settings = settings_factory()
    records = [{"title": "seed link", "url": "http://x.com", "quality_label": "seed"}]
    pull_result = _make_pull_result("ebsco_api", records, tmp_path)

    result = render.render_gap_result(pull_result, settings, "run_test")

    assert result.charts_generated == 0


# ---------------------------------------------------------------------------
# Skipped sources
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source_id", ["bea", "census", "ilostat", "oecd"])
def test_render_skipped_sources_produce_no_charts(settings_factory, tmp_path, source_id):
    settings = settings_factory()
    records = [{"key": "val", "name": "something"}]
    pull_result = _make_pull_result(source_id, records, tmp_path)

    result = render.render_gap_result(pull_result, settings, "run_test")

    assert result.charts_generated == 0


# ---------------------------------------------------------------------------
# Unresolvable gap
# ---------------------------------------------------------------------------

def test_render_unresolvable_gap_is_skipped(settings_factory):
    settings = settings_factory()
    pull_result = GapPullResult(gap_id="AUTO-01-G1", status="unresolvable")

    result = render.render_gap_result(pull_result, settings, "run_test")

    assert result.skipped is True
    assert result.charts_generated == 0


# ---------------------------------------------------------------------------
# World Bank
# ---------------------------------------------------------------------------

WB_RECORDS = [
    {"id": "NY.GDP.MKTP.CD", "name": "GDP (current US$)",
     "topics": [{"id": "3", "value": "Economy & Growth"}, {"id": "19", "value": "Trade"}]},
    {"id": "SP.POP.TOTL", "name": "Population, total",
     "topics": [{"id": "8", "value": "Health"}, {"id": "17", "value": "Social Development"}]},
    {"id": "NE.EXP.GNFS.ZS", "name": "Exports of goods",
     "topics": [{"id": "19", "value": "Trade"}, {"id": "3", "value": "Economy & Growth"}]},
]


def test_render_world_bank_produces_topic_chart(settings_factory, tmp_path):
    settings = settings_factory()
    pull_result = _make_pull_result("world_bank", WB_RECORDS, tmp_path)

    result = render.render_gap_result(pull_result, settings, "run_test")

    assert result.charts_generated == 1
    assert "worldbank" in result.chart_paths[0]
