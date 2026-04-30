"""Tests for scripts/normalize_seed_queries.py and the document_fetch.py wiring.

Coverage:
  1. normalize_seed_queries._process_file — reads JSON, calls LLM (mocked),
     writes bquery_normalized; idempotent on re-run; respects --force.
  2. normalize_seed_queries.main — CLI ergonomics: --dry-run, --limit, missing run dir.
  3. document_fetch._classify_record + _splice_normalized_bquery — when a record has
     bquery_normalized, that text ends up in the seed URL used by the fetch pipeline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

# Make sure the project root is on the path (mirrors the script's own bootstrap).
import importlib.util
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _seed_record(bquery: str = "Amazon e-commerce revolution", **extra: Any) -> Dict[str, Any]:
    """Return a minimal seed JSON record matching the real artifact shape."""
    rec: Dict[str, Any] = {
        "title": "ebsco_api search results",
        "url": (
            "https://search.ebscohost.com/login.aspx?direct=true"
            f"&bquery={bquery.replace(' ', '+')}"
        ),
        "query": bquery,
        "bquery": bquery,
        "gap_id": "AUTO-01-G1",
        "link_type": "provider_search",
        "quality_label": "seed",
        "quality_rank": "20",
    }
    rec.update(extra)
    return rec


def _write_seed_file(directory: Path, records: List[Dict[str, Any]], name: str = "results.json") -> Path:
    """Write *records* as a JSON array to *directory*/<name>."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _make_run_dir(tmp_path: Path, run_id: str = "run_test", source: str = "ebsco_api") -> Path:
    """Create a minimal pull_outputs/<run_id>/<gap_id>/<source>/ tree."""
    src_dir = tmp_path / "pull_outputs" / run_id / "AUTO-01-G1" / source
    _write_seed_file(src_dir, [_seed_record()])
    return tmp_path / "pull_outputs"


# ---------------------------------------------------------------------------
# 1. _process_file — core normalization logic
# ---------------------------------------------------------------------------

class TestProcessFile:
    """Unit tests for normalize_seed_queries._process_file."""

    def _make_client(self, normalized: str = '("Amazon" OR "Amazon.com") AND "e-commerce"') -> MagicMock:
        """Return a mock LLMClient whose complete() returns *normalized*."""
        client = MagicMock()
        client.complete.return_value = normalized
        return client

    def test_writes_bquery_normalized_field(self, tmp_path: Path) -> None:
        """_process_file adds bquery_normalized to each record and saves the file."""
        from scripts.normalize_seed_queries import _process_file

        records = [_seed_record("Amazon e-commerce")]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client('("Amazon") AND "e-commerce"')

        n = _process_file(jf, client, force=False, dry_run=False, limit_remaining=None)

        assert n == 1
        saved = json.loads(jf.read_text(encoding="utf-8"))
        assert isinstance(saved, list)
        assert saved[0]["bquery_normalized"] == '("Amazon") AND "e-commerce"'
        # Original bquery must be preserved.
        assert saved[0]["bquery"] == "Amazon e-commerce"
        # bquery_original mirrors original for explicit rollback convenience.
        assert saved[0]["bquery_original"] == "Amazon e-commerce"

    def test_idempotent_skips_already_normalized(self, tmp_path: Path) -> None:
        """Re-running without --force skips records that already have bquery_normalized."""
        from scripts.normalize_seed_queries import _process_file

        records = [_seed_record("Amazon e-commerce", bquery_normalized="already set")]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client()

        n = _process_file(jf, client, force=False, dry_run=False, limit_remaining=None)

        assert n == 0
        client.complete.assert_not_called()

    def test_force_renormalizes_existing(self, tmp_path: Path) -> None:
        """--force causes already-normalized records to be reprocessed."""
        from scripts.normalize_seed_queries import _process_file

        fresh = '("Amazon" OR "Amazon.com") AND "e-commerce"'
        records = [_seed_record("Amazon e-commerce", bquery_normalized="old value")]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client(fresh)

        n = _process_file(jf, client, force=True, dry_run=False, limit_remaining=None)

        assert n == 1
        saved = json.loads(jf.read_text(encoding="utf-8"))
        assert saved[0]["bquery_normalized"] == fresh

    def test_dry_run_does_not_write_file(self, tmp_path: Path) -> None:
        """--dry-run shows activity counters but must not modify the JSON file."""
        from scripts.normalize_seed_queries import _process_file

        records = [_seed_record("test query")]
        jf = _write_seed_file(tmp_path, records)
        original_text = jf.read_text(encoding="utf-8")
        # dry_run=True — LLM is not called (we use a sentinel to confirm)
        client = MagicMock()
        client.complete.return_value = "should not matter"

        n = _process_file(jf, client, force=False, dry_run=True, limit_remaining=None)

        assert n == 1  # counted as "would-normalize"
        assert jf.read_text(encoding="utf-8") == original_text
        client.complete.assert_not_called()

    def test_limit_remaining_stops_early(self, tmp_path: Path) -> None:
        """limit_remaining=1 normalizes at most 1 record from a multi-record file."""
        from scripts.normalize_seed_queries import _process_file

        records = [_seed_record(f"query {i}") for i in range(3)]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client("normalized")

        n = _process_file(jf, client, force=False, dry_run=False, limit_remaining=1)

        assert n == 1
        saved = json.loads(jf.read_text(encoding="utf-8"))
        # Only the first record should be normalized; rest should be untouched.
        assert saved[0].get("bquery_normalized") == "normalized"
        assert "bquery_normalized" not in saved[1]
        assert "bquery_normalized" not in saved[2]

    def test_skips_records_without_bquery(self, tmp_path: Path) -> None:
        """Records with no bquery / query field are silently skipped."""
        from scripts.normalize_seed_queries import _process_file

        records = [{"title": "no query here", "quality_label": "seed"}]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client()

        n = _process_file(jf, client, force=False, dry_run=False, limit_remaining=None)

        assert n == 0
        client.complete.assert_not_called()

    def test_truncates_llm_response_to_200_chars(self, tmp_path: Path) -> None:
        """Responses longer than 200 chars are truncated to stay within EBSCO limits."""
        from scripts.normalize_seed_queries import _process_file

        long_response = "A" * 250
        records = [_seed_record("something")]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client(long_response)

        _process_file(jf, client, force=False, dry_run=False, limit_remaining=None)

        saved = json.loads(jf.read_text(encoding="utf-8"))
        assert len(saved[0]["bquery_normalized"]) <= 200


