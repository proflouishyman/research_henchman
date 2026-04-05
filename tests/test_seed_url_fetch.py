"""Tests for seed URL resolution into pulled local artifacts."""

from __future__ import annotations

import socket
import sys
import threading
import types
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import adapters.seed_url_fetch as seed_url_fetch
from adapters.seed_url_fetch import resolve_seed_rows


def test_resolve_seed_rows_fetches_parent_and_child_documents(tmp_path):
    web_root = tmp_path / "web"
    web_root.mkdir(parents=True, exist_ok=True)
    (web_root / "paper.pdf").write_bytes(b"%PDF-1.4 test")
    (web_root / "index.html").write_text(
        '<html><body><a href="/paper.pdf">paper</a><a href="/style.css">css</a></body></html>',
        encoding="utf-8",
    )
    (web_root / "style.css").write_text("body{color:black}", encoding="utf-8")

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

    try:
        rows = [
            {
                "title": "seed search row",
                "url": f"http://127.0.0.1:{port}/index.html",
                "link_type": "provider_search",
                "quality_label": "seed",
                "quality_rank": "20",
            }
        ]
        source_root = tmp_path / "output" / "AUTO-01-G1" / "project_muse"
        out_rows, stats = resolve_seed_rows(
            rows=rows,
            source_root=source_root,
            source_id="project_muse",
            query="history of capitalism",
            gap_id="AUTO-01-G1",
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert out_rows
    assert int(stats.get("resolved_files", 0)) >= 2
    assert any(Path(str(row.get("path", ""))).suffix.lower() == ".html" for row in out_rows)
    assert any(Path(str(row.get("path", ""))).suffix.lower() == ".pdf" for row in out_rows)
    assert not any(Path(str(row.get("path", ""))).suffix.lower() == ".css" for row in out_rows)
    assert any(str(row.get("quality_label", "")).lower() == "medium" for row in out_rows)
    assert any(str(row.get("quality_label", "")).lower() == "high" for row in out_rows)


def test_resolve_seed_rows_flags_verification_challenge_as_blocked(tmp_path):
    web_root = tmp_path / "web"
    web_root.mkdir(parents=True, exist_ok=True)
    (web_root / "index.html").write_text(
        "<html><body>Verification required! Please complete this challenge to continue.</body></html>",
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

    try:
        rows = [
            {
                "title": "seed search row",
                "url": f"http://127.0.0.1:{port}/index.html",
                "link_type": "provider_search",
                "quality_label": "seed",
                "quality_rank": "20",
            }
        ]
        source_root = tmp_path / "output" / "AUTO-01-G1" / "project_muse"
        out_rows, stats = resolve_seed_rows(
            rows=rows,
            source_root=source_root,
            source_id="project_muse",
            query="history of capitalism",
            gap_id="AUTO-01-G1",
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert out_rows
    blocked = [row for row in out_rows if str(row.get("blocked_reason", ""))]
    assert blocked
    assert str(blocked[0].get("blocked_reason", "")) == "verification_challenge"
    assert str(blocked[0].get("quality_label", "")).lower() == "seed"
    assert "retry" in str(blocked[0].get("action_required", "")).lower()
    assert int(stats.get("blocked_files", 0)) >= 1
    assert int(stats.get("challenge_blocks", 0)) >= 1


def test_fetch_via_cdp_keeps_user_browser_session_open(monkeypatch):
    calls = {"browser_close": 0, "context_close": 0}

    class _FakePage:
        def goto(self, *_args, **_kwargs):
            return None

        def content(self):
            return "<html><body>ok</body></html>"

        def close(self):
            return None

    class _FakeContext:
        def new_page(self):
            return _FakePage()

        def close(self):
            calls["context_close"] += 1

    class _FakeBrowser:
        def __init__(self):
            self.contexts = [_FakeContext()]

        def new_context(self):
            return _FakeContext()

        def close(self):
            calls["browser_close"] += 1

    class _FakePlaywright:
        def __init__(self):
            self.chromium = self

        def connect_over_cdp(self, _url):
            return _FakeBrowser()

    class _FakePlaywrightCtx:
        def __enter__(self):
            return _FakePlaywright()

        def __exit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setenv("ORCH_PLAYWRIGHT_CDP_URL", "http://host.docker.internal:9222")
    monkeypatch.setattr(seed_url_fetch, "effective_cdp_url", lambda url: url)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(sync_playwright=lambda: _FakePlaywrightCtx()))

    html = seed_url_fetch._fetch_via_cdp("https://example.com")

    assert "ok" in html
    assert calls["browser_close"] == 0
    assert calls["context_close"] == 0
