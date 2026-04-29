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

import contextlib
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

    @contextlib.contextmanager
    def session(self):
        """Reusable browser session — opens ONE persistent tab and yields a
        session object that reuses it across all fetches.

        Without this context manager, every ``fetch_with_eval`` call enters
        its own ``sync_playwright`` block and creates a new tab via
        ``ctx.new_page()``, which on Chrome+CDP repeatedly steals OS focus
        away from whatever the user is doing. With ``session()``, only the
        initial tab open pulls focus once; the user's previously-active tab
        is brought back to the front immediately, and all subsequent
        navigations reuse the same tab without further focus events.

        Yields an object exposing ``fetch``, ``fetch_with_eval``,
        ``is_available``, and ``open_tabs`` — interface-compatible with
        ``BrowserClient`` so callers (``run_fetch`` / ``fetch_seed_page``)
        can use a session and a raw client interchangeably.

        For non-CDP providers (HTTP / claude_cu) the session is a no-op
        passthrough — ``yield self`` — since there is no UI to manage.
        """
        if self.provider != BrowserProvider.PLAYWRIGHT_CDP:
            yield self
            return

        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            # Playwright not installed — fall back to per-call behavior.
            yield self
            return

        from adapters.cdp_utils import effective_cdp_url  # local import
        effective = effective_cdp_url(self.cdp_url)

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(effective)
            except Exception:
                # CDP unreachable — fall back to per-call behavior so each
                # individual fetch can fail with its own error envelope.
                yield self
                return

            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()

            # Restore the user's previously-active tab to the front so our
            # new working tab doesn't hold focus during the run.
            try:
                others = [p for p in ctx.pages if p is not page]
                if others:
                    others[0].bring_to_front()
            except Exception:
                pass

            try:
                yield _PersistentPageSession(self, ctx, page)
            finally:
                try:
                    page.close()
                except Exception:
                    pass

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

    def fetch_with_eval(
        self,
        url: str,
        js_expr: str,
        *,
        wait_ms: int = 2000,
        wait_for: Optional[str] = None,
    ) -> tuple:
        """Navigate to `url` via CDP, wait for JS rendering, run `js_expr`.

        Returns ``(PageResult, eval_result)`` where ``eval_result`` is whatever
        the JS expression returns (list / dict / scalar / None).  Only supported
        under the ``playwright_cdp`` provider; HTTP and claude_cu providers return
        the page result with ``eval_result = None``.

        Parameters
        ----------
        wait_ms:
            Maximum time to wait for the result anchor (or, when ``wait_for``
            is None, a fixed sleep before evaluating ``js_expr``).
        wait_for:
            Optional CSS selector for a result-list anchor (e.g. EBSCO's
            ``article[data-auto="search-result-item"]``). When provided,
            ``wait_for_selector(wait_for, timeout=wait_ms)`` is used so we
            return as soon as content has rendered (typically much faster
            than ``wait_ms``); if the selector never appears within
            ``wait_ms``, evaluation proceeds anyway so soft-fails / empty
            results still get logged. Anchor-based waits are content-driven
            and look less bot-like than uniform fixed sleeps.

        Used by document_fetch extractors (EBSCO, JSTOR) that need to query the
        live DOM to extract article records from search-results pages.
        """
        if self.provider != BrowserProvider.PLAYWRIGHT_CDP:
            page_result = self.fetch(url)
            return page_result, None

        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise RuntimeError("playwright not installed") from exc

        from adapters.cdp_utils import effective_cdp_url  # local import
        effective = effective_cdp_url(self.cdp_url)

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(effective)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(url, timeout=self.timeout_seconds * 1000, wait_until="domcontentloaded")
                if wait_for:
                    try:
                        page.wait_for_selector(wait_for, timeout=wait_ms, state="visible")
                    except Exception:
                        # Selector never appeared — proceed to eval so the caller
                        # still sees the page content (block detection, soft-fail
                        # signals, etc.). Eval may return empty.
                        pass
                elif wait_ms > 0:
                    page.wait_for_timeout(wait_ms)
                try:
                    eval_result = page.evaluate(js_expr)
                except Exception as eval_exc:  # noqa: BLE001
                    eval_result = None
                    _ = eval_exc  # logged via PageResult error below

                html_bytes = page.content().encode("utf-8", errors="ignore")
                page_result = PageResult(
                    url=url,
                    status_code=200,
                    content=html_bytes,
                    content_type="text/html",
                )
                # Parent-page text first, then live-DOM iframe probe.
                blocked, reason, action = _detect_blocked(html_bytes, url)
                if not blocked:
                    blocked, reason, action = _detect_iframe_block(page)
                page_result.blocked = blocked
                page_result.blocked_reason = reason
                page_result.action_required = action
                return page_result, eval_result
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
# Persistent-page session (single-tab reuse)
# ------------------------------------------------------------------