# ---------------------------------------------------------------------------
# 2. main() — CLI argument handling
# ---------------------------------------------------------------------------

class TestMain:
    """Integration tests for normalize_seed_queries.main()."""

    def test_returns_error_when_run_dir_missing(self, tmp_path: Path) -> None:
        """main() exits with non-zero when the run-id directory does not exist."""
        from scripts.normalize_seed_queries import main

        rc = main([
            "--run-id", "nonexistent_run",
            "--data-root", str(tmp_path),
        ])
        assert rc != 0

    def test_dry_run_reports_without_writing(self, tmp_path: Path) -> None:
        """main() with --dry-run prints activity but leaves JSON unchanged."""
        from scripts.normalize_seed_queries import main

        pull_root = _make_run_dir(tmp_path)
        jf = pull_root / "run_test" / "AUTO-01-G1" / "ebsco_api" / "results.json"
        original_text = jf.read_text(encoding="utf-8")

        with patch("scripts.normalize_seed_queries.make_llm_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.model = "qwen2.5:7b"
            mock_client.complete.return_value = "normalized query"
            mock_factory.return_value = mock_client

            rc = main([
                "--run-id", "run_test",
                "--data-root", str(tmp_path),
                "--dry-run",
            ])

        assert rc == 0
        # File must be untouched.
        assert jf.read_text(encoding="utf-8") == original_text
        # LLM should NOT be called in dry-run mode.
        mock_client.complete.assert_not_called()

    def test_limit_flag_restricts_records_processed(self, tmp_path: Path) -> None:
        """--limit N causes main() to stop after N records are normalized."""
        from scripts.normalize_seed_queries import main

        # Write two records in the same file.
        src_dir = tmp_path / "pull_outputs" / "run_lim" / "AUTO-01-G1" / "ebsco_api"
        _write_seed_file(src_dir, [_seed_record("q1"), _seed_record("q2")])

        with patch("scripts.normalize_seed_queries.make_llm_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.model = "qwen2.5:7b"
            mock_client.complete.return_value = "norm"
            mock_factory.return_value = mock_client

            rc = main([
                "--run-id", "run_lim",
                "--data-root", str(tmp_path),
                "--limit", "1",
            ])

        assert rc == 0
        jf = src_dir / "results.json"
        saved = json.loads(jf.read_text(encoding="utf-8"))
        # Exactly one record should be normalized.
        normalized_count = sum(1 for r in saved if "bquery_normalized" in r)
        assert normalized_count == 1

    def test_gap_id_filter_restricts_to_one_gap(self, tmp_path: Path) -> None:
        """--gap-id only processes JSON files inside that specific gap directory."""
        from scripts.normalize_seed_queries import main

        root = tmp_path / "pull_outputs" / "run_gf"
        # Gap 1
        g1 = root / "AUTO-01-G1" / "ebsco_api"
        _write_seed_file(g1, [_seed_record("query g1")])
        # Gap 2
        g2 = root / "AUTO-02-G1" / "ebsco_api"
        _write_seed_file(g2, [_seed_record("query g2")])

        with patch("scripts.normalize_seed_queries.make_llm_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.model = "qwen2.5:7b"
            mock_client.complete.return_value = "norm"
            mock_factory.return_value = mock_client

            rc = main([
                "--run-id", "run_gf",
                "--gap-id", "AUTO-01-G1",
                "--data-root", str(tmp_path),
            ])

        assert rc == 0
        # AUTO-01-G1 should be normalized.
        g1_saved = json.loads((g1 / "results.json").read_text())
        assert "bquery_normalized" in g1_saved[0]
        # AUTO-02-G1 should be untouched.
        g2_saved = json.loads((g2 / "results.json").read_text())
        assert "bquery_normalized" not in g2_saved[0]


# ---------------------------------------------------------------------------
# 3. document_fetch.py wiring — _splice_normalized_bquery + _classify_record
# ---------------------------------------------------------------------------

class TestSpliceNormalizedBquery:
    """Unit tests for document_fetch._splice_normalized_bquery."""

    def test_replaces_bquery_in_legacy_login_aspx_url(self) -> None:
        """The normalized text replaces the bquery param in a login.aspx URL."""
        from adapters.document_fetch import _splice_normalized_bquery

        url = "https://search.ebscohost.com/login.aspx?direct=true&bquery=Amazon+e-commerce"
        normalized = '("Amazon") AND "e-commerce"'
        result = _splice_normalized_bquery(url, normalized)

        import urllib.parse
        parsed = urllib.parse.urlparse(result)
        qs = urllib.parse.parse_qs(parsed.query)
        assert qs.get("bquery", [""])[0] == normalized
        # Other params must survive untouched.
        assert qs.get("direct", [""])[0] == "true"

    def test_no_op_for_non_login_aspx_url(self) -> None:
        """Non-EBSCO URLs pass through unchanged."""
        from adapters.document_fetch import _splice_normalized_bquery

        url = "https://www.jstor.org/search?q=test"
        result = _splice_normalized_bquery(url, "normalized")
        assert result == url

    def test_no_op_when_url_has_no_bquery_param(self) -> None:
        """login.aspx URLs without a bquery param are returned unchanged."""
        from adapters.document_fetch import _splice_normalized_bquery

        url = "https://search.ebscohost.com/login.aspx?direct=true"
        result = _splice_normalized_bquery(url, "normalized")
        assert result == url

    def test_no_op_for_already_direct_research_ebsco_url(self) -> None:
        """Already-direct research.ebsco.com URLs are not modified."""
        from adapters.document_fetch import _splice_normalized_bquery

        url = "https://research.ebsco.com/c/6hfcoc/search/results?q=test&db=asn"
        result = _splice_normalized_bquery(url, "normalized")
        assert result == url


class TestClassifyRecordWithNormalizedBquery:
    """Integration tests: when bquery_normalized is set on a record, the
    seed URL returned in the FetchItem carries the normalized query text."""

    def _ebsco_seed_record(self, bquery: str = "Amazon e-commerce", **extra: Any) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "title": "ebsco search",
            "url": (
                "https://search.ebscohost.com/login.aspx?direct=true"
                f"&bquery={bquery.replace(' ', '+')}"
            ),
            "bquery": bquery,
            "quality_label": "seed",
        }
        rec.update(extra)
        return rec

    def test_seed_url_uses_normalized_bquery_when_present(self, tmp_path: Path) -> None:
        """When bquery_normalized is set, _classify_record uses it in the URL."""
        from adapters.document_fetch import _classify_record
        import urllib.parse

        normalized = '("Amazon" OR "Amazon.com") AND "e-commerce"'
        rec = self._ebsco_seed_record(bquery_normalized=normalized)
        item = _classify_record(rec, "AUTO-01-G1", "ebsco_api", tmp_path, skip_already_fetched=False)

        assert item is not None
        assert item.fetch_type == "seed"
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(item.url).query)
        assert qs.get("bquery", [""])[0] == normalized

    def test_seed_url_uses_original_bquery_when_normalized_absent(self, tmp_path: Path) -> None:
        """Without bquery_normalized, the original URL is used unchanged."""
        from adapters.document_fetch import _classify_record

        raw = "Amazon e-commerce"
        rec = self._ebsco_seed_record(bquery=raw)  # no bquery_normalized field
        item = _classify_record(rec, "AUTO-01-G1", "ebsco_api", tmp_path, skip_already_fetched=False)

        assert item is not None
        # The URL should still contain the original form-encoded query.
        assert "Amazon+e-commerce" in item.url

    def test_downstream_rewrite_uses_normalized_bquery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: bquery_normalized in JSON → splice in _classify_record →
        _rewrite_ebsco_url_if_configured builds a well-formed research.ebsco.com URL."""
        from adapters.document_fetch import _classify_record, _rewrite_ebsco_url_if_configured
        import urllib.parse

        monkeypatch.setenv("ORCH_EBSCO_OPID", "6hfcoc")
        monkeypatch.setenv("ORCH_EBSCO_DB", "asn,bsu")

        normalized = '("Amazon" OR "Amazon.com") AND "e-commerce"'
        rec = self._ebsco_seed_record(bquery_normalized=normalized)
        item = _classify_record(rec, "AUTO-01-G1", "ebsco_api", tmp_path, skip_already_fetched=False)
        assert item is not None

        final_url = _rewrite_ebsco_url_if_configured(item.url)

        # Final URL must be a direct research.ebsco.com URL.
        assert final_url.startswith("https://research.ebsco.com/c/6hfcoc/search/results?")
        # The normalized query text must survive the whole pipeline.
        parsed = urllib.parse.urlparse(final_url)
        qs = urllib.parse.parse_qs(parsed.query)
        assert qs.get("q", [""])[0] == normalized
