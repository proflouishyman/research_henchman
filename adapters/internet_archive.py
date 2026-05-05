"""Internet Archive (archive.org) thin client adapter.

Provides search, item metadata, and direct PDF URL resolution against the
Internet Archive's publicly accessible JSON API. No authentication required.

Public API:
  search(query, *, mediatype='texts', limit=50) -> List[Dict]
  item_metadata(identifier)                     -> Dict
  download_url(identifier, format='pdf')        -> Optional[str]

Politeness: 200ms throttle between requests (IA is a non-profit archive).

Usage example::

    from adapters.internet_archive import search, download_url
    results = search("mail order catalog Sears", limit=25)
    for r in results:
        pdf = download_url(r['identifier'])
        print(r['title'], pdf)
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Archive.org advanced search endpoint — returns JSON.
_SEARCH_URL = "https://archive.org/advancedsearch.php"

# Metadata endpoint — returns rich item JSON (files list, metadata, etc.).
_METADATA_URL = "https://archive.org/metadata/{identifier}"

# User-Agent sent with all requests. IA's robots.txt is permissive but we
# identify ourselves politely.
_USER_AGENT = (
    "research-henchman/1.0 "
    "(academic writing assistant; lhyman6@jh.edu; "
    "github.com/research-henchman)"
)

# Minimum delay (seconds) between successive API calls. Archive.org is a
# non-profit; we stay well under their rate limits.
_THROTTLE_SECONDS = 0.2

_last_call: float = 0.0


def _throttle() -> None:
    """Sleep if necessary to respect the 200ms inter-request delay."""
    global _last_call
    elapsed = time.monotonic() - _last_call
    if elapsed < _THROTTLE_SECONDS:
        time.sleep(_THROTTLE_SECONDS - elapsed)
    _last_call = time.monotonic()


def _get_json(url: str, *, timeout: int = 20) -> Any:
    """Fetch *url* and parse the JSON response. Raises on HTTP error."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    _throttle()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        import json
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def search(
    query: str,
    *,
    mediatype: str = "texts",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Search Internet Archive and return up to *limit* result dicts.

    Fields returned per result (from the ``fl[]`` field list):
      identifier, title, creator, date, description, downloads, format

    Items with mediatype != *mediatype* are filtered by the API itself via
    the ``mediatype:texts`` fq parameter. Returns an empty list on any error.

    Parameters
    ----------
    query:
        Plain-language or Boolean IA search query.
    mediatype:
        Archive.org mediatype to restrict results to (default: 'texts').
    limit:
        Maximum number of results to return (max 200 per IA single page).
    """
    params = urllib.parse.urlencode(
        [
            ("q",       f"({query}) AND mediatype:{mediatype}"),
            ("fl[]",    "identifier"),
            ("fl[]",    "title"),
            ("fl[]",    "creator"),
            ("fl[]",    "date"),
            ("fl[]",    "description"),
            ("fl[]",    "downloads"),
            ("fl[]",    "format"),
            ("output",  "json"),
            ("rows",    str(min(limit, 200))),
            ("start",   "0"),
            ("sort[]",  "downloads desc"),  # popularity-sorted for quality bias
        ],
        doseq=True,
    )
    url = f"{_SEARCH_URL}?{params}"
    try:
        data = _get_json(url)
    except Exception:
        return []

    docs = (data.get("response") or {}).get("docs") or []
    results: List[Dict[str, Any]] = []
    for doc in docs:
        results.append({
            "identifier":   str(doc.get("identifier") or ""),
            "title":        str(doc.get("title") or ""),
            "creator":      _flat(doc.get("creator")),
            "date":         str(doc.get("date") or ""),
            "description":  _flat(doc.get("description")),
            "downloads":    int(doc.get("downloads") or 0),
            "format":       _flat(doc.get("format")),
        })
    return results


def item_metadata(identifier: str) -> Dict[str, Any]:
    """Return the full metadata dict for an IA identifier.

    The response includes ``metadata``, ``files``, ``server``, etc. Returns
    an empty dict on any error.
    """
    url = _METADATA_URL.format(identifier=urllib.parse.quote(identifier, safe=""))
    try:
        return _get_json(url) or {}
    except Exception:
        return {}


def download_url(identifier: str, format: str = "pdf") -> Optional[str]:
    """Return the direct download URL for the first file matching *format*.

    Constructs the canonical ``https://archive.org/download/<id>/<id>.<format>``
    URL if a PDF-format file is found in the item's files list. Returns None
    if no file in the requested format exists or on any error.

    Most text items on IA have a PDF either as the original upload or as a
    derived file (``_text.pdf`` or ``_orig.pdf``). We check both the item
    metadata files list (most accurate) and fall back to the canonical URL
    guess (cheaper when metadata fetch is unavailable).
    """
    try:
        meta = item_metadata(identifier)
    except Exception:
        meta = {}

    files = meta.get("files") or []
    fmt_lower = format.lower()

    # Prefer the smallest file matching the requested format (DjVu-derived
    # PDFs are large; text PDFs tend to be smaller and more useful).
    candidates: List[str] = []
    for f in files:
        fname = str(f.get("name") or "")
        if fname.lower().endswith(f".{fmt_lower}"):
            candidates.append(fname)

    if candidates:
        # Prefer the _text.pdf / shortest name first.
        candidates.sort(key=lambda n: (0 if "_text" in n else 1, len(n)))
        fname = candidates[0]
        return f"https://archive.org/download/{identifier}/{urllib.parse.quote(fname, safe='')}"

    # Fallback: construct the canonical URL and assume it exists.
    # IA serves 404 for non-existent files gracefully.
    if files:  # item exists but has no PDF
        return None

    # No metadata — best-effort canonical URL guess.
    return f"https://archive.org/download/{identifier}/{identifier}.{fmt_lower}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flat(val: Any) -> str:
    """Flatten a value that may be a list (IA multi-value fields) or string."""
    if val is None:
        return ""
    if isinstance(val, list):
        return "; ".join(str(v) for v in val if v)
    return str(val)
