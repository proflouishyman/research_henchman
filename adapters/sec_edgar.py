"""Thin client for SEC EDGAR's free public API.

No auth is required, but SEC mandates a descriptive User-Agent identifying
the requesting party (per https://www.sec.gov/os/accessing-edgar-data).
We pass ``Research Henchman <lhyman6@jh.edu>`` by default but accept an
override at the call sites so the same module can be used from tests or
re-purposed.

Public surface (consumed by ``layers/pull_dispatch.py`` and
``scripts/pull_gap_tree.py``):

  - ``lookup_cik(name, *, user_agent=DEFAULT_UA, cache_path=None)``
        Returns the 10-digit zero-padded CIK string, or None.
  - ``list_filings(cik, form_types, *, limit=10, user_agent=DEFAULT_UA)``
        Returns a list of dicts: accession_number, form, filing_date,
        report_date, primary_document, primary_doc_url. Newest first.
  - ``fetch_filing_text(filing, *, max_chars=200_000, user_agent=DEFAULT_UA)``
        Returns plain text (HTML stripped) — '' on 404 / error.

Rate limiting: SEC asks for ≤10 requests/second. We sleep 100 ms between
requests in this module. Callers running many concurrent lookups should
serialize through this module's helpers.

Caching: the company-tickers JSON (~1.5 MB) is cached on disk on first
call to avoid hammering SEC for repeat lookups. Default cache path is
``data/sec_edgar/company_tickers.json`` (under the project root); a
custom path can be provided for tests.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SEC asks every automated client to identify itself with a contact email.
DEFAULT_USER_AGENT = "Research Henchman <lhyman6@jh.edu>"

# 100 ms throttle between requests — SEC's published ceiling is 10 req/s.
_RATE_LIMIT_SLEEP = 0.1

# Resolved relative to this module so tests can override via *cache_path*.
_DEFAULT_CACHE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "sec_edgar" / "company_tickers.json"
)

_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
# Filing primary docs live under www.sec.gov/Archives/edgar/data/<int_cik>/<acc_no_dashes_stripped>/<doc>
_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{int_cik}/{acc_nodash}/{doc}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_get(url: str, user_agent: str, *, timeout: int = 30) -> bytes:
    """GET *url* with the SEC-required User-Agent. Returns response bytes.

    Sleeps :data:`_RATE_LIMIT_SLEEP` *after* the request returns so chains of
    calls in the same Python process stay under SEC's published 10 req/s
    ceiling. Raises ``urllib.error.HTTPError`` on non-2xx (caller decides
    how to handle 404 / 403).
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "identity",  # avoid gzip — easier on tests
            "Host": _host_for(url),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    finally:
        # Sleep regardless of success/failure so retries are throttled too.
        time.sleep(_RATE_LIMIT_SLEEP)
    return data


def _host_for(url: str) -> str:
    # Lightweight host extraction — SEC's three hostnames all behave the same.
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# CIK lookup
# ---------------------------------------------------------------------------


def _load_company_tickers(
    *, user_agent: str, cache_path: Optional[Path] = None
) -> Dict[str, dict]:
    """Return SEC's company-tickers JSON, fetching + caching on first call.

    The on-disk JSON is the raw SEC payload (a dict keyed by stringified
    integers); we don't transform it on save so the cache file matches what
    SEC serves. Schema (per row):
       {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
    """
    cache = cache_path if cache_path is not None else _DEFAULT_CACHE_PATH
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt cache — refetch.
            pass

    raw = _http_get(_COMPANY_TICKERS_URL, user_agent=user_agent)
    payload = json.loads(raw.decode("utf-8"))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def lookup_cik(
    name: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    cache_path: Optional[Path] = None,
) -> Optional[str]:
    """Return the 10-digit zero-padded CIK for *name*, or None.

    Match strategy: case-insensitive substring against each company's
    ``title``. When multiple rows match, pick the smallest CIK (older
    companies usually = the one users mean — e.g. "Amazon" matches
    "AMAZON.COM INC" over a later-incorporated subsidiary).
    """
    if not name or not name.strip():
        return None
    needle = name.strip().lower()
    payload = _load_company_tickers(user_agent=user_agent, cache_path=cache_path)

    matches: List[int] = []
    for row in payload.values():
        title = str(row.get("title", "")).lower()
        if needle in title:
            cik_int = int(row.get("cik_str", 0))
            if cik_int > 0:
                matches.append(cik_int)
    if not matches:
        return None
    return f"{min(matches):010d}"


