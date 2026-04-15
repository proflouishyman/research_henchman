"""Adapter link emission tests for click-through document UX."""

from __future__ import annotations

import json
import socket
import threading
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from adapters import document_links
from adapters.document_links import build_link_rows, provider_search_url
from adapters.io_utils import era_years_from_gap
from adapters.keyed_apis import EbscoApiAdapter
from adapters.playwright_adapters import JstorPlaywrightAdapter
from contracts import GapPriority, GapType, PlannedGap


def _gap() -> PlannedGap:
    return PlannedGap(
        gap_id="AUTO-01-G1",
        chapter="Chapter One",
        claim_text="Claim text",
        gap_type=GapType.IMPLICIT,
        priority=GapPriority.MEDIUM,
    )


def _gap_with_era(era_start: int, era_end: int) -> PlannedGap:
    """Return a gap whose query_ladder carries synonym ring era bounds."""

    gap = _gap()
    gap.query_ladder = {
        "synonym_ring": {
            "era_start": era_start,
            "era_end": era_end,
            "terminology_shifts": [],
            "institutional_names": [],
            "era_modifiers": [],
        },
        "constrained": "{PRIMARY} newspaper historical press",
        "contextual": "{PRIMARY}",
        "broad": "{PRIMARY}",
        "fallback": "historical evidence",
        "generation_method": "heuristic",
    }
    return gap


def test_provider_search_url_builds_jstor_query() -> None:
    out = provider_search_url("jstor", "john mcdonogh supercargo")
    assert "jstor.org" in out
    assert "john+mcdonogh+supercargo" in out


def test_build_link_rows_orders_local_high_quality_before_seed(monkeypatch, tmp_path) -> None:
    local_pdf = tmp_path / "local_hit.pdf"
    local_pdf.write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        document_links,
        "local_document_candidates",
        lambda _query, limit=5: [(local_pdf, 5.0)],
    )

    rows = build_link_rows("jstor", "john mcdonogh supercargo", "AUTO-01-G1", limit_local=2)
    assert rows
    assert rows[0].get("quality_label") == "high"
    assert rows[0].get("path") == str(local_pdf)
    assert any(row.get("quality_label") == "seed" for row in rows)


def test_ebsco_adapter_emits_clickthrough_rows(tmp_path) -> None:
    adapter = EbscoApiAdapter()
    run_dir = tmp_path / "runs"
    result = adapter.pull(_gap(), "john mcdonogh supercargo", str(run_dir))

    assert result.document_count >= 1
    assert result.status in {"completed", "partial"}
    artifact = Path(result.run_dir) / "john_mcdonogh_supercargo.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and payload
    assert any(str(row.get("url", "")).startswith("http") for row in payload)
    assert any(str(row.get("quality_label", "")) for row in payload)


def test_playwright_seed_adapter_emits_clickthrough_rows(tmp_path) -> None:
    adapter = JstorPlaywrightAdapter()
    run_dir = tmp_path / "runs"
    result = adapter.pull(_gap(), "john mcdonogh supercargo", str(run_dir))

    assert result.document_count >= 1
    artifact = Path(result.run_dir) / "john_mcdonogh_supercargo.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert any(str(row.get("url", "")).startswith("http") for row in payload)
    assert any(str(row.get("quality_label", "")) for row in payload)


def test_playwright_seed_adapter_resolves_seed_urls_into_local_artifacts(monkeypatch, tmp_path) -> None:
    web_root = tmp_path / "web"
    web_root.mkdir(parents=True, exist_ok=True)
    (web_root / "doc.pdf").write_bytes(b"%PDF-1.4 fixture")
    (web_root / "index.html").write_text(
        '<html><body><a href="/doc.pdf">doc</a></body></html>',
        encoding="utf-8",
    )

    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_root), **kwargs)

        def log_message(self, format, *args):  # noqa: A003
            return

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
    server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setattr(document_links, "local_document_candidates", lambda _query, limit=5: [])
    monkeypatch.setattr(document_links, "provider_search_url", lambda _source_id, _query, **_kw: f"http://127.0.0.1:{port}/index.html")

    try:
        adapter = JstorPlaywrightAdapter()
        run_dir = tmp_path / "runs"
        result = adapter.pull(_gap(), "john mcdonogh supercargo", str(run_dir))
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.status in {"completed", "partial"}
    artifact = Path(result.run_dir) / "john_mcdonogh_supercargo.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert any(str(row.get("link_type", "")).startswith("resolved") for row in payload)
    resolved_paths = [Path(str(row.get("path", ""))) for row in payload if str(row.get("link_type", "")).startswith("resolved")]
    assert resolved_paths
    assert any(p.suffix.lower() == ".html" for p in resolved_paths)
    assert any(p.suffix.lower() == ".pdf" for p in resolved_paths)


