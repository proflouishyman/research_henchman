"""Tests for adapters/internet_archive.py — HTTP-mocked IA client.

Uses unittest.mock to patch urllib.request.urlopen so no real network calls
are made. Tests cover search result parsing, item_metadata, and download_url.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from adapters.internet_archive import (
    _flat,
    download_url,
    item_metadata,
    search,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(payload: dict | list, status: int = 200) -> MagicMock:
    """Build a mock response whose .read() returns JSON-encoded *payload*."""
    raw = json.dumps(payload).encode()
    resp = MagicMock()
    resp.read.return_value = raw
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------

class TestSearch:
    """Unit tests for adapters.internet_archive.search."""

    def test_returns_empty_on_http_error(self):
        """Network failure → empty list (not an exception)."""
        import urllib.error
        with patch("adapters.internet_archive._get_json", side_effect=urllib.error.URLError("timeout")):
            result = search("mail order catalog")
        assert result == []

    def test_parses_results_correctly(self):
        """Happy path: API response is mapped to normalised result dicts."""
        fake = {
            "response": {
                "docs": [
                    {
                        "identifier": "sears_catalog_1908",
                        "title": "Sears Roebuck Catalog 1908",
                        "creator": "Sears, Roebuck and Co.",
                        "date": "1908",
                        "description": "The great price book.",
                        "downloads": 1523,
                        "format": ["PDF", "Text"],
                    },
                    {
                        "identifier": "ward_1910",
                        "title": "Montgomery Ward 1910",
                        "creator": None,
                        "date": "1910",
                        "description": None,
                        "downloads": 0,
                        "format": None,
                    },
                ]
            }
        }
        with patch("adapters.internet_archive._get_json", return_value=fake):
            results = search("mail order catalog", limit=5)

        assert len(results) == 2

        r0 = results[0]
        assert r0["identifier"] == "sears_catalog_1908"
        assert r0["title"] == "Sears Roebuck Catalog 1908"
        assert r0["creator"] == "Sears, Roebuck and Co."
        assert r0["date"] == "1908"
        assert r0["downloads"] == 1523

        r1 = results[1]
        assert r1["identifier"] == "ward_1910"
        assert r1["downloads"] == 0
        assert r1["creator"] == ""  # None → ""

    def test_empty_docs_key(self):
        """Response with empty docs list → empty result."""
        fake = {"response": {"docs": []}}
        with patch("adapters.internet_archive._get_json", return_value=fake):
            result = search("obscure query")
        assert result == []

    def test_missing_response_key(self):
        """Malformed API response → empty result (no crash)."""
        with patch("adapters.internet_archive._get_json", return_value={}):
            result = search("query")
        assert result == []


# ---------------------------------------------------------------------------
# item_metadata()
# ---------------------------------------------------------------------------

class TestItemMetadata:
    """Unit tests for adapters.internet_archive.item_metadata."""

    def test_returns_dict_on_success(self):
        """Happy path: returns the raw dict from the metadata API."""
        fake = {
            "metadata": {"title": "Sears Catalog"},
            "files": [{"name": "sears_catalog.pdf", "format": "PDF"}],
        }
        with patch("adapters.internet_archive._get_json", return_value=fake):
            result = item_metadata("sears_catalog_1908")
        assert result["metadata"]["title"] == "Sears Catalog"
        assert len(result["files"]) == 1

    def test_returns_empty_dict_on_error(self):
        """Network error → empty dict (not an exception)."""
        with patch("adapters.internet_archive._get_json", side_effect=Exception("err")):
            result = item_metadata("bad_id")
        assert result == {}


# ---------------------------------------------------------------------------
# download_url()
# ---------------------------------------------------------------------------

class TestDownloadUrl:
    """Unit tests for adapters.internet_archive.download_url."""

    def test_returns_pdf_url_when_file_found(self):
        """PDF file present in metadata → direct download URL returned."""
        fake_meta = {
            "files": [
                {"name": "sears_1908_text.pdf", "format": "Text PDF"},
                {"name": "sears_1908.djvu", "format": "DjVu"},
            ]
        }
        with patch("adapters.internet_archive.item_metadata", return_value=fake_meta):
            url = download_url("sears_catalog_1908")
        # Should prefer the _text.pdf variant.
        assert url is not None
        assert "sears_1908_text.pdf" in url
        assert url.startswith("https://archive.org/download/sears_catalog_1908/")

    def test_returns_none_when_no_pdf_but_files_exist(self):
        """Item has files but no PDF → None."""
        fake_meta = {"files": [{"name": "sears.djvu", "format": "DjVu"}]}
        with patch("adapters.internet_archive.item_metadata", return_value=fake_meta):
            url = download_url("sears_catalog_1908")
        assert url is None

    def test_returns_canonical_guess_when_no_files(self):
        """No files in metadata (empty) → falls back to canonical URL guess."""
        fake_meta = {}
        with patch("adapters.internet_archive.item_metadata", return_value=fake_meta):
            url = download_url("some_identifier")
        assert url == "https://archive.org/download/some_identifier/some_identifier.pdf"

    def test_error_on_metadata_fetch_falls_back_to_canonical(self):
        """Metadata fetch error → canonical URL guess (no crash)."""
        with patch("adapters.internet_archive.item_metadata", return_value={}):
            url = download_url("error_id")
        # Should still return *something* rather than raising.
        assert url is not None


# ---------------------------------------------------------------------------
# _flat()
# ---------------------------------------------------------------------------

class TestFlat:
    def test_none_returns_empty_string(self):
        assert _flat(None) == ""

    def test_list_joined_with_semicolon(self):
        assert _flat(["a", "b"]) == "a; b"

    def test_string_passthrough(self):
        assert _flat("hello world") == "hello world"

    def test_empty_list(self):
        assert _flat([]) == ""