# ---------------------------------------------------------------------------
# Filing list
# ---------------------------------------------------------------------------


def list_filings(
    cik: str,
    form_types: Iterable[str],
    *,
    limit: int = 10,
    user_agent: str = DEFAULT_USER_AGENT,
) -> List[Dict[str, str]]:
    """Return up to *limit* recent filings of *form_types* for *cik*.

    Each result dict has keys: accession_number, form, filing_date,
    report_date, primary_document, primary_doc_url. Newest first
    (SEC's recent submissions array is already in reverse-chronological
    order, so we just walk it and filter by form).
    """
    cik_padded = cik.lstrip("0").zfill(10)
    url = _SUBMISSIONS_URL.format(cik=cik_padded)
    raw = _http_get(url, user_agent=user_agent)
    body = json.loads(raw.decode("utf-8"))
    recent = (body.get("filings") or {}).get("recent") or {}

    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    filing_dates = recent.get("filingDate") or []
    report_dates = recent.get("reportDate") or []
    primary_docs = recent.get("primaryDocument") or []

    wanted = {f.strip() for f in form_types if f and str(f).strip()}
    out: List[Dict[str, str]] = []
    int_cik = str(int(cik_padded))  # archive URL drops leading zeros

    for i, form in enumerate(forms):
        if form not in wanted:
            continue
        acc = accessions[i] if i < len(accessions) else ""
        if not acc:
            continue
        primary = primary_docs[i] if i < len(primary_docs) else ""
        acc_nodash = acc.replace("-", "")
        primary_url = _ARCHIVE_URL.format(
            int_cik=int_cik, acc_nodash=acc_nodash, doc=primary
        ) if primary else ""
        out.append({
            "accession_number": acc,
            "form": form,
            "filing_date": filing_dates[i] if i < len(filing_dates) else "",
            "report_date": report_dates[i] if i < len(report_dates) else "",
            "primary_document": primary,
            "primary_doc_url": primary_url,
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Filing text fetch
# ---------------------------------------------------------------------------


class _HTMLTextStripper(HTMLParser):
    """Collect text-only content, dropping <script>/<style> bodies."""

    _SKIP_TAGS = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):  # noqa: D401, N802
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):  # noqa: D401, N802
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):  # noqa: D401, N802
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()


def _strip_html(html: str) -> str:
    stripper = _HTMLTextStripper()
    try:
        stripper.feed(html)
    except Exception:
        # Malformed HTML — fall back to a regex-based last resort so callers
        # still get *some* text rather than blowing up the dispatch loop.
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    return stripper.text


def fetch_filing_text(
    filing: Dict[str, str],
    *,
    max_chars: int = 200_000,
    user_agent: str = DEFAULT_USER_AGENT,
) -> str:
    """Download the primary document for *filing* and return plain text.

    Returns '' on 404 (filing pulled / withdrawn) or any other HTTP error
    so the caller can keep going with the rest of the gap. HTML documents
    are stripped to plain text; non-HTML primary docs (rare for 10-K /
    10-Q / S-1 / DEF 14A — those are usually .htm) are returned as-is
    decoded UTF-8 with surrogate-escape on bad bytes. Output is truncated
    to *max_chars*.
    """
    url = filing.get("primary_doc_url") or ""
    if not url:
        return ""
    try:
        raw = _http_get(url, user_agent=user_agent)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ""
        return ""
    except Exception:
        return ""

    text_raw = raw.decode("utf-8", errors="replace")
    primary = filing.get("primary_document", "").lower()
    if primary.endswith((".htm", ".html")) or "<html" in text_raw[:1000].lower():
        text = _strip_html(text_raw)
    else:
        text = text_raw

    if len(text) > max_chars:
        text = text[:max_chars]
    return text