# ---------------------------------------------------------------------------
# Era date faceting tests
# ---------------------------------------------------------------------------


def test_provider_search_url_jstor_includes_era_dates() -> None:
    url = provider_search_url("jstor", "electronic commerce", era_start=1993, era_end=1997)
    assert "jstor.org" in url
    assert "sd=1993" in url
    assert "ed=1997" in url


def test_provider_search_url_jstor_no_dates_when_missing() -> None:
    url = provider_search_url("jstor", "electronic commerce")
    assert "sd=" not in url
    assert "ed=" not in url


def test_provider_search_url_proquest_includes_era_dates() -> None:
    url = provider_search_url("proquest_historical_newspapers", "railroad labor", era_start=1880, era_end=1900)
    assert "proquest.com" in url
    assert "daterange=custom" in url
    assert "startdate=1880" in url
    assert "enddate=1900" in url


def test_provider_search_url_ebsco_includes_era_dates() -> None:
    url = provider_search_url("ebsco_api", "online retailing", era_start=1994, era_end=1998)
    assert "ebscohost.com" in url
    assert "DT1=19940101" in url
    assert "DT2=19981231" in url


def test_provider_search_url_gale_includes_era_dates() -> None:
    url = provider_search_url("gale_primary_sources", "civil war correspondence", era_start=1861, era_end=1865)
    assert "gale.com" in url
    assert "startDate=1861" in url
    assert "endDate=1865" in url


def test_provider_search_url_no_era_for_unsupported_source() -> None:
    # Project MUSE does not currently use era facets; URL must still work.
    url = provider_search_url("project_muse", "abolitionism", era_start=1850, era_end=1870)
    assert "muse.jhu.edu" in url


def test_build_link_rows_passes_era_to_provider_url() -> None:
    rows = build_link_rows("jstor", "electronic commerce", "G1", era_start=1993, era_end=1997)
    seed_rows = [r for r in rows if r.get("quality_label") == "seed"]
    assert seed_rows, "Expected at least one seed provider link row"
    seed_url = seed_rows[0]["url"]
    assert "sd=1993" in seed_url
    assert "ed=1997" in seed_url


def test_era_years_from_gap_returns_none_when_no_ladder() -> None:
    gap = _gap()
    start, end = era_years_from_gap(gap)
    assert start is None
    assert end is None


def test_era_years_from_gap_extracts_era_bounds() -> None:
    gap = _gap_with_era(1994, 1998)
    start, end = era_years_from_gap(gap)
    assert start == 1994
    assert end == 1998


def test_era_years_from_gap_handles_malformed_values() -> None:
    gap = _gap()
    gap.query_ladder = {"synonym_ring": {"era_start": "not-a-year", "era_end": None}}
    start, end = era_years_from_gap(gap)
    assert start is None
    assert end is None


def test_ebsco_adapter_propagates_era_dates_to_seed_url(tmp_path, monkeypatch) -> None:
    captured: list = []

    def _fake_build(source_id, query, gap_id, limit_local=5, era_start=None, era_end=None):
        captured.append({"era_start": era_start, "era_end": era_end})
        return [{"title": "seed", "url": "https://example.com", "quality_label": "seed", "quality_rank": "20", "gap_id": gap_id, "query": query, "link_type": "provider_search"}]

    import adapters.keyed_apis as keyed_apis_mod
    monkeypatch.setattr(keyed_apis_mod, "build_link_rows", _fake_build)

    gap = _gap_with_era(1994, 1998)
    adapter = EbscoApiAdapter()
    adapter.pull(gap, "online retailing", str(tmp_path))

    assert captured, "build_link_rows was not called"
    assert captured[0]["era_start"] == 1994
    assert captured[0]["era_end"] == 1998


def test_jstor_adapter_propagates_era_dates_to_seed_url(tmp_path, monkeypatch) -> None:
    captured: list = []

    def _fake_build(source_id, query, gap_id, limit_local=5, era_start=None, era_end=None):
        captured.append({"era_start": era_start, "era_end": era_end})
        return [{"title": "seed", "url": "https://example.com", "quality_label": "seed", "quality_rank": "20", "gap_id": gap_id, "query": query, "link_type": "provider_search"}]

    import adapters.playwright_adapters as pw_mod
    monkeypatch.setattr(pw_mod, "build_link_rows", _fake_build)

    gap = _gap_with_era(1850, 1870)
    adapter = JstorPlaywrightAdapter()
    adapter._link_seed_result(gap, "abolitionism archives", str(tmp_path), note="test")