class _PersistentPageSession:
    """Single-tab browser session yielded by ``BrowserClient.session()``.

    Reuses one Playwright page across many ``fetch_with_eval`` / ``fetch``
    calls so the run only steals focus once instead of once per URL.

    The interface intentionally mirrors ``BrowserClient`` (``fetch``,
    ``fetch_with_eval``, ``is_available``, ``open_tabs``) so callers can
    pass either a session or a raw client without branching.
    """

    def __init__(self, client: "BrowserClient", ctx: Any, page: Any) -> None:
        self._client = client
        self._ctx = ctx
        self._page = page
        self._timeout_seconds = client.timeout_seconds
        self._user_agent = client.user_agent

    # -- pass-through helpers -----------------------------------------

    def is_available(self) -> bool:
        return self._client.is_available()

    def open_tabs(self, urls: List[str]) -> Dict[str, Any]:
        # Tab opening is a separate UX action — delegate to the client so
        # it uses a fresh sync_playwright session and doesn't disturb our
        # persistent page.
        return self._client.open_tabs(urls)

    # -- core fetch path (reuses self._page) --------------------------

    def fetch(self, url: str, **_: Any) -> PageResult:
        """Navigate the persistent page to ``url`` and return its content."""
        start = time.monotonic()
        page = self._page
        try:
            page.goto(
                url,
                timeout=self._timeout_seconds * 1000,
                wait_until="domcontentloaded",
            )
            html_bytes = page.content().encode("utf-8", errors="ignore")
            result = PageResult(
                url=url,
                status_code=200,
                content=html_bytes,
                content_type="text/html",
            )
            # Check parent-page text first; fall back to live-DOM iframe
            # probe for cross-origin CAPTCHA widgets (reCAPTCHA / Turnstile).
            blocked, reason, action = _detect_blocked(html_bytes, url)
            if not blocked:
                blocked, reason, action = _detect_iframe_block(page)
            result.blocked = blocked
            result.blocked_reason = reason
            result.action_required = action
            result.elapsed_ms = int((time.monotonic() - start) * 1000)
            return result
        except Exception as exc:  # noqa: BLE001
            return PageResult(
                url=url, status_code=0, content=b"", content_type="",
                blocked=True, blocked_reason="error",
                error=str(exc)[:200],
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )

    def fetch_with_eval(
        self,
        url: str,
        js_expr: str,
        *,
        wait_ms: int = 2000,
        wait_for: Optional[str] = None,
    ) -> tuple:
        """Navigate the persistent page to ``url``, wait, and run ``js_expr``."""
        start = time.monotonic()
        page = self._page
        try:
            page.goto(
                url,
                timeout=self._timeout_seconds * 1000,
                wait_until="domcontentloaded",
            )
        except Exception as exc:  # noqa: BLE001
            return (
                PageResult(
                    url=url, status_code=0, content=b"", content_type="",
                    blocked=True, blocked_reason="timeout",
                    action_required="Check network or browser",
                    error=str(exc)[:200],
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                ),
                None,
            )

        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=wait_ms, state="visible")
            except Exception:
                # Selector never appeared — proceed anyway so soft-fail
                # signals (block detection, empty results) still surface.
                pass
        elif wait_ms > 0:
            page.wait_for_timeout(wait_ms)

        try:
            eval_result = page.evaluate(js_expr)
        except Exception:
            eval_result = None

        html_bytes = page.content().encode("utf-8", errors="ignore")
        result = PageResult(
            url=url,
            status_code=200,
            content=html_bytes,
            content_type="text/html",
        )
        # Parent-page text first, then live-DOM iframe probe (CAPTCHAs in
        # cross-origin iframes never show up in parent HTML / regex).
        blocked, reason, action = _detect_blocked(html_bytes, url)
        if not blocked:
            blocked, reason, action = _detect_iframe_block(page)
        result.blocked = blocked
        result.blocked_reason = reason
        result.action_required = action
        result.elapsed_ms = int((time.monotonic() - start) * 1000)
        return result, eval_result


