"""Resolve seed/provider-search URLs into pulled local artifacts."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .browser_client import BrowserClient, BrowserProvider, make_browser_client
from .cdp_utils import effective_cdp_url
from .io_utils import safe_query_token


def _default_browser_client() -> BrowserClient:
    """Build a BrowserClient from env, defaulting to playwright_cdp."""
    cdp_url = os.environ.get("ORCH_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222").strip()
    provider_raw = os.environ.get("ORCH_BROWSER_PROVIDER", "playwright_cdp").strip().lower()
    try:
        provider = BrowserProvider(provider_raw)
    except ValueError:
        provider = BrowserProvider.PLAYWRIGHT_CDP
    return BrowserClient(provider=provider, cdp_url=cdp_url, timeout_seconds=FETCH_TIMEOUT_SECONDS)


HREF_RE = re.compile(r"""href=["']([^"'#]+)["']""", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".rtf", ".html", ".htm"}
STATIC_ASSET_EXTENSIONS = {
    ".css",
    ".js",
    ".mjs",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".map",
    ".mp4",
    ".webm",
    ".mp3",
    ".wav",
    ".zip",
}
DISCOVERY_HINTS = {
    "article",
    "journal",
    "paper",
    "chapter",
    "content",
    "record",
    "detail",
    "document",
    "docview",
    "stable",
    "pdf",
}
MAX_FETCH_BYTES = 4_000_000
FETCH_TIMEOUT_SECONDS = 20
BLOCK_RULES = [
    (
        "captcha",
        (
            "captcha",
            "recaptcha",
            "hcaptcha",
            "verify you are human",
            "cf-chl",
            "cloudflare",
        ),
    ),
    (
        "verification_challenge",
        (
            "verification required",
            "please complete this challenge",
            "security challenge",
            "bot check",
        ),
    ),
    (
        "login_required",
        (
            "sign in",
            "log in",
            "login",
            "institutional login",
            "access through your institution",
            "authentication required",
            "login.aspx",
        ),
    ),
    (
        "access_denied",
        (
            "access denied",
            "forbidden",
            "not authorized",
            "permission denied",
        ),
    ),
]
BLOCK_REASON_HINTS = {
    "captcha": "Complete CAPTCHA in the signed-in browser session, then retry this run.",
    "verification_challenge": "Complete site verification challenge in-browser, then retry this run.",
    "login_required": "Sign in through your library/institution in-browser, then retry this run.",
    "access_denied": "Verify your institutional access/login and retry this run.",
}


def resolve_seed_rows(
    rows: List[Dict[str, str]],
    source_root: Path,
    source_id: str,
    query: str,
    gap_id: str,
    *,
    max_seed_urls: int = 2,
    max_child_links: int = 3,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Fetch provider-search seed URLs and emit local pulled-file rows."""

    urls = _seed_urls(rows)
    if not urls:
        return [], {"resolved_files": 0, "seed_urls": 0}

    out_root = source_root / "_resolved_urls" / safe_query_token(query)
    out_root.mkdir(parents=True, exist_ok=True)
    seen: Set[str] = set()
    resolved_rows: List[Dict[str, str]] = []
    resolved_files = 0

    for idx, url in enumerate(urls[:max_seed_urls], start=1):
        if url in seen:
            continue
        seen.add(url)
        parent_file, parent_excerpt, parent_block_reason = _fetch_and_save(url=url, out_root=out_root, prefix=f"seed_{idx:02d}")
        if parent_file is None:
            continue
        resolved_rows.append(
            _row_for_local_fetch(
                source_id,
                query,
                gap_id,
                parent_file,
                url,
                parent_excerpt,
                blocked_reason=parent_block_reason,
            )
        )
        resolved_files += 1

        if parent_block_reason:
            continue

        if parent_file.suffix.lower() in {".html", ".htm"}:
            html = parent_file.read_text(encoding="utf-8", errors="ignore")
            child_links = _extract_child_links(url, html)
            for cidx, child_url in enumerate(child_links[:max_child_links], start=1):
                if child_url in seen:
                    continue
                seen.add(child_url)
                child_file, child_excerpt, child_block_reason = _fetch_and_save(
                    url=child_url,
                    out_root=out_root,
                    prefix=f"seed_{idx:02d}_child_{cidx:02d}",
                )
                if child_file is None:
                    continue
                resolved_rows.append(
                    _row_for_local_fetch(
                        source_id,
                        query,
                        gap_id,
                        child_file,
                        child_url,
                        child_excerpt,
                        blocked_reason=child_block_reason,
                    )
                )
                resolved_files += 1

    blocked_files = sum(1 for row in resolved_rows if str(row.get("blocked_reason", "")).strip())
    return resolved_rows, {
        "resolved_files": resolved_files,
        "blocked_files": blocked_files,
        "captcha_blocks": sum(1 for row in resolved_rows if str(row.get("blocked_reason", "")) == "captcha"),
        "challenge_blocks": sum(
            1 for row in resolved_rows if str(row.get("blocked_reason", "")) == "verification_challenge"
        ),
        "login_blocks": sum(1 for row in resolved_rows if str(row.get("blocked_reason", "")) == "login_required"),
        "seed_urls": min(len(urls), max_seed_urls),
    }


