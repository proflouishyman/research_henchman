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
    monkeypatch.setattr(document_links, "provider_search_url", lambda _source_id, _query: f"http://127.0.0.1:{port}/index.html")

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
