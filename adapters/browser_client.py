"""Browser client abstraction for Research Henchman.

Supported providers:
  playwright_cdp  (default) — Chrome + CDP attach via Playwright
  http                      — urllib: static pages, no JS; works without browser
  claude_cu                 — STUB: Anthropic Computer Use API (future)

Select via ORCH_BROWSER_PROVIDER env var.

All providers expose the same interface so adapters and main.py are
provider-agnostic. Swapping from playwright_cdp to claude_cu (once live)
is a config-level change only — no adapter code changes needed.

Key types:
    PageResult   — response envelope with blocked-page detection
    BrowserClient — fetch(), probe_login(), open_tabs(), is_available()

Usage (adapters):
    client = make_browser_client(settings)
    if not client.is_available():
        raise RuntimeError("browser unavailable")
    result = client.fetch("https://example.com")
    if result.blocked:
        # surface action_required to UI
        ...
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class BrowserProvider(str, Enum):
    PLAYWRIGHT_CDP = "playwright_cdp"
    HTTP           = "http"
    CLAUDE_CU      = "claude_cu"   # stub — Anthropic Computer Use API


@dataclass
class PageResult:
    """Envelope for one browser fetch."""

    url: str
    status_code: int
    content: bytes
    content_type: str
    blocked: bool = False
    blocked_reason: str = ""    # captcha | login | access_denied | timeout
    action_required: str = ""   # human-readable hint for UI
    error: str = ""
    elapsed_ms: int = 0


@dataclass
class BrowserClient:
    """Provider-agnostic browser client."""

    provider: BrowserProvider
    cdp_url: str = "http://127.0.0.1:9222"
    timeout_seconds: int = 30
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    def is_available(self) -> bool:
        """Return True if the provider is reachable and usable."""
        if self.provider == BrowserProvider.HTTP:
            return True
        if self.provider == BrowserProvider.PLAYWRIGHT_CDP:
            return self._playwright_cdp_ping()
        if self.provider == BrowserProvider.CLAUDE_CU:
            return self._claude_cu_available()
        return False

    def unavailable_reason(self) -> str:
        """Human-readable reason when is_available() is False."""
        if self.provider == BrowserProvider.HTTP:
            return ""
        if self.provider == BrowserProvider.PLAYWRIGHT_CDP:
            if not self._playwright_cdp_ping():
                return f"playwright_cdp_unreachable: no browser at {self.cdp_url}"
            return ""
        if self.provider == BrowserProvider.CLAUDE_CU:
            if not self._claude_cu_available():
                return "claude_cu_not_implemented: provider is a future stub"
            return ""
        return f"unknown_provider: {self.provider}"

    def fetch(
        self,
        url: str,
        *,
        context_page: Any = None,
        follow_redirects: bool = True,
    ) -> PageResult:
        """Fetch a URL and return a PageResult with blocked-page detection."""
        start = time.monotonic()
        try:
            if self.provider == BrowserProvider.PLAYWRIGHT_CDP:
                result = self._playwright_fetch(url, context_page=context_page)
            elif self.provider == BrowserProvider.HTTP:
                result = self._http_fetch(url)
            elif self.provider == BrowserProvider.CLAUDE_CU:
                result = self._claude_cu_fetch(url)
            else:
                result = PageResult(
                    url=url, status_code=0, content=b"", content_type="",
                    blocked=True, blocked_reason="unsupported_provider",
                    error=f"unsupported provider: {self.provider}",
                )
            result.elapsed_ms = int((time.monotonic() - start) * 1000)
            if not result.blocked:
                result.blocked, result.blocked_reason, result.action_required = (
                    _detect_blocked(result.content, result.url)
                )
            return result
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return PageResult(
                url=url, status_code=0, content=b"", content_type="",
                blocked=True, blocked_reason="timeout",
                action_required="Check network or browser connection",
                error=str(exc)[:200],
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return PageResult(
                url=url, status_code=0, content=b"", content_type="",
                blocked=True, blocked_reason="error",
                error=str(exc)[:200],
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )

    def probe_login(self, url: str) -> Dict[str, Any]:
        """Check whether a provider URL is accessible or blocked behind a login wall."""
        result = self.fetch(url)
        if result.error and not result.content:
            return {
                "status": "unreachable",
                "url": url,
                "blocked_reason": result.blocked_reason or "network_error",
                "action_required": result.action_required or "Check network connection",
                "error": result.error,
            }
        if result.blocked:
            return {
                "status": "blocked",
                "url": url,
                "blocked_reason": result.blocked_reason,
                "action_required": result.action_required,
                "excerpt": result.content[:500].decode("utf-8", errors="ignore"),
            }
        return {
            "status": "ok",
            "url": url,
            "blocked_reason": "",
            "action_required": "",
        }

    def open_tabs(self, urls: List[str]) -> Dict[str, Any]:
        """Open URLs as tabs in the browser session (CDP only; HTTP is a no-op)."""
        clean = [u.strip() for u in urls if u.strip().lower().startswith(("http://", "https://"))]
        if not clean:
            return {"opened": 0, "opened_urls": []}

        if self.provider == BrowserProvider.PLAYWRIGHT_CDP:
            return self._playwright_open_tabs(clean)
        if self.provider == BrowserProvider.CLAUDE_CU:
            return self._claude_cu_open_tabs(clean)
        # HTTP provider: silently skip (no interactive browser)
        return {"opened": 0, "opened_urls": [], "note": "http_provider_no_tabs"}

    # ------------------------------------------------------------------
    # Playwright / CDP backend
    # ------------------------------------------------------------------

    def _playwright_cdp_ping(self) -> bool:
        """Quick check whether the CDP endpoint responds."""
        try:
            req = urllib.request.Request(
                f"{self.cdp_url.rstrip('/')}/json/version",
                headers={"Host": _cdp_host(self.cdp_url)},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _playwright_fetch(self, url: str, *, context_page: Any = None) -> PageResult:
        """Fetch via Playwright CDP session using background request if possible."""
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise RuntimeError("playwright not installed") from exc

        from adapters.cdp_utils import effective_cdp_url  # local import to avoid circulars
        effective = effective_cdp_url(self.cdp_url)

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(effective)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            try:
                # Background request (no visible page — avoids focus stealing)
                api_req = ctx.request
                resp = api_req.get(
                    url,
                    headers={"User-Agent": self.user_agent},
                    timeout=self.timeout_seconds * 1000,
                )
                content = resp.body()
                ct = resp.headers.get("content-type", "")
                return PageResult(
                    url=url,
                    status_code=resp.status,
                    content=content,
                    content_type=ct,
                )
            except Exception:
                # Fall through to full page load
                pass

            page = ctx.new_page()
            try:
                page.goto(url, timeout=self.timeout_seconds * 1000)
                content = page.content().encode("utf-8", errors="ignore")
                ct = "text/html"
                return PageResult(url=url, status_code=200, content=content, content_type=ct)
            finally:
                page.close()

    def _playwright_open_tabs(self, urls: List[str]) -> Dict[str, Any]:
        """Open URLs as visible tabs in the CDP browser."""
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            return {"opened": 0, "opened_urls": [], "error": "playwright_not_installed"}

        from adapters.cdp_utils import effective_cdp_url
        effective = effective_cdp_url(self.cdp_url)

        opened_urls: List[str] = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(effective)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                for url in urls:
                    try:
                        page = ctx.new_page()
                        page.goto(url, timeout=30_000)
                        opened_urls.append(url)
                    except Exception:
                        pass
        except Exception as exc:
            return {"opened": len(opened_urls), "opened_urls": opened_urls, "error": str(exc)[:200]}
        return {"opened": len(opened_urls), "opened_urls": opened_urls}

    # ------------------------------------------------------------------
    # HTTP backend
    # ------------------------------------------------------------------

    def _http_fetch(self, url: str) -> PageResult:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            ct = str(resp.headers.get("Content-Type", "")).lower()
            content = resp.read(4_000_000)
        return PageResult(
            url=url,
            status_code=resp.status,
            content=content,
            content_type=ct,
        )

    # ------------------------------------------------------------------
    # Claude Computer Use backend (stub — future Anthropic integration)
    # ------------------------------------------------------------------

    def _claude_cu_available(self) -> bool:
        """Claude CU is not yet implemented; always returns False."""
        return False

    def _claude_cu_fetch(self, url: str) -> PageResult:
        """Placeholder for Anthropic Computer Use API browser fetch.

        When implemented, this will:
        1. Launch a headless desktop via the Anthropic Computer Use API
        2. Navigate to `url` in a browser window
        3. Handle login walls, CAPTCHAs, and JS-rendered content natively
        4. Extract text/HTML content and return as PageResult

        This is the primary path for gated academic databases (JSTOR, ProQuest)
        that block headless browsers but can be navigated by Claude CU.
        """
        return PageResult(
            url=url, status_code=0, content=b"", content_type="",
            blocked=True, blocked_reason="not_implemented",
            action_required="Claude Computer Use provider is not yet implemented",
            error="claude_cu_stub",
        )

    def _claude_cu_open_tabs(self, urls: List[str]) -> Dict[str, Any]:
        return {"opened": 0, "opened_urls": [], "error": "claude_cu_not_implemented"}


# ------------------------------------------------------------------
# Blocked-page detection
# ------------------------------------------------------------------

_BLOCK_SIGNALS = [
    (re.compile(r"access\s+denied", re.IGNORECASE), "access_denied", "Request access through your institution"),
    (re.compile(r"captcha", re.IGNORECASE), "captcha", "Complete CAPTCHA in browser"),
    (re.compile(r"please\s+(log|sign)\s*in", re.IGNORECASE), "login", "Sign in at provider"),
    (re.compile(r"authentication\s+required", re.IGNORECASE), "login", "Sign in at provider"),
    (re.compile(r"your\s+session\s+has\s+expired", re.IGNORECASE), "login", "Re-authenticate at provider"),
    (re.compile(r"institutional\s+access", re.IGNORECASE), "login", "Ensure institutional VPN/proxy is active"),
    (re.compile(r"not\s+authorized", re.IGNORECASE), "access_denied", "Check subscription or access rights"),
]


def _detect_blocked(content: bytes, url: str) -> tuple[bool, str, str]:
    """Return (blocked, blocked_reason, action_required) from page content."""
    if not content:
        return False, "", ""
    text = content[:8000].decode("utf-8", errors="ignore")
    for pattern, reason, action in _BLOCK_SIGNALS:
        if pattern.search(text):
            return True, reason, action
    return False, "", ""


def _cdp_host(cdp_url: str) -> str:
    parsed = urllib.parse.urlparse(cdp_url)
    return parsed.netloc or "localhost"


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------

def make_browser_client(settings: Any) -> BrowserClient:
    """Build a BrowserClient from OrchestratorSettings."""
    provider_raw = getattr(settings, "browser_provider", "playwright_cdp") or "playwright_cdp"
    try:
        provider = BrowserProvider(str(provider_raw).strip().lower())
    except ValueError:
        provider = BrowserProvider.PLAYWRIGHT_CDP

    return BrowserClient(
        provider=provider,
        cdp_url=getattr(settings, "playwright_cdp_url", "http://127.0.0.1:9222"),
        timeout_seconds=getattr(settings, "pull_timeout_seconds", 30),
    )