def blocked_reason_hint(reason: str) -> str:
    """Return user-action hint for blocked retrieval pages."""

    return BLOCK_REASON_HINTS.get(str(reason or "").strip().lower(), "")


def probe_sign_in_access(url: str, *, browser_client: Optional[BrowserClient] = None) -> Dict[str, Any]:
    """Probe one provider URL and classify whether login access appears ready.

    Prefers CDP-backed fetch (respects authenticated session state).
    Falls back to HTTP for basic reachability diagnostics when CDP is unavailable.
    """
    target_url = str(url or "").strip()
    if not target_url.lower().startswith(("http://", "https://")):
        return {"status": "unreachable", "fetch_mode": "none", "error": "invalid url",
                "blocked_reason": "", "action_required": "", "excerpt": ""}

    client = browser_client or _default_browser_client()
    result = client.fetch(target_url)
    fetch_mode = client.provider.value

    if not result.content and result.error:
        # CDP failed; try plain HTTP
        http_client = BrowserClient(provider=BrowserProvider.HTTP, timeout_seconds=min(FETCH_TIMEOUT_SECONDS, 12))
        result = http_client.fetch(target_url)
        fetch_mode = "direct_http"

    if not result.content:
        return {
            "status": "unreachable",
            "fetch_mode": fetch_mode,
            "error": result.error or "fetch failed",
            "blocked_reason": "",
            "action_required": "",
            "excerpt": "",
        }

    suffix = _suffix_for_response(target_url, result.content_type, result.content)
    excerpt = _excerpt_from_bytes(result.content, suffix)
    raw_probe = result.content[:2048].decode("utf-8", errors="ignore")
    blocked_reason = _detect_block_reason(excerpt, raw_probe, target_url)
    action_required = blocked_reason_hint(blocked_reason) if blocked_reason else ""
    status = "blocked" if blocked_reason else "ok"
    return {
        "status": status,
        "fetch_mode": fetch_mode,
        "error": "",
        "blocked_reason": blocked_reason,
        "action_required": action_required,
        "excerpt": excerpt,
    }


def _seed_urls(rows: List[Dict[str, str]]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("url", "")).strip()
        if not raw.lower().startswith(("http://", "https://")):
            continue
        if raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def _suffix_for_response(url: str, content_type: str, body: bytes) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    if ext in DOC_EXTENSIONS:
        return ext
    if "pdf" in content_type:
        return ".pdf"
    if "html" in content_type or body.lstrip().startswith((b"<!doctype html", b"<html")):
        return ".html"
    if "text/plain" in content_type:
        return ".txt"
    return ".html"


