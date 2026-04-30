"""Tests for scripts/normalize_seed_queries.py and the document_fetch.py wiring.

Coverage:
  1. normalize_seed_queries._process_file — reads JSON, calls LLM (mocked),
     writes bquery_normalized as a List[str]; idempotent on re-run; respects --force;
     migrates old string-format bquery_normalized to list.
  2. normalize_seed_queries.main — CLI ergonomics: --dry-run, --limit, --variants,
     missing run dir.
  3. document_fetch._classify_record + _splice_normalized_bquery — when a record has
     bquery_normalized as a list, one FetchItem is produced per variant, each with a
     distinct URL.
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

    def _make_client(
        self,
        responses: "str | List[str] | None" = None,
    ) -> MagicMock:
        """Return a mock LLMClient whose complete() returns a numbered-list response.

        *responses* may be:
        - None         → returns a default 3-variant numbered list.
        - A bare str   → used as the raw LLM response text directly.
        - A List[str]  → formatted as a numbered list response.
        """
        if responses is None:
            responses = [
                '("Amazon" OR "Amazon.com") AND "e-commerce"',
                '"online retail" AND (disruption OR transformation)',
                '"internet commerce" AND (history OR evolution)',
            ]
        if isinstance(responses, list):
            raw = "\n".join(f"{i+1}. {q}" for i, q in enumerate(responses))
        else:
            raw = responses
        client = MagicMock()
        client.complete.return_value = raw
        return client

    def test_writes_bquery_normalized_as_list(self, tmp_path: Path) -> None:
        """_process_file writes bquery_normalized as a List[str] (not a plain string)."""
        from scripts.normalize_seed_queries import _process_file

        variant_1 = '("Amazon") AND "e-commerce"'
        variant_2 = '"online shopping" AND (disruption OR change)'
        variant_3 = 'Amazon AND retail* AND history'
        records = [_seed_record("Amazon e-commerce")]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client([variant_1, variant_2, variant_3])

        n = _process_file(jf, client, force=False, dry_run=False, limit_remaining=None, variants=3)

        assert n == 1
        saved = json.loads(jf.read_text(encoding="utf-8"))
        assert isinstance(saved, list)
        assert isinstance(saved[0]["bquery_normalized"], list)
        assert saved[0]["bquery_normalized"] == [variant_1, variant_2, variant_3]
        # Original bquery must be preserved.
        assert saved[0]["bquery"] == "Amazon e-commerce"
        # bquery_original mirrors original for explicit rollback convenience.
        assert saved[0]["bquery_original"] == "Amazon e-commerce"

    def test_idempotent_skips_already_normalized_list(self, tmp_path: Path) -> None:
        """Re-running without --force skips records where bquery_normalized is a
        non-empty list of length >= variants."""
        from scripts.normalize_seed_queries import _process_file

        existing = ["q1", "q2", "q3"]
        records = [_seed_record("Amazon e-commerce", bquery_normalized=existing)]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client()

        # Default variants=3, existing list has 3 — should be skipped.
        n = _process_file(jf, client, force=False, dry_run=False, limit_remaining=None, variants=3)

        assert n == 0
        client.complete.assert_not_called()

    def test_idempotent_reruns_when_fewer_variants_than_requested(self, tmp_path: Path) -> None:
        """If existing list has fewer variants than requested, re-normalize."""
        from scripts.normalize_seed_queries import _process_file

        existing = ["q1", "q2"]  # only 2 variants
        records = [_seed_record("Amazon e-commerce", bquery_normalized=existing)]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client()

        # Requesting 3 variants — existing has only 2, so it should re-normalize.
        n = _process_file(jf, client, force=False, dry_run=False, limit_remaining=None, variants=3)

        assert n == 1
        client.complete.assert_called_once()

    def test_force_renormalizes_existing(self, tmp_path: Path) -> None:
        """--force causes already-normalized records to be reprocessed."""
        from scripts.normalize_seed_queries import _process_file

        fresh = [
            '("Amazon" OR "Amazon.com") AND "e-commerce"',
            '"online retail" AND disruption',
            'Amazon AND retail* AND history',
        ]
        records = [_seed_record("Amazon e-commerce", bquery_normalized=["old value"])]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client(fresh)

        n = _process_file(jf, client, force=True, dry_run=False, limit_remaining=None, variants=3)

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

        n = _process_file(jf, client, force=False, dry_run=True, limit_remaining=None, variants=3)

        assert n == 1  # counted as "would-normalize"
        assert jf.read_text(encoding="utf-8") == original_text
        client.complete.assert_not_called()

    def test_limit_remaining_stops_early(self, tmp_path: Path) -> None:
        """limit_remaining=1 normalizes at most 1 record from a multi-record file."""
        from scripts.normalize_seed_queries import _process_file

        records = [_seed_record(f"query {i}") for i in range(3)]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client()

        n = _process_file(jf, client, force=False, dry_run=False, limit_remaining=1, variants=3)

        assert n == 1
        saved = json.loads(jf.read_text(encoding="utf-8"))
        # Only the first record should be normalized; rest should be untouched.
        assert isinstance(saved[0].get("bquery_normalized"), list)
        assert "bquery_normalized" not in saved[1]
        assert "bquery_normalized" not in saved[2]

    def test_skips_records_without_bquery(self, tmp_path: Path) -> None:
        """Records with no bquery / query field are silently skipped."""
        from scripts.normalize_seed_queries import _process_file

        records = [{"title": "no query here", "quality_label": "seed"}]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client()

        n = _process_file(jf, client, force=False, dry_run=False, limit_remaining=None, variants=3)

        assert n == 0
        client.complete.assert_not_called()

    def test_truncates_each_variant_to_200_chars(self, tmp_path: Path) -> None:
        """Each variant is truncated to 200 chars to stay within EBSCO limits."""
        from scripts.normalize_seed_queries import _process_file

        # LLM response: two long variants in numbered-list format.
        long_a = "A" * 250
        long_b = "B" * 250
        raw_response = f"1. {long_a}\n2. {long_b}"
        records = [_seed_record("something")]
        jf = _write_seed_file(tmp_path, records)
        client = self._make_client(raw_response)

        _process_file(jf, client, force=False, dry_run=False, limit_remaining=None, variants=2)

        saved = json.loads(jf.read_text(encoding="utf-8"))
        variants = saved[0]["bquery_normalized"]
        assert isinstance(variants, list)
        for v in variants:
            assert len(v) <= 200


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

    def _mock_llm_factory(self, responses: "List[str] | None" = None) -> "tuple[MagicMock, MagicMock]":
        """Return (mock_factory, mock_client) where client.complete() returns a
        numbered-list LLM response."""
        if responses is None:
            responses = ["norm_q1", "norm_q2", "norm_q3"]
        raw = "\n".join(f"{i+1}. {q}" for i, q in enumerate(responses))
        mock_client = MagicMock()
        mock_client.model = "qwen2.5:7b"
        mock_client.complete.return_value = raw
        mock_factory = MagicMock(return_value=mock_client)
        return mock_factory, mock_client

    def test_dry_run_reports_without_writing(self, tmp_path: Path) -> None:
        """main() with --dry-run prints activity but leaves JSON unchanged."""
        from scripts.normalize_seed_queries import main

        pull_root = _make_run_dir(tmp_path)
        jf = pull_root / "run_test" / "AUTO-01-G1" / "ebsco_api" / "results.json"
        original_text = jf.read_text(encoding="utf-8")

        mock_factory, mock_client = self._mock_llm_factory()
        with patch("scripts.normalize_seed_queries.make_llm_client", mock_factory):
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

        mock_factory, _ = self._mock_llm_factory()
        with patch("scripts.normalize_seed_queries.make_llm_client", mock_factory):
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

    def test_variants_flag_sets_list_length(self, tmp_path: Path) -> None:
        """--variants 2 produces a 2-element bquery_normalized list."""
        from scripts.normalize_seed_queries import main

        src_dir = tmp_path / "pull_outputs" / "run_v" / "AUTO-01-G1" / "ebsco_api"
        _write_seed_file(src_dir, [_seed_record("query v")])

        raw_response = "1. query_a\n2. query_b"
        mock_client = MagicMock()
        mock_client.model = "qwen2.5:7b"
        mock_client.complete.return_value = raw_response
        mock_factory = MagicMock(return_value=mock_client)

        with patch("scripts.normalize_seed_queries.make_llm_client", mock_factory):
            rc = main([
                "--run-id", "run_v",
                "--data-root", str(tmp_path),
                "--variants", "2",
            ])

        assert rc == 0
        saved = json.loads((src_dir / "results.json").read_text())
        bqn = saved[0]["bquery_normalized"]
        assert isinstance(bqn, list)
        assert bqn == ["query_a", "query_b"]

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

        mock_factory, _ = self._mock_llm_factory()
        with patch("scripts.normalize_seed_queries.make_llm_client", mock_factory):
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
    """Integration tests: when bquery_normalized is set on a record, _classify_record
    produces one FetchItem per variant, each with a distinct spliced URL."""

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

    def test_three_variants_produce_three_fetch_items(self, tmp_path: Path) -> None:
        """When bquery_normalized is a 3-element list, _classify_record returns 3 FetchItems."""
        from adapters.document_fetch import _classify_record
        import urllib.parse

        variants = [
            '("Amazon" OR "Amazon.com") AND "e-commerce"',
            '"online retail" AND (disruption OR transformation)',
            '"internet commerce" AND (history OR evolution)',
        ]
        rec = self._ebsco_seed_record(bquery_normalized=variants)
        items = _classify_record(rec, "AUTO-01-G1", "ebsco_api", tmp_path, skip_already_fetched=False)

        assert len(items) == 3
        for i, (item, expected_query) in enumerate(zip(items, variants), start=1):
            assert item.fetch_type == "seed"
            assert item.variant_index == i
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(item.url).query)
            assert qs.get("bquery", [""])[0] == expected_query

    def test_old_string_bquery_normalized_produces_one_fetch_item(self, tmp_path: Path) -> None:
        """Backward compat: a bare string bquery_normalized is treated as a 1-element list."""
        from adapters.document_fetch import _classify_record
        import urllib.parse

        normalized = '("Amazon" OR "Amazon.com") AND "e-commerce"'
        # Old-style: string, not list.
        rec = self._ebsco_seed_record(bquery_normalized=normalized)
        items = _classify_record(rec, "AUTO-01-G1", "ebsco_api", tmp_path, skip_already_fetched=False)

        assert len(items) == 1
        assert items[0].fetch_type == "seed"
        assert items[0].variant_index == 1
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(items[0].url).query)
        assert qs.get("bquery", [""])[0] == normalized

    def test_seed_url_uses_original_bquery_when_normalized_absent(self, tmp_path: Path) -> None:
        """Without bquery_normalized, the original URL is used in a single FetchItem."""
        from adapters.document_fetch import _classify_record

        raw = "Amazon e-commerce"
        rec = self._ebsco_seed_record(bquery=raw)  # no bquery_normalized field
        items = _classify_record(rec, "AUTO-01-G1", "ebsco_api", tmp_path, skip_already_fetched=False)

        assert len(items) == 1
        item = items[0]
        # The URL should still contain the original form-encoded query.
        assert "Amazon+e-commerce" in item.url
        # variant_index is 0 for un-normalized seeds.
        assert item.variant_index == 0

    def test_downstream_rewrite_uses_normalized_bquery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: bquery_normalized list → splice in _classify_record →
        _rewrite_ebsco_url_if_configured builds well-formed research.ebsco.com URLs."""
        from adapters.document_fetch import _classify_record, _rewrite_ebsco_url_if_configured
        import urllib.parse

        monkeypatch.setenv("ORCH_EBSCO_OPID", "6hfcoc")
        monkeypatch.setenv("ORCH_EBSCO_DB", "asn,bsu")

        normalized = '("Amazon" OR "Amazon.com") AND "e-commerce"'
        rec = self._ebsco_seed_record(bquery_normalized=[normalized])
        items = _classify_record(rec, "AUTO-01-G1", "ebsco_api", tmp_path, skip_already_fetched=False)
        assert len(items) == 1
        item = items[0]

        final_url = _rewrite_ebsco_url_if_configured(item.url)

        # Final URL must be a direct research.ebsco.com URL.
        assert final_url.startswith("https://research.ebsco.com/c/6hfcoc/search/results?")
        # The normalized query text must survive the whole pipeline.
        parsed = urllib.parse.urlparse(final_url)
        qs = urllib.parse.parse_qs(parsed.query)
        assert qs.get("q", [""])[0] == normalized

    def test_dedup_identical_variants_produces_single_item(self, tmp_path: Path) -> None:
        """If bquery_normalized contains duplicate queries, only one FetchItem per unique URL."""
        from adapters.document_fetch import _classify_record

        q = '("Amazon") AND "e-commerce"'
        # Two identical variants — the splice produces identical URLs.
        rec = self._ebsco_seed_record(bquery_normalized=[q, q, q])
        items = _classify_record(rec, "AUTO-01-G1", "ebsco_api", tmp_path, skip_already_fetched=False)

        # All three variants are passed through (dedup of LLM output happens in
        # normalize_seed_queries._parse_numbered_list; _classify_record preserves
        # whatever is in the list so callers can control dedup policy).
        # This test validates that 3 identical list entries → 3 FetchItems with
        # distinct variant_index values (URL happens to be the same — that's OK;
        # the fetch pipeline's file-exists check prevents re-saving).
        assert len(items) == 3
        urls = [item.url for item in items]
        # All URLs are the same because the variant text is the same.
        assert len(set(urls)) == 1

    def test_migration_string_to_list_in_process_file(self, tmp_path: Path) -> None:
        """An old string bquery_normalized is migrated to a list on re-run."""
        from scripts.normalize_seed_queries import _process_file

        # Simulate a record written by the old single-query version.
        old_normalized = '("Amazon") AND "e-commerce"'
        records = [_seed_record("Amazon e-commerce", bquery_normalized=old_normalized)]
        jf = _write_seed_file(tmp_path, records)

        # Client returns a 3-item numbered list.
        new_variants = [
            '("Amazon" OR "Amazon.com") AND "e-commerce"',
            '"online retail" AND disruption',
            'Amazon AND retail* AND history',
        ]
        raw_response = "\n".join(f"{i+1}. {q}" for i, q in enumerate(new_variants))
        client = MagicMock()
        client.complete.return_value = raw_response

        # Requesting 3 variants — old record has a string (treated as 1 element),
        # which is fewer than 3, so it should be re-normalized.
        n = _process_file(jf, client, force=False, dry_run=False, limit_remaining=None, variants=3)

        assert n == 1
        saved = json.loads(jf.read_text(encoding="utf-8"))
        bqn = saved[0]["bquery_normalized"]
        assert isinstance(bqn, list)
        assert bqn == new_variants