# ------------------------------------------------------------------
# Blocked-page detection
# ------------------------------------------------------------------

_BLOCK_SIGNALS = [
    (re.compile(r"access\s+denied", re.IGNORECASE), "access_denied", "Request access through your institution"),
    # CAPTCHA family — reCAPTCHA, hCaptcha, "I'm not a robot" widget text,
    # generic human-verification phrasing, and Cloudflare interstitials.
    (re.compile(r"captcha", re.IGNORECASE), "captcha", "Complete CAPTCHA in browser"),
    (re.compile(r"i'?m\s+not\s+a\s+robot|i\s+am\s+not\s+a\s+robot", re.IGNORECASE), "captcha", "Solve 'I'm not a robot' challenge in browser"),
    (re.compile(r"verify\s+(you\s+are\s+(human|a\s+person)|your\s+humanity)", re.IGNORECASE), "captcha", "Complete human-verification challenge in browser"),
    (re.compile(r"checking\s+your\s+browser|just\s+a\s+moment", re.IGNORECASE), "captcha", "Wait for / solve the Cloudflare challenge in browser"),
    # Rate-limit / quota — EBSCO Entitlement API and similar return 429 with
    # phrases like "Rate limit quota violation. Quota limit exceeded."
    (re.compile(r"too\s+many\s+requests|rate\s+limit|quota\s+(limit\s+)?exceeded|quota\s+violation", re.IGNORECASE), "rate_limit", "Rate-limited — wait or back off and retry"),
    # Generic explicit-block language (Cloudflare, ezproxy, etc.)
    (re.compile(r"(you\s+have\s+been|your\s+access\s+has\s+been)\s+blocked", re.IGNORECASE), "access_denied", "Access blocked — contact provider or wait before retry"),
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


# JS used by _detect_iframe_block to find CAPTCHA iframes / globals.
# Text regex can't see widgets that live in Google/hCaptcha/Cloudflare iframes
# because the parent page's HTML doesn't contain "I'm not a robot" — that text
# lives inside the cross-origin iframe. This DOM probe checks iframe src
# patterns and the JS challenge-API globals (covers v3 / invisible variants).
_IFRAME_BLOCK_JS = """() => {
    try {
        const frames = Array.from(document.querySelectorAll('iframe'));
        for (const f of frames) {
            const src = (f.src || '').toLowerCase();
            if (!src) continue;
            if (src.includes('recaptcha')) return 'recaptcha';
            if (src.includes('hcaptcha'))  return 'hcaptcha';
            if (src.includes('challenges.cloudflare.com') || src.includes('turnstile')) return 'turnstile';
            if (src.includes('captcha'))   return 'captcha';
        }
        // Globals catch invisible / v3-style challenges with no visible iframe.
        if (typeof window.grecaptcha !== 'undefined') return 'recaptcha';
        if (typeof window.hcaptcha   !== 'undefined') return 'hcaptcha';
        if (typeof window.turnstile  !== 'undefined') return 'turnstile';
    } catch (e) {}
    return '';
}"""


def _detect_iframe_block(page: Any) -> tuple[bool, str, str]:
    """Probe the live page DOM for CAPTCHA iframes / challenge globals.

    Complements _detect_blocked (which only sees the parent-page text) for
    cross-origin challenge widgets where the visible "I'm not a robot" text
    lives in an iframe and never appears in the parent HTML.

    Returns (blocked, reason, action). Best-effort: returns (False, "", "")
    on any error so it never breaks a successful fetch.
    """
    try:
        kind = page.evaluate(_IFRAME_BLOCK_JS)
    except Exception:
        return False, "", ""
    if not kind:
        return False, "", ""
    pretty = {
        "recaptcha": "Solve reCAPTCHA challenge in browser",
        "hcaptcha":  "Solve hCaptcha challenge in browser",
        "turnstile": "Wait for / solve Cloudflare Turnstile in browser",
        "captcha":   "Solve CAPTCHA challenge in browser",
    }
    return True, "captcha", pretty.get(kind, "Solve challenge in browser")


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
