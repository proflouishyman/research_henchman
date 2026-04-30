"""Tests for scripts/fetch_documents.py CLI wrapper.

Verifies that the thin CLI correctly delegates to the document_fetch library,
handles --dry-run without writing files, and parses all expected flags.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers: minimal pull-output fixture
# ---------------------------------------------------------------------------

def _make_pull_dir(tmp_path: Path) -> Path:
    """Create a minimal pull_output directory with one seed JSON record."""
    run_dir = tmp_path / "pull_outputs" / "run_test"
    seed_dir = run_dir / "AUTO-01-G1" / "jstor"
    seed_dir.mkdir(parents=True)
    (seed_dir / "query_results.json").write_text(
        json.dumps([
            {
                "title": "Test Article",
                "url": "https://www.jstor.org/search?q=test",
                "quality_label": "seed",
            }
        ]),
        encoding="utf-8",
    )
    return run_dir


# ---------------------------------------------------------------------------
# Import helper: load the CLI module with PROJECT_ROOT on sys.path
# ---------------------------------------------------------------------------

def _import_cli():
    """Import scripts/fetch_documents ensuring project root is on path."""
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Force reimport so path changes take effect
    if "scripts.fetch_documents" in sys.modules:
        del sys.modules["scripts.fetch_documents"]
    if "fetch_documents" in sys.modules:
        del sys.modules["fetch_documents"]

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fetch_documents_cli",
        project_root / "scripts" / "fetch_documents.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Test: --dry-run exits cleanly without writing files
# ---------------------------------------------------------------------------

def test_dry_run_exits_cleanly_no_files_written(tmp_path: Path, monkeypatch) -> None:
    """--dry-run should print items and return without writing any output files."""
    run_dir = _make_pull_dir(tmp_path)
    run_id  = "run_test"

    # Build minimal settings stub that points data_root at tmp_path
    mock_settings = SimpleNamespace(
        data_root=tmp_path,
        playwright_cdp_url="http://127.0.0.1:9222",
        browser_provider="playwright_cdp",
        pull_timeout_seconds=30,
    )

    cli = _import_cli()

    # Patch out OrchestratorSettings so no real .env is required
    monkeypatch.setattr(cli, "OrchestratorSettings", SimpleNamespace(
        from_env=lambda: mock_settings,
    ))

    # Patch _resolve_run to return our fixture directory
    monkeypatch.setattr(cli, "_resolve_run", lambda rid, settings: (run_id, run_dir))

    # Patch make_browser_client (should never be called in dry-run)
    mock_browser = MagicMock()
    monkeypatch.setattr(cli, "make_browser_client", lambda s: mock_browser)

    # Inject CLI args: --dry-run --run-id run_test
    monkeypatch.setattr(sys, "argv", ["fetch_documents.py", "--dry-run", "--run-id", run_id])

    # main() should return normally (not sys.exit)
    cli.main()

    # Confirm no fetched/ subdirectory was created
    fetched_dirs = list(run_dir.rglob("fetched"))
    assert not fetched_dirs, f"--dry-run wrote files: {fetched_dirs}"

    # Confirm browser was never asked to fetch anything
    mock_browser.fetch.assert_not_called()
    mock_browser.fetch_with_eval.assert_not_called()


# ---------------------------------------------------------------------------
# Test: --dry-run with no items prints message and returns cleanly
# ---------------------------------------------------------------------------

def test_dry_run_empty_pull_dir(tmp_path: Path, monkeypatch) -> None:
    """--dry-run on an empty pull directory should print and return without error."""
    empty_run_dir = tmp_path / "pull_outputs" / "run_empty"
    empty_run_dir.mkdir(parents=True)

    mock_settings = SimpleNamespace(
        data_root=tmp_path,
        playwright_cdp_url="http://127.0.0.1:9222",
        browser_provider="playwright_cdp",
        pull_timeout_seconds=30,
    )

    cli = _import_cli()

    monkeypatch.setattr(cli, "OrchestratorSettings", SimpleNamespace(
        from_env=lambda: mock_settings,
    ))
    monkeypatch.setattr(cli, "_resolve_run", lambda rid, settings: ("run_empty", empty_run_dir))
    monkeypatch.setattr(cli, "make_browser_client", lambda s: MagicMock())
    monkeypatch.setattr(sys, "argv", ["fetch_documents.py", "--dry-run"])

    # Should print "No fetchable items" and return without error
    cli.main()


# ---------------------------------------------------------------------------
# Test: --no-prompt flag is accepted and skips input() calls
# ---------------------------------------------------------------------------

def test_no_prompt_flag_skips_input(tmp_path: Path, monkeypatch) -> None:
    """--no-prompt should parse correctly and never call input()."""
    run_dir = _make_pull_dir(tmp_path)

    mock_settings = SimpleNamespace(
        data_root=tmp_path,
        playwright_cdp_url="http://127.0.0.1:9222",
        browser_provider="playwright_cdp",
        pull_timeout_seconds=30,
    )

    cli = _import_cli()

    monkeypatch.setattr(cli, "OrchestratorSettings", SimpleNamespace(
        from_env=lambda: mock_settings,
    ))
    monkeypatch.setattr(cli, "_resolve_run", lambda rid, settings: ("run_test", run_dir))

    # Browser is available so CDP check passes without input()
    mock_browser = MagicMock()
    mock_browser.is_available.return_value = True
    # run_fetch returns a minimal stats object
    from adapters.document_fetch import FetchDocumentsStats
    monkeypatch.setattr(cli, "make_browser_client", lambda s: mock_browser)
    monkeypatch.setattr(cli, "run_fetch", lambda items, browser, emit=None, on_blocked=None, pause_on_captcha=False: FetchDocumentsStats(items_found=len(items)))

    monkeypatch.setattr(sys, "argv", ["fetch_documents.py", "--no-prompt", "--run-id", "run_test"])

    # Ensure input() is never reached (would block the test if called)
    with patch("builtins.input", side_effect=AssertionError("input() called in --no-prompt mode")):
        cli.main()


# ---------------------------------------------------------------------------
# Test: _make_emit produces a callable that formats [stage/status] lines
# ---------------------------------------------------------------------------

def test_make_emit_formats_log_line(capsys) -> None:
    """_make_emit() should return a callable that prints [stage/status] message."""
    cli = _import_cli()
    emit = cli._make_emit()

    emit("fetching", "seed_ok", "article saved", {"gap_id": "G1"})
    captured = capsys.readouterr()
    assert "[fetching/seed_ok] article saved" in captured.out


# ---------------------------------------------------------------------------
# Test: _port_from_url extracts port correctly
# ---------------------------------------------------------------------------

def test_port_from_url_standard() -> None:
    cli = _import_cli()
    assert cli._port_from_url("http://localhost:9222") == 9222
    assert cli._port_from_url("http://127.0.0.1:9999") == 9999


def test_port_from_url_default_fallback() -> None:
    cli = _import_cli()
    # No port in URL should default to 9222
    assert cli._port_from_url("http://localhost") == 9222


# ---------------------------------------------------------------------------
# Test: --no-launch does NOT spawn Chrome when CDP is unreachable
# ---------------------------------------------------------------------------

def test_no_launch_does_not_spawn_chrome(tmp_path: Path, monkeypatch) -> None:
    """--no-launch should preserve legacy behavior: print help and wait, never call Popen."""
    run_dir = _make_pull_dir(tmp_path)

    mock_settings = SimpleNamespace(
        data_root=tmp_path,
        playwright_cdp_url="http://127.0.0.1:9222",
        browser_provider="playwright_cdp",
        pull_timeout_seconds=30,
    )

    cli = _import_cli()

    monkeypatch.setattr(cli, "OrchestratorSettings", SimpleNamespace(
        from_env=lambda: mock_settings,
    ))
    monkeypatch.setattr(cli, "_resolve_run", lambda rid, settings: ("run_test", run_dir))

    # Browser is NOT available (CDP unreachable)
    mock_browser = MagicMock()
    mock_browser.is_available.return_value = False
    monkeypatch.setattr(cli, "make_browser_client", lambda s: mock_browser)

    # Track whether Popen was ever called
    popen_calls = []

    def _fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError("subprocess.Popen must NOT be called with --no-launch")

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)

    # With --no-launch + --no-prompt, the CLI should print the help box and then
    # attempt to rebuild the client; if it's still unavailable, it exits with
    # sys.exit(1).  We catch that exit rather than letting it propagate.
    monkeypatch.setattr(sys, "argv", [
        "fetch_documents.py", "--no-launch", "--no-prompt", "--run-id", "run_test",
    ])

    with pytest.raises(SystemExit):
        cli.main()

    # The key assertion: Popen was never reached
    assert not popen_calls, "Popen was called despite --no-launch"


# ---------------------------------------------------------------------------
# Test: default auto-launch calls Popen with --remote-debugging-port and
#       --user-data-dir, polls CDP, and proceeds normally
# ---------------------------------------------------------------------------

def test_auto_launch_calls_popen_with_cdp_flags(tmp_path: Path, monkeypatch) -> None:
    """Default mode: Popen should be called with the right Chrome flags when CDP is unreachable."""
    run_dir = _make_pull_dir(tmp_path)

    mock_settings = SimpleNamespace(
        data_root=tmp_path,
        playwright_cdp_url="http://127.0.0.1:9222",
        browser_provider="playwright_cdp",
        pull_timeout_seconds=30,
    )

    cli = _import_cli()

    monkeypatch.setattr(cli, "OrchestratorSettings", SimpleNamespace(
        from_env=lambda: mock_settings,
    ))
    monkeypatch.setattr(cli, "_resolve_run", lambda rid, settings: ("run_test", run_dir))

    # Browser reports unavailable on first call, then available (after launch).
    call_count = {"n": 0}
    mock_browser = MagicMock()

    def _is_available_side_effect():
        call_count["n"] += 1
        # First call (before launch) → False; subsequent calls → True
        return call_count["n"] > 1

    mock_browser.is_available.side_effect = _is_available_side_effect
    # open_tabs is a no-op for this test; run_fetch returns minimal stats.
    from adapters.document_fetch import FetchDocumentsStats
    monkeypatch.setattr(cli, "make_browser_client", lambda s: mock_browser)
    monkeypatch.setattr(cli, "run_fetch",
                        lambda items, browser, emit=None, on_blocked=None, pause_on_captcha=False: FetchDocumentsStats(items_found=len(items)))

    # Capture Popen arguments without actually spawning Chrome.
    popen_calls = []

    class _FakeProc:
        pid = 99999

    def _fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)

    # Stub out the CDP poll so the test does not wait 15 s.
    monkeypatch.setattr(cli, "_cdp_poll_until_ready", lambda url, **kw: True)

    monkeypatch.setattr(sys, "argv", [
        "fetch_documents.py", "--no-prompt", "--run-id", "run_test",
    ])

    cli.main()

    # Popen must have been called exactly once.
    assert len(popen_calls) == 1, f"Expected 1 Popen call, got {len(popen_calls)}"
    launched_cmd = popen_calls[0]

    # The command must contain --remote-debugging-port= and --user-data-dir=
    cmd_str = " ".join(launched_cmd)
    assert "--remote-debugging-port=" in cmd_str, (
        f"--remote-debugging-port not in Popen cmd: {launched_cmd}"
    )
    assert "--user-data-dir=" in cmd_str, (
        f"--user-data-dir not in Popen cmd: {launched_cmd}"
    )