def _fetch_and_save(
    url: str,
    out_root: Path,
    prefix: str,
    *,
    browser_client: Optional[BrowserClient] = None,
) -> Tuple[Path | None, str, str]:
    """Fetch one URL via BrowserClient (CDP → HTTP fallback) and save locally."""
    client = browser_client or _default_browser_client()

    # Try browser-backed fetch first (respects authenticated session)
    result = client.fetch(url)
    body: bytes | None = result.content if (result.content and not result.error) else None
    content_type = result.content_type

    # Fall back to direct HTTP if browser failed (and we haven't tried HTTP yet)
    if not body and client.provider != BrowserProvider.HTTP:
        http_client = BrowserClient(provider=BrowserProvider.HTTP, timeout_seconds=FETCH_TIMEOUT_SECONDS)
        http_result = http_client.fetch(url)
        if http_result.content:
            body = http_result.content
            content_type = http_result.content_type

    if not body:
        return None, "", ""

    suffix = _suffix_for_response(url, content_type, body)
    target = out_root / f"{prefix}{suffix}"
    target.write_bytes(body)
    excerpt = _excerpt_from_bytes(body, suffix)
    raw_probe = body[:2048].decode("utf-8", errors="ignore")
    block_reason = _detect_block_reason(excerpt, raw_probe, url)
    return target, excerpt, block_reason


def _fetch_via_cdp(url: str) -> str:
    """Backwards-compat shim. Delegates to BrowserClient."""
    client = _default_browser_client()
    result = client.fetch(url)
    if result.content and not result.error:
        return result.content.decode("utf-8", errors="ignore")
    return ""


def _excerpt_from_bytes(body: bytes, suffix: str, max_chars: int = 280) -> str:
    if suffix.lower() not in {".html", ".htm", ".txt"}:
        return ""
    text = body.decode("utf-8", errors="ignore")
    if suffix.lower() in {".html", ".htm"}:
        text = TAG_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text).strip()
    if not text:
        return ""
    return text[:max_chars].rstrip()


def _extract_child_links(base_url: str, html: str) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    base_netloc = urllib.parse.urlparse(base_url).netloc.lower()
    for raw in HREF_RE.findall(html or ""):
        full = urllib.parse.urljoin(base_url, raw.strip())
        parsed = urllib.parse.urlparse(full)
        if parsed.scheme not in {"http", "https"}:
            continue
        if not parsed.netloc:
            continue
        if base_netloc and parsed.netloc.lower() != base_netloc:
            continue
        ext = Path(parsed.path).suffix.lower()
        if ext in STATIC_ASSET_EXTENSIONS:
            continue
        text_blob = f"{parsed.path} {parsed.query}".lower()
        if ext and ext in DOC_EXTENSIONS:
            likely = True
        else:
            likely = any(hint in text_blob for hint in DISCOVERY_HINTS)
        if not likely:
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


def _row_for_local_fetch(
    source_id: str,
    query: str,
    gap_id: str,
    path: Path,
    source_url: str,
    excerpt: str,
    *,
    blocked_reason: str = "",
) -> Dict[str, str]:
    ext = path.suffix.lower()
    if blocked_reason:
        quality_label = "seed"
        quality_rank = "18"
    elif ext in {".pdf", ".doc", ".docx", ".txt", ".rtf"}:
        quality_label = "high"
        quality_rank = "92"
    else:
        quality_label = "medium"
        quality_rank = "72"

    row: Dict[str, str] = {
        "title": f"{source_id} pulled artifact {path.name}",
        "path": str(path),
        "query": query,
        "gap_id": gap_id,
        "source_url": source_url,
        "link_type": "resolved_snapshot",
        "quality_label": quality_label,
        "quality_rank": quality_rank,
    }
    if excerpt:
        row["excerpt"] = excerpt
    if blocked_reason:
        row["blocked_reason"] = blocked_reason
        hint = blocked_reason_hint(blocked_reason)
        if hint:
            row["action_required"] = hint
    return row


def _detect_block_reason(*texts: str) -> str:
    """Detect common CAPTCHA/login/access walls in fetched page content."""

    blob = " ".join(str(text or "") for text in texts).lower()
    for reason, needles in BLOCK_RULES:
        if any(needle in blob for needle in needles):
            return reason
    return ""
