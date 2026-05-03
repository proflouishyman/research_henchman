"""Tests for adapters/sec_edgar.py — mocked HTTP, no network calls."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from adapters import sec_edgar


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_SAMPLE_TICKERS = {
    "0": {"cik_str": 1018724, "ticker": "AMZN", "title": "AMAZON COM INC"},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "2": {"cik_str": 9999999, "ticker": "AMZX", "title": "Amazon Industrial Services"},
    "3": {"cik_str": 1411579, "ticker": "MELI", "title": "MercadoLibre, Inc."},
}


def _sample_submissions_payload() -> dict:
    """5 10-Ks, 1 10-Q, 1 8-K — used to verify form filtering + ordering."""
    forms = ["10-K", "10-Q", "10-K", "10-K", "8-K", "10-K", "10-K"]
    accs = [
        f"0000000000-{i:02d}-000000" for i in range(len(forms))
    ]
    filing_dates = [f"2024-{(i % 12) + 1:02d}-01" for i in range(len(forms))]
    report_dates = [f"2023-{(i % 12) + 1:02d}-31" for i in range(len(forms))]
    primary = [f"doc{i}.htm" for i in range(len(forms))]
    return {
        "filings": {
            "recent": {
                "form": forms,
                "accessionNumber": accs,
                "filingDate": filing_dates,
                "reportDate": report_dates,
                "primaryDocument": primary,
            }
        }
    }


@pytest.fixture
def fake_http(monkeypatch):
    """Patch ``adapters.sec_edgar._http_get`` with a registered URL → bytes map."""
    routes: dict = {}

    def _fake(url: str, user_agent: str, *, timeout: int = 30) -> bytes:
        if url in routes:
            return routes[url]
        # Default — pretend tickers JSON
        if url == sec_edgar._COMPANY_TICKERS_URL:
            return json.dumps(_SAMPLE_TICKERS).encode()
        raise AssertionError(f"unexpected URL in test: {url}")

    monkeypatch.setattr(sec_edgar, "_http_get", _fake)
    return routes


# ---------------------------------------------------------------------------
# CIK lookup
# ---------------------------------------------------------------------------


class TestLookupCik:
    def test_amazon_returns_smallest_cik(self, fake_http, tmp_path):
        # "Amazon" matches both AMAZON COM INC (1018724) and Amazon Industrial
        # Services (9999999). Lookup should pick the smaller CIK.
        cache = tmp_path / "tickers.json"
        cik = sec_edgar.lookup_cik(
            "Amazon", cache_path=cache,
        )
        assert cik == "0001018724"
        # Cache file written for reuse
        assert cache.exists()

    def test_case_insensitive(self, fake_http, tmp_path):
        cache = tmp_path / "tickers.json"
        assert sec_edgar.lookup_cik("apple", cache_path=cache) == "0000320193"

    def test_no_match_returns_none(self, fake_http, tmp_path):
        cache = tmp_path / "tickers.json"
        assert sec_edgar.lookup_cik("Nonexistent Co", cache_path=cache) is None

    def test_empty_string_returns_none(self, fake_http, tmp_path):
        cache = tmp_path / "tickers.json"
        assert sec_edgar.lookup_cik("", cache_path=cache) is None
        assert sec_edgar.lookup_cik("   ", cache_path=cache) is None

    def test_uses_cache_on_second_call(self, fake_http, tmp_path):
        cache = tmp_path / "tickers.json"
        # First call writes the cache.
        sec_edgar.lookup_cik("Amazon", cache_path=cache)
        # Mutate the cache file to a known different payload — second call
        # should read from disk, not refetch.
        cache.write_text(json.dumps(
            {"0": {"cik_str": 42, "ticker": "ZZZ", "title": "Cached Co"}}
        ))
        assert sec_edgar.lookup_cik("Cached", cache_path=cache) == "0000000042"


# ---------------------------------------------------------------------------
# list_filings
# ---------------------------------------------------------------------------


class TestListFilings:
    def test_returns_only_10k(self, fake_http):
        url = sec_edgar._SUBMISSIONS_URL.format(cik="0001018724")
        fake_http[url] = json.dumps(_sample_submissions_payload()).encode()
        out = sec_edgar.list_filings("0001018724", ["10-K"], limit=5)
        assert len(out) == 5  # the sample has 5 10-Ks
        for row in out:
            assert row["form"] == "10-K"
            assert row["accession_number"]
            assert row["primary_doc_url"].startswith(
                "https://www.sec.gov/Archives/edgar/data/1018724/"
            )

    def test_respects_limit(self, fake_http):
        url = sec_edgar._SUBMISSIONS_URL.format(cik="0001018724")
        fake_http[url] = json.dumps(_sample_submissions_payload()).encode()
        out = sec_edgar.list_filings("0001018724", ["10-K"], limit=2)
        assert len(out) == 2

    def test_multiple_form_types(self, fake_http):
        url = sec_edgar._SUBMISSIONS_URL.format(cik="0001018724")
        fake_http[url] = json.dumps(_sample_submissions_payload()).encode()
        out = sec_edgar.list_filings(
            "0001018724", ["10-K", "10-Q"], limit=10,
        )
        # 5 10-Ks + 1 10-Q
        forms = [r["form"] for r in out]
        assert forms.count("10-K") == 5
        assert forms.count("10-Q") == 1


# ---------------------------------------------------------------------------
# fetch_filing_text
# ---------------------------------------------------------------------------


class TestFetchFilingText:
    def test_strips_html(self, fake_http):
        filing = {
            "primary_doc_url": "https://example.com/doc.htm",
            "primary_document": "doc.htm",
        }
        fake_http[filing["primary_doc_url"]] = (
            b"<html><head><title>x</title></head>"
            b"<body><p>Hello <b>world</b></p>"
            b"<script>var bad = 1;</script></body></html>"
        )
        text = sec_edgar.fetch_filing_text(filing)
        assert "Hello world" in text
        assert "var bad" not in text

    def test_truncates_at_max_chars(self, fake_http):
        long_html = "<html><body>" + ("abc " * 100_000) + "</body></html>"
        filing = {
            "primary_doc_url": "https://example.com/long.htm",
            "primary_document": "long.htm",
        }
        fake_http[filing["primary_doc_url"]] = long_html.encode()
        text = sec_edgar.fetch_filing_text(filing, max_chars=500)
        assert len(text) <= 500

    def test_404_returns_empty(self, monkeypatch):
        import urllib.error

        def _raise(url, user_agent, *, timeout=30):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        monkeypatch.setattr(sec_edgar, "_http_get", _raise)
        text = sec_edgar.fetch_filing_text(
            {"primary_doc_url": "https://example.com/x", "primary_document": "x.htm"}
        )
        assert text == ""

    def test_empty_url_returns_empty(self):
        # No primary_doc_url at all — must return '' without raising.
        assert sec_edgar.fetch_filing_text({"primary_document": ""}) == ""
