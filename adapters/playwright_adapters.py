"""Playwright-backed source adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .base import PullAdapter
from .cdp_utils import effective_cdp_url
from .document_links import build_link_rows
from .io_utils import era_years_from_gap, write_json_records
from .seed_url_fetch import blocked_reason_hint, resolve_seed_rows
from contracts import PlannedGap, SourceAvailability, SourceResult, SourceType


class PlaywrightAdapter(PullAdapter):
    """Base class for browser-session adapters."""

    source_type = SourceType.PLAYWRIGHT

    def is_available(self, availability: SourceAvailability) -> bool:
        return self.source_id in availability.playwright_sources

    def validate(self, availability: SourceAvailability) -> str:
        if availability.playwright_unavailable_reason:
            return f"Browser session unavailable: {availability.playwright_unavailable_reason}"
        if self.source_id not in availability.playwright_sources:
            return f"{self.source_id}: not in active browser session list"
        return ""

    def _link_seed_result(self, gap: PlannedGap, query: str, run_dir: str, note: str) -> SourceResult:
        """Emit actionable click-through links for browser-backed sources.

        This preserves user momentum until source-specific browser scraping is
        fully implemented by returning provider search URLs plus local corpus
        matches when available.
        """

        try:
            era_start, era_end = era_years_from_gap(gap)
            rows = build_link_rows(self.source_id, query, gap.gap_id, limit_local=4, era_start=era_start, era_end=era_end)
            source_root = Path(run_dir) / gap.gap_id / self.source_id
            source_root.mkdir(parents=True, exist_ok=True)
            resolved_rows, resolved_stats = resolve_seed_rows(
                rows=rows,
                source_root=source_root,
                source_id=self.source_id,
                query=query,
                gap_id=gap.gap_id,
            )
            rows.extend(resolved_rows)
            blocked_files = int(resolved_stats.get("blocked_files", 0))
            captcha_blocks = int(resolved_stats.get("captcha_blocks", 0))
            login_blocks = int(resolved_stats.get("login_blocks", 0))
            challenge_blocks = int(resolved_stats.get("challenge_blocks", 0))
            for row in rows:
                row_note = note
                blocked_reason = str(row.get("blocked_reason", "")).strip().lower()
                if blocked_reason:
                    hint = blocked_reason_hint(blocked_reason)
                    row_note = f"{row_note} User action required: {hint}" if hint else row_note
                row["note"] = row_note
                row["source_id"] = self.source_id
            root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
            pulled_docs = sum(
                1
                for row in rows
                if (
                    str(row.get("quality_label", "")).lower() in {"high", "medium"}
                    and not str(row.get("blocked_reason", "")).strip()
                )
            )
            status = "completed" if pulled_docs > 0 else ("partial" if rows else "failed")
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=len(rows),
                run_dir=root,
                artifact_type="json_records",
                status=status,
                stats={
                    "records": len(rows),
                    "pulled_docs": pulled_docs,
                    "seed_only": pulled_docs <= 0,
                    "resolved_files": int(resolved_stats.get("resolved_files", 0)),
                    "blocked_files": blocked_files,
                    "captcha_blocks": captcha_blocks,
                    "login_blocks": login_blocks,
                    "challenge_blocks": challenge_blocks,
                    "action_required": blocked_files > 0,
                    "link_mode": "provider_search+local_corpus+resolved_fetch",
                },
            )
        except Exception as exc:
            return SourceResult(
                source_id=self.source_id,
                source_type=self.source_type,
                query=query,
                gap_id=gap.gap_id,
                document_count=0,
                run_dir=str(Path(run_dir) / gap.gap_id / self.source_id),
                artifact_type="json_records",
                status="failed",
                error=str(exc)[:200],
            )


class EbscohostPlaywrightAdapter(PlaywrightAdapter):
    """EBSCOhost adapter: scrapes search results via an authenticated CDP session.

    Requires Chrome to be running with --remote-debugging-port=9222 and the user
    already logged into EBSCOhost (headless login is blocked by Duo MFA).
    Falls back to seed click-through URLs when no CDP session is available.
    """

    source_id = "ebscohost"

    # ── CDP helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _try_cdp_urls(configured: str) -> str:
        """Return first reachable CDP URL, trying localhost first."""
        candidates = []
        if configured and "localhost" not in configured and "127.0.0.1" not in configured:
            candidates.append("http://localhost:9222")
        candidates.append(configured or "http://localhost:9222")
        for url in candidates:
            try:
                probe = f"{url.rstrip('/')}/json/version"
                with urllib.request.urlopen(probe, timeout=3) as r:
                    if r.status == 200:
                        return url
            except Exception:
                pass
        return ""

    # ── EBSCOhost search + scrape ────────────────────────────────────────────

    @staticmethod
    def _build_search_url(query: str, db: str, era_start: Any, era_end: Any) -> str:
        import urllib.parse as _up
        encoded = _up.quote_plus(query)
        url = (
            f"https://search.ebscohost.com/login.aspx"
            f"?direct=true&bquery={encoded}&db={db}&site=eds-live&scope=site"
        )
        if era_start and era_end:
            url += f"&DT1={era_start}0101&DT2={era_end}1231"
        return url

    @staticmethod
    def _extract_records(page: Any, query: str, gap_id: str) -> list:
        """Extract structured records from an EBSCOhost results page via JS eval."""
        try:
            records = page.evaluate("""() => {
                const results = [];
                // Try multiple known EBSCOhost result container selectors
                const containers = document.querySelectorAll(
                    '.result-list-item, article.record, [data-auto="record"], li.results-list-item'
                );
                containers.forEach((el, idx) => {
                    if (idx >= 10) return;
                    const getText = sel => {
                        const node = el.querySelector(sel);
                        return node ? node.innerText.trim() : '';
                    };
                    const getAttr = (sel, attr) => {
                        const node = el.querySelector(sel);
                        return node ? (node.getAttribute(attr) || '').trim() : '';
                    };

                    // Title — try multiple selectors
                    const title =
                        getText('.title-link') ||
                        getText('[data-auto="result-item-title"]') ||
                        getText('h3.title') ||
                        getText('a.record__title') ||
                        getText('.result__title a') ||
                        '';

                    // Authors
                    const authors =
                        getText('.authors-list') ||
                        getText('[data-auto="result-item-authors"]') ||
                        getText('.record__body .authors') ||
                        '';

                    // Source/journal
                    const source =
                        getText('.source-content') ||
                        getText('[data-auto="result-item-source"]') ||
                        getText('.record__body .source') ||
                        '';

                    // Date
                    const date =
                        getText('.date-content') ||
                        getText('[data-auto="result-item-date"]') ||
                        '';

                    // Abstract
                    const abstract =
                        getText('.abstract-value') ||
                        getText('.record__abstract') ||
                        getText('.abstract-text') ||
                        '';

                    // Links
                    const pdfLink = getAttr('a[href*="pdfviewer"], a.pdf-link, [data-auto="pdf-link"]', 'href');
                    const htmlLink = getAttr('a[href*="ehost/detail"], a.detail-link, [data-auto="detail-link"]', 'href');

                    // Accession number (in data attributes or URL)
                    const detailAnchor = el.querySelector('a[href*="AN="], a[href*="an="]');
                    let an = '';
                    if (detailAnchor) {
                        const m = detailAnchor.href.match(/[&?]AN=([^&]+)/i);
                        if (m) an = decodeURIComponent(m[1]);
                    }

                    if (title) {
                        results.push({title, authors, source, date, abstract, pdf_url: pdfLink,
                                      detail_url: htmlLink, accession_num: an});
                    }
                });
                return results;
            }""")
        except Exception:
            records = []
        return records

    @staticmethod
    def _fetch_abstract(page: Any, detail_url: str, timeout_ms: int) -> str:
        """Open a detail page and extract the full abstract."""
        try:
            p2 = page.context.new_page()
            try:
                if not detail_url.startswith("http"):
                    detail_url = "https://search.ebscohost.com" + detail_url
                p2.goto(detail_url, timeout=timeout_ms, wait_until="domcontentloaded")
                abstract = p2.evaluate("""() => {
                    const node = document.querySelector(
                        '#abstract, .abstract-value, .abstractField, [data-auto="abstract"]'
                    );
                    return node ? node.innerText.trim() : '';
                }""")
                return abstract or ""
            finally:
                p2.close()
        except Exception:
            return ""

    def _scrape(
        self, cdp_url: str, query: str, gap_id: str,
        db: str, era_start: Any, era_end: Any, timeout_seconds: int,
    ) -> list:
        from playwright.sync_api import sync_playwright  # type: ignore
        from .cdp_utils import effective_cdp_url

        effective = effective_cdp_url(cdp_url)
        search_url = self._build_search_url(query, db, era_start, era_end)
        ms = timeout_seconds * 1000

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(effective)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(search_url, timeout=ms, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=min(ms, 20000))

                # If we landed on a login page, we can't proceed headlessly
                if "login.ebsco.com" in page.url or "Sign In" in page.title():
                    return []

                records = self._extract_records(page, query, gap_id)

                # For records missing abstracts, try fetching detail page
                for rec in records[:5]:
                    if not rec.get("abstract") and rec.get("detail_url"):
                        rec["abstract"] = self._fetch_abstract(page, rec["detail_url"], 15000)

                return records
            finally:
                page.close()

    # ── main pull ────────────────────────────────────────────────────────────

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        import os as _os
        era_start, era_end = era_years_from_gap(gap)
        db = _os.environ.get("EBSCO_DB", "bth").strip() or "bth"

        # Resolve CDP URL
        configured_cdp = _os.environ.get("ORCH_PLAYWRIGHT_CDP_URL", "http://localhost:9222")
        cdp_url = self._try_cdp_urls(configured_cdp)

        if not cdp_url:
            return self._link_seed_result(
                gap, query, run_dir,
                note="EBSCOhost: no CDP session available. Start Chrome with --remote-debugging-port=9222 and log in.",
            )

        # Attempt live scrape
        try:
            scraped = self._scrape(cdp_url, query, gap_id=gap.gap_id, db=db,
                                   era_start=era_start, era_end=era_end,
                                   timeout_seconds=timeout_seconds)
        except Exception as exc:
            return self._link_seed_result(
                gap, query, run_dir,
                note=f"EBSCOhost scrape failed ({exc!s:.100}); seed links provided.",
            )

        if not scraped:
            return self._link_seed_result(
                gap, query, run_dir,
                note="EBSCOhost: CDP available but no results extracted (login wall or empty results).",
            )

        # Convert to standard row format
        rows = []
        for rec in scraped:
            has_full = bool(rec.get("pdf_url") or rec.get("detail_url"))
            has_abstract = bool(rec.get("abstract"))
            quality_label = "high" if has_full else ("medium" if has_abstract else "seed")
            rows.append({
                "title":         rec.get("title", ""),
                "authors":       rec.get("authors", ""),
                "journal":       rec.get("source", ""),
                "pub_date":      rec.get("date", ""),
                "abstract":      rec.get("abstract", "")[:2000],
                "pdf_url":       rec.get("pdf_url", ""),
                "url":           rec.get("detail_url", "") or rec.get("pdf_url", ""),
                "accession_num": rec.get("accession_num", ""),
                "query":         query,
                "gap_id":        gap.gap_id,
                "quality_label": quality_label,
                "quality_rank":  90 if quality_label == "high" else (60 if quality_label == "medium" else 20),
                "source":        "ebscohost_playwright",
                "link_type":     "full_text" if has_full else ("abstract" if has_abstract else "record"),
            })

        root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
        pulled_docs = sum(1 for r in rows if r["quality_label"] in {"high", "medium"})
        return SourceResult(
            source_id=self.source_id,
            source_type=self.source_type,
            query=query,
            gap_id=gap.gap_id,
            document_count=len(rows),
            run_dir=root,
            artifact_type="json_records",
            status="completed" if pulled_docs > 0 else ("partial" if rows else "failed"),
            stats={
                "records":     len(rows),
                "pulled_docs": pulled_docs,
                "seed_only":   pulled_docs <= 0,
                "link_mode":   "ebscohost_playwright_cdp",
            },
        )


class StatistaPlaywrightAdapter(PlaywrightAdapter):
    """Statista browser adapter placeholder implementation."""

    source_id = "statista"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        return self._link_seed_result(
            gap,
            query,
            run_dir,
            note="Statista Playwright retrieval pending source-specific workflow; seeded click-through links provided.",
        )


class JstorPlaywrightAdapter(PlaywrightAdapter):
    """JSTOR browser adapter: scrapes search results via an authenticated CDP session."""

    source_id = "jstor"

    @staticmethod
    def _build_search_url(query: str, era_start: Any, era_end: Any) -> str:
        import urllib.parse as _up
        encoded = _up.quote_plus(query)
        url = f"https://www.jstor.org/action/doBasicSearch?Query={encoded}&acc=on&wc=on&so=rel"
        if era_start and era_end:
            url += f"&dateRangeFirst={era_start}&dateRangeLast={era_end}"
        return url

    @staticmethod
    def _extract_records(page: Any) -> list:
        try:
            return page.evaluate("""() => {
                const results = [];
                const containers = document.querySelectorAll(
                    '.result-item, .media-card-outer, article.result, [class*="result-item"], li[class*="result"]'
                );
                containers.forEach((el, idx) => {
                    if (idx >= 10) return;
                    const getText = sel => {
                        const node = el.querySelector(sel);
                        return node ? node.innerText.trim() : '';
                    };
                    const getAttr = (sel, attr) => {
                        const node = el.querySelector(sel);
                        return node ? (node.getAttribute(attr) || '').trim() : '';
                    };

                    const title =
                        getText('.result-item__title a') ||
                        getText('[class*="media-card"] .title a') ||
                        getText('h3 a') || getText('h2 a') || '';

                    const authors =
                        getText('.result-item__contrib') ||
                        getText('[class*="contrib"]') ||
                        getText('[class*="author"]') || '';

                    const journal =
                        getText('.result-item__source') ||
                        getText('[class*="source"]') ||
                        getText('[class*="journal"]') || '';

                    const date =
                        getText('.result-item__pub-date') ||
                        getText('[class*="pub-date"]') ||
                        getText('[class*="year"]') || '';

                    const abstract =
                        getText('[class*="abstract"]') ||
                        getText('.teaser') || '';

                    const pdfHref =
                        getAttr('a[href*="/stable/pdf/"]', 'href') ||
                        getAttr('a.pdf, a[class*="pdf"]', 'href') || '';

                    const detailHref =
                        getAttr('.result-item__title a', 'href') ||
                        getAttr('[class*="media-card"] .title a', 'href') ||
                        getAttr('h3 a, h2 a', 'href') || '';

                    if (title) {
                        results.push({
                            title, authors, journal, date, abstract,
                            pdf_url:    pdfHref  ? 'https://www.jstor.org' + (pdfHref.startsWith('/') ? pdfHref : '/' + pdfHref) : '',
                            detail_url: detailHref ? 'https://www.jstor.org' + (detailHref.startsWith('/') ? detailHref : '/' + detailHref) : '',
                        });
                    }
                });
                return results;
            }""")
        except Exception:
            return []

    def _scrape(self, cdp_url: str, query: str, gap_id: str, era_start: Any, era_end: Any, timeout_seconds: int) -> list:
        from playwright.sync_api import sync_playwright  # type: ignore
        from .cdp_utils import effective_cdp_url

        effective = effective_cdp_url(cdp_url)
        search_url = self._build_search_url(query, era_start, era_end)
        ms = timeout_seconds * 1000

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(effective)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(search_url, timeout=ms, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=min(ms, 20000))
                # JSTOR login wall check
                if "jstor.org/login" in page.url or "Sign In" in (page.title() or ""):
                    return []
                return self._extract_records(page)
            finally:
                page.close()

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        import os as _os
        era_start, era_end = era_years_from_gap(gap)
        configured_cdp = _os.environ.get("ORCH_PLAYWRIGHT_CDP_URL", "http://localhost:9222")
        cdp_url = EbscohostPlaywrightAdapter._try_cdp_urls(configured_cdp)

        if not cdp_url:
            return self._link_seed_result(gap, query, run_dir,
                note="JSTOR: no CDP session available. Start Chrome with --remote-debugging-port=9222 and log in.")

        try:
            scraped = self._scrape(cdp_url, query, gap.gap_id, era_start, era_end, timeout_seconds)
        except Exception as exc:
            return self._link_seed_result(gap, query, run_dir,
                note=f"JSTOR scrape failed ({exc!s:.100}); seed links provided.")

        if not scraped:
            return self._link_seed_result(gap, query, run_dir,
                note="JSTOR: CDP available but no results extracted (login wall or empty results).")

        rows = []
        for rec in scraped:
            has_full = bool(rec.get("pdf_url") or rec.get("detail_url"))
            has_abstract = bool(rec.get("abstract"))
            quality_label = "high" if has_full else ("medium" if has_abstract else "seed")
            rows.append({
                "title":         rec.get("title", ""),
                "authors":       rec.get("authors", ""),
                "journal":       rec.get("journal", ""),
                "pub_date":      rec.get("date", ""),
                "abstract":      rec.get("abstract", "")[:2000],
                "pdf_url":       rec.get("pdf_url", ""),
                "url":           rec.get("detail_url", "") or rec.get("pdf_url", ""),
                "query":         query,
                "gap_id":        gap.gap_id,
                "quality_label": quality_label,
                "quality_rank":  90 if quality_label == "high" else (60 if quality_label == "medium" else 20),
                "source":        "jstor_playwright",
                "link_type":     "full_text" if has_full else ("abstract" if has_abstract else "record"),
            })

        root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
        pulled_docs = sum(1 for r in rows if r["quality_label"] in {"high", "medium"})
        return SourceResult(
            source_id=self.source_id,
            source_type=self.source_type,
            query=query,
            gap_id=gap.gap_id,
            document_count=len(rows),
            run_dir=root,
            artifact_type="json_records",
            status="completed" if pulled_docs > 0 else ("partial" if rows else "failed"),
            stats={"records": len(rows), "pulled_docs": pulled_docs,
                   "seed_only": pulled_docs <= 0, "link_mode": "jstor_playwright_cdp"},
        )


class ProjectMusePlaywrightAdapter(PlaywrightAdapter):
    """Project MUSE browser adapter: scrapes search results via an authenticated CDP session."""

    source_id = "project_muse"

    @staticmethod
    def _build_search_url(query: str, era_start: Any, era_end: Any) -> str:
        import urllib.parse as _up
        encoded = _up.quote_plus(query)
        url = f"https://muse.jhu.edu/search?q={encoded}&type=general"
        if era_start and era_end:
            url += f"&min_year={era_start}&max_year={era_end}"
        return url

    @staticmethod
    def _extract_records(page: Any) -> list:
        try:
            return page.evaluate("""() => {
                const results = [];
                const containers = document.querySelectorAll(
                    '.search-result-item, [class*="result-item"], article.result, .result'
                );
                containers.forEach((el, idx) => {
                    if (idx >= 10) return;
                    const getText = sel => {
                        const node = el.querySelector(sel);
                        return node ? node.innerText.trim() : '';
                    };
                    const getAttr = (sel, attr) => {
                        const node = el.querySelector(sel);
                        return node ? (node.getAttribute(attr) || '').trim() : '';
                    };

                    const title =
                        getText('.result-title a') ||
                        getText('h3 a') || getText('h2 a') ||
                        getText('[class*="title"] a') || '';

                    const authors =
                        getText('.result-authors') ||
                        getText('[class*="author"]') ||
                        getText('.contrib') || '';

                    const journal =
                        getText('.result-source') ||
                        getText('[class*="journal"]') ||
                        getText('[class*="source"]') || '';

                    const date =
                        getText('.result-date') ||
                        getText('[class*="date"]') ||
                        getText('[class*="year"]') || '';

                    const abstract =
                        getText('.result-abstract') ||
                        getText('[class*="abstract"]') || '';

                    const detailHref =
                        getAttr('.result-title a', 'href') ||
                        getAttr('h3 a', 'href') || getAttr('h2 a', 'href') ||
                        getAttr('[class*="title"] a', 'href') || '';

                    const pdfHref = getAttr('a[href*="/pdf/"], a.pdf-link', 'href') || '';

                    if (title) {
                        const base = 'https://muse.jhu.edu';
                        results.push({
                            title, authors, journal, date, abstract,
                            pdf_url:    pdfHref    ? (pdfHref.startsWith('http') ? pdfHref : base + pdfHref) : '',
                            detail_url: detailHref ? (detailHref.startsWith('http') ? detailHref : base + detailHref) : '',
                        });
                    }
                });
                return results;
            }""")
        except Exception:
            return []

    def _scrape(self, cdp_url: str, query: str, gap_id: str, era_start: Any, era_end: Any, timeout_seconds: int) -> list:
        from playwright.sync_api import sync_playwright  # type: ignore
        from .cdp_utils import effective_cdp_url

        effective = effective_cdp_url(cdp_url)
        search_url = self._build_search_url(query, era_start, era_end)
        ms = timeout_seconds * 1000

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(effective)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(search_url, timeout=ms, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=min(ms, 20000))
                # Login wall check
                if "login" in page.url.lower() or "sign in" in (page.title() or "").lower():
                    return []
                return self._extract_records(page)
            finally:
                page.close()

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        import os as _os
        era_start, era_end = era_years_from_gap(gap)
        configured_cdp = _os.environ.get("ORCH_PLAYWRIGHT_CDP_URL", "http://localhost:9222")
        cdp_url = EbscohostPlaywrightAdapter._try_cdp_urls(configured_cdp)

        if not cdp_url:
            return self._link_seed_result(gap, query, run_dir,
                note="Project MUSE: no CDP session available. Start Chrome with --remote-debugging-port=9222 and log in.")

        try:
            scraped = self._scrape(cdp_url, query, gap.gap_id, era_start, era_end, timeout_seconds)
        except Exception as exc:
            return self._link_seed_result(gap, query, run_dir,
                note=f"Project MUSE scrape failed ({exc!s:.100}); seed links provided.")

        if not scraped:
            return self._link_seed_result(gap, query, run_dir,
                note="Project MUSE: CDP available but no results extracted (login wall or empty results).")

        rows = []
        for rec in scraped:
            has_full = bool(rec.get("pdf_url") or rec.get("detail_url"))
            has_abstract = bool(rec.get("abstract"))
            quality_label = "high" if has_full else ("medium" if has_abstract else "seed")
            rows.append({
                "title":         rec.get("title", ""),
                "authors":       rec.get("authors", ""),
                "journal":       rec.get("journal", ""),
                "pub_date":      rec.get("date", ""),
                "abstract":      rec.get("abstract", "")[:2000],
                "pdf_url":       rec.get("pdf_url", ""),
                "url":           rec.get("detail_url", "") or rec.get("pdf_url", ""),
                "query":         query,
                "gap_id":        gap.gap_id,
                "quality_label": quality_label,
                "quality_rank":  90 if quality_label == "high" else (60 if quality_label == "medium" else 20),
                "source":        "project_muse_playwright",
                "link_type":     "full_text" if has_full else ("abstract" if has_abstract else "record"),
            })

        root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
        pulled_docs = sum(1 for r in rows if r["quality_label"] in {"high", "medium"})
        return SourceResult(
            source_id=self.source_id,
            source_type=self.source_type,
            query=query,
            gap_id=gap.gap_id,
            document_count=len(rows),
            run_dir=root,
            artifact_type="json_records",
            status="completed" if pulled_docs > 0 else ("partial" if rows else "failed"),
            stats={"records": len(rows), "pulled_docs": pulled_docs,
                   "seed_only": pulled_docs <= 0, "link_mode": "project_muse_playwright_cdp"},
        )


class ProquestHistoricalNewsPlaywrightAdapter(PlaywrightAdapter):
    """ProQuest Historical Newspapers browser adapter: scrapes via authenticated CDP session."""

    source_id = "proquest_historical_newspapers"

    @staticmethod
    def _build_search_url(query: str, era_start: Any, era_end: Any) -> str:
        import urllib.parse as _up
        encoded = _up.quote_plus(query)
        url = f"https://www.proquest.com/hnpnewyorktimes/results/1?q={encoded}&t:ac=subject/NEWSPAPER"
        if era_start and era_end:
            url += f"&daterange=custom&startdate={era_start}0101&enddate={era_end}1231"
        return url

    @staticmethod
    def _extract_records(page: Any) -> list:
        try:
            return page.evaluate("""() => {
                const results = [];
                const containers = document.querySelectorAll(
                    '.resultItem, [data-testid="result-item"], .record, article.result'
                );
                containers.forEach((el, idx) => {
                    if (idx >= 10) return;
                    const getText = sel => {
                        const node = el.querySelector(sel);
                        return node ? node.innerText.trim() : '';
                    };
                    const getAttr = (sel, attr) => {
                        const node = el.querySelector(sel);
                        return node ? (node.getAttribute(attr) || '').trim() : '';
                    };

                    const title =
                        getText('.title a') || getText('h3 a') || getText('h2 a') ||
                        getText('[class*="title"] a') || '';

                    const authors = getText('[class*="author"]') || getText('.byline') || '';
                    const source = getText('[class*="pub"]') || getText('.source') || '';
                    const date = getText('[class*="date"]') || getText('.pubdate') || '';
                    const abstract = getText('[class*="abstract"]') || getText('.snippet') || '';

                    const detailHref = getAttr('.title a', 'href') || getAttr('h3 a', 'href') || '';
                    const pdfHref = getAttr('a[href*="pdf"]', 'href') || '';

                    if (title) {
                        const base = 'https://www.proquest.com';
                        results.push({
                            title, authors, journal: source, date, abstract,
                            pdf_url:    pdfHref    ? (pdfHref.startsWith('http') ? pdfHref : base + pdfHref) : '',
                            detail_url: detailHref ? (detailHref.startsWith('http') ? detailHref : base + detailHref) : '',
                        });
                    }
                });
                return results;
            }""")
        except Exception:
            return []

    def _scrape(self, cdp_url: str, query: str, gap_id: str, era_start: Any, era_end: Any, timeout_seconds: int) -> list:
        from playwright.sync_api import sync_playwright  # type: ignore
        from .cdp_utils import effective_cdp_url

        effective = effective_cdp_url(cdp_url)
        search_url = self._build_search_url(query, era_start, era_end)
        ms = timeout_seconds * 1000

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(effective)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(search_url, timeout=ms, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=min(ms, 20000))
                if "login" in page.url.lower() or "sign in" in (page.title() or "").lower():
                    return []
                return self._extract_records(page)
            finally:
                page.close()

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        import os as _os
        era_start, era_end = era_years_from_gap(gap)
        configured_cdp = _os.environ.get("ORCH_PLAYWRIGHT_CDP_URL", "http://localhost:9222")
        cdp_url = EbscohostPlaywrightAdapter._try_cdp_urls(configured_cdp)

        if not cdp_url:
            return self._link_seed_result(gap, query, run_dir,
                note="ProQuest Historical Newspapers: no CDP session. Start Chrome with --remote-debugging-port=9222 and log in.")

        try:
            scraped = self._scrape(cdp_url, query, gap.gap_id, era_start, era_end, timeout_seconds)
        except Exception as exc:
            return self._link_seed_result(gap, query, run_dir,
                note=f"ProQuest Historical Newspapers scrape failed ({exc!s:.100}); seed links provided.")

        if not scraped:
            return self._link_seed_result(gap, query, run_dir,
                note="ProQuest Historical Newspapers: CDP available but no results extracted.")

        rows = []
        for rec in scraped:
            has_full = bool(rec.get("pdf_url") or rec.get("detail_url"))
            has_abstract = bool(rec.get("abstract"))
            quality_label = "high" if has_full else ("medium" if has_abstract else "seed")
            rows.append({
                "title":         rec.get("title", ""),
                "authors":       rec.get("authors", ""),
                "journal":       rec.get("journal", ""),
                "pub_date":      rec.get("date", ""),
                "abstract":      rec.get("abstract", "")[:2000],
                "pdf_url":       rec.get("pdf_url", ""),
                "url":           rec.get("detail_url", "") or rec.get("pdf_url", ""),
                "query":         query,
                "gap_id":        gap.gap_id,
                "quality_label": quality_label,
                "quality_rank":  90 if quality_label == "high" else (60 if quality_label == "medium" else 20),
                "source":        "proquest_historical_newspapers_playwright",
                "link_type":     "full_text" if has_full else ("abstract" if has_abstract else "record"),
            })

        root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
        pulled_docs = sum(1 for r in rows if r["quality_label"] in {"high", "medium"})
        return SourceResult(
            source_id=self.source_id,
            source_type=self.source_type,
            query=query,
            gap_id=gap.gap_id,
            document_count=len(rows),
            run_dir=root,
            artifact_type="json_records",
            status="completed" if pulled_docs > 0 else ("partial" if rows else "failed"),
            stats={"records": len(rows), "pulled_docs": pulled_docs,
                   "seed_only": pulled_docs <= 0, "link_mode": "proquest_historical_cdp"},
        )


class AmericasHistoricalNewsPlaywrightAdapter(PlaywrightAdapter):
    """America's Historical Newspapers browser adapter placeholder implementation."""

    source_id = "americas_historical_newspapers"

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        return self._link_seed_result(
            gap,
            query,
            run_dir,
            note="America's Historical Newspapers retrieval pending source-specific workflow; seeded click-through links provided.",
        )


