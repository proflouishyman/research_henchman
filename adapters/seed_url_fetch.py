"""Resolve seed/provider-search URLs into pulled local artifacts."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from .cdp_utils import effective_cdp_url
from .io_utils import safe_query_token


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


def probe_sign_in_access(url: str) -> Dict[str, Any]:
    """Probe one provider URL and classify whether login access appears ready.

    Non-obvious logic:
    - For login readiness we prefer CDP-backed fetch first so existing
      authenticated browser session state is respected.
    - If CDP is unavailable, fall back to direct HTTP to still provide
      basic reachability diagnostics.
    """

    target_url = str(url or "").strip()
    if not target_url.lower().startswith(("http://", "https://")):
        return {
            "status": "unreachable",
            "fetch_mode": "none",
            "error": "invalid url",
            "blocked_reason": "",
            "action_required": "",
            "excerpt": "",
        }

    body: bytes | None = None
    content_type = ""
    fetch_mode = "none"
    error = ""

    html = _fetch_via_cdp(target_url)
    if html:
        body = html.encode("utf-8", errors="ignore")
        content_type = "text/html"
        fetch_mode = "cdp"

    if body is None:
        try:
            req = urllib.request.Request(
                target_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    )
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=min(FETCH_TIMEOUT_SECONDS, 12)) as resp:
                content_type = str(resp.headers.get("Content-Type", "")).lower()
                body = resp.read(MAX_FETCH_BYTES)
            fetch_mode = "direct_http"
        except Exception as exc:  # noqa: BLE001 - structured diagnostic response.
            error = str(exc)[:180]

    if body is None:
        return {
            "status": "unreachable",
            "fetch_mode": fetch_mode,
            "error": error or "fetch failed",
            "blocked_reason": "",
            "action_required": "",
            "excerpt": "",
        }

    suffix = _suffix_for_response(target_url, content_type, body)
    excerpt = _excerpt_from_bytes(body, suffix)
    raw_probe = body[:2048].decode("utf-8", errors="ignore")
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


def _fetch_and_save(url: str, out_root: Path, prefix: str) -> Tuple[Path | None, str, str]:
    body: bytes | None = None
    content_type = ""
    failed = False
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            content_type = str(resp.headers.get("Content-Type", "")).lower()
            body = resp.read(MAX_FETCH_BYTES)
    except (urllib.error.URLError, TimeoutError, OSError):
        failed = True
    except Exception:
        failed = True

    if failed or body is None:
        html = _fetch_via_cdp(url)
        if html:
            body = html.encode("utf-8", errors="ignore")
            content_type = "text/html"
        else:
            return None, "", ""

    suffix = _suffix_for_response(url, content_type, body)
    target = out_root / f"{prefix}{suffix}"
    target.write_bytes(body)
    excerpt = _excerpt_from_bytes(body, suffix)
    raw_probe = body[:2048].decode("utf-8", errors="ignore")
    block_reason = _detect_block_reason(excerpt, raw_probe, url)
    return target, excerpt, block_reason


def _fetch_via_cdp(url: str) -> str:
    """Best-effort browser-backed fetch using authenticated CDP session."""

    cdp_url = str(os.environ.get("ORCH_PLAYWRIGHT_CDP_URL", "")).strip()
    if not cdp_url:
        return ""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return ""

    timeout_ms = int(max(5, FETCH_TIMEOUT_SECONDS) * 1000)
    target_cdp_url = effective_cdp_url(cdp_url)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(target_cdp_url)
            had_contexts = bool(browser.contexts)
            context = browser.contexts[0] if had_contexts else browser.new_context()
            # Best effort: use a request context seeded from browser storage
            # state so authenticated pulls can run without opening/focusing tabs.
            try:
                request_ctx = pw.request.new_context(storage_state=context.storage_state())
                try:
                    response = request_ctx.get(url, timeout=timeout_ms)
                    body = response.text()
                    if body:
                        return str(body)
                finally:
                    request_ctx.dispose()
            except Exception:
                pass

            # Fallback: open a transient page only when request-context fetch fails.
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            html = page.content()
            page.close()
            # When attached via CDP, do not close the user browser session.
            # Close only temporary context if we had to create one.
            if not had_contexts:
                context.close()
            return str(html or "")
    except Exception:
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