class GalePrimarySourcesPlaywrightAdapter(PlaywrightAdapter):
    """Gale Primary Sources browser adapter: scrapes via authenticated CDP session."""

    source_id = "gale_primary_sources"

    @staticmethod
    def _build_search_url(query: str, era_start: Any, era_end: Any) -> str:
        import urllib.parse as _up
        encoded = _up.quote_plus(query)
        url = f"https://link.gale.com/apps/resultslist?q={encoded}&source=MOML"
        if era_start and era_end:
            url += f"&startDate={era_start}&endDate={era_end}"
        return url

    @staticmethod
    def _extract_records(page: Any) -> list:
        try:
            return page.evaluate("""() => {
                const results = [];
                const containers = document.querySelectorAll(
                    '.result-item, [class*="resultslist"] li, article.result, .hit'
                );
                containers.forEach((el, idx) => {
                    if (idx >= 10) return;
                    const getText = sel => {
                        const node = el.querySelector(sel);
                        return node ? node.innerText.trim() : '';
                    };
                    const getAttr = (sel, attr) => {
                        const node = el.querySelector(sel);
                        return node ? (node.getAttribute(attr) || '').trim() : '';
                    };

                    const title =
                        getText('[class*="title"] a') ||
                        getText('h3 a') || getText('h2 a') || '';

                    const authors = getText('[class*="author"]') || getText('.byline') || '';
                    const journal = getText('[class*="source"]') || getText('[class*="pub"]') || '';
                    const date = getText('[class*="date"]') || getText('[class*="year"]') || '';
                    const abstract = getText('[class*="abstract"]') || getText('[class*="snippet"]') || '';
                    const detailHref = getAttr('[class*="title"] a', 'href') || getAttr('h3 a', 'href') || '';
                    const pdfHref = getAttr('a[href*="pdf"]', 'href') || '';

                    if (title) {
                        results.push({
                            title, authors, journal, date, abstract,
                            pdf_url:    pdfHref    || '',
                            detail_url: detailHref || '',
                        });
                    }
                });
                return results;
            }""")
        except Exception:
            return []

    def _scrape(self, cdp_url: str, query: str, gap_id: str, era_start: Any, era_end: Any, timeout_seconds: int) -> list:
        from playwright.sync_api import sync_playwright  # type: ignore
        from .cdp_utils import effective_cdp_url

        effective = effective_cdp_url(cdp_url)
        search_url = self._build_search_url(query, era_start, era_end)
        ms = timeout_seconds * 1000

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(effective)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(search_url, timeout=ms, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=min(ms, 20000))
                if "login" in page.url.lower() or "sign in" in (page.title() or "").lower():
                    return []
                return self._extract_records(page)
            finally:
                page.close()

    def pull(self, gap: PlannedGap, query: str, run_dir: str, timeout_seconds: int = 120) -> SourceResult:
        import os as _os
        era_start, era_end = era_years_from_gap(gap)
        configured_cdp = _os.environ.get("ORCH_PLAYWRIGHT_CDP_URL", "http://localhost:9222")
        cdp_url = EbscohostPlaywrightAdapter._try_cdp_urls(configured_cdp)

        if not cdp_url:
            return self._link_seed_result(gap, query, run_dir,
                note="Gale Primary Sources: no CDP session. Start Chrome with --remote-debugging-port=9222 and log in.")

        try:
            scraped = self._scrape(cdp_url, query, gap.gap_id, era_start, era_end, timeout_seconds)
        except Exception as exc:
            return self._link_seed_result(gap, query, run_dir,
                note=f"Gale Primary Sources scrape failed ({exc!s:.100}); seed links provided.")

        if not scraped:
            return self._link_seed_result(gap, query, run_dir,
                note="Gale Primary Sources: CDP available but no results extracted.")

        rows = []
        for rec in scraped:
            has_full = bool(rec.get("pdf_url") or rec.get("detail_url"))
            has_abstract = bool(rec.get("abstract"))
            quality_label = "high" if has_full else ("medium" if has_abstract else "seed")
            rows.append({
                "title":         rec.get("title", ""),
                "authors":       rec.get("authors", ""),
                "journal":       rec.get("journal", ""),
                "pub_date":      rec.get("date", ""),
                "abstract":      rec.get("abstract", "")[:2000],
                "pdf_url":       rec.get("pdf_url", ""),
                "url":           rec.get("detail_url", "") or rec.get("pdf_url", ""),
                "query":         query,
                "gap_id":        gap.gap_id,
                "quality_label": quality_label,
                "quality_rank":  90 if quality_label == "high" else (60 if quality_label == "medium" else 20),
                "source":        "gale_primary_sources_playwright",
                "link_type":     "full_text" if has_full else ("abstract" if has_abstract else "record"),
            })

        root = write_json_records(rows, run_dir, gap.gap_id, self.source_id, query)
        pulled_docs = sum(1 for r in rows if r["quality_label"] in {"high", "medium"})
        return SourceResult(
            source_id=self.source_id,
            source_type=self.source_type,
            query=query,
            gap_id=gap.gap_id,
            document_count=len(rows),
            run_dir=root,
            artifact_type="json_records",
            status="completed" if pulled_docs > 0 else ("partial" if rows else "failed"),
            stats={"records": len(rows), "pulled_docs": pulled_docs,
                   "seed_only": pulled_docs <= 0, "link_mode": "gale_primary_sources_cdp"},
        )


def check_cdp_endpoint(cdp_url: str, timeout_seconds: int = 5) -> str:
    """Return empty string when CDP endpoint is reachable, else error reason."""

    probe_url = f"{effective_cdp_url(cdp_url).rstrip('/')}/json"
    try:
        with urllib.request.urlopen(probe_url, timeout=max(1, timeout_seconds)) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if isinstance(payload, list):
            return ""
        return "unexpected CDP response payload"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore").strip()
        if detail:
            return f"HTTP {exc.code}: {detail[:120]}"
        return f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return str(exc.reason)[:160]
    except Exception as exc:  # noqa: BLE001 - return reason string instead of raising.
        return str(exc)[:160]
