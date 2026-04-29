[2026-04-29] - Browser thrash, iframe-CAPTCHA blind spot, undifferentiated retry, and fixed-wait races

Problem
After the initial pause-on-blocked feature shipped, real-world testing exposed four follow-on issues that, together, made the pipeline visibly broken: (1) every fetch_with_eval call entered its own sync_playwright block and created a new tab via ctx.new_page(), which on Chrome+CDP repeatedly stole OS focus from whatever the user was doing — a 20-page run produced 20 focus events; (2) reCAPTCHA / hCaptcha / Cloudflare Turnstile widgets live inside cross-origin iframes whose visible "I'm not a robot" text never appears in the parent page HTML, so the existing _BLOCK_SIGNALS text regex never fired and the pipeline skipped CAPTCHA pages without pausing; (3) on_blocked ran the same "human pause + retry" path for every blocked reason, including rate_limit (where humans cannot help) and access_denied (where retrying never works); (4) fetch_with_eval used a fixed page.wait_for_timeout(2500) which raced the EBSCO SPA — pages that rendered slowly returned 0 articles, contributing to a 15% miss rate in --limit 20 testing.

Root Cause
1. focus thrash: Playwright's sync API has no clean "create background tab" option; ctx.new_page() opens a focused tab. Architecturally, each fetch was creating + closing its own session/page so there was no opportunity to amortize the focus cost.
2. iframe blind spot: _detect_blocked() only inspected content[:8000] of the parent-page bytes. CAPTCHA widget DOM is cross-origin (Google/Cloudflare-hosted iframes); the parent page's bytes contain only a placeholder div.
3. retry mismatch: blocked_reason was already typed (captcha / login / rate_limit / access_denied) but the on_blocked handler dispatched all reasons through the same input() pause. Rate-limited backoff and skip-without-pause for access_denied were possible but unimplemented.
4. fixed wait: page.wait_for_timeout(2500) is uniform regardless of how fast or slow the page actually renders — wastes time on fast pages and times out on slow ones.

Solution
1. Single-tab session. Added BrowserClient.session() context manager that opens ONE persistent page and yields a _PersistentPageSession proxy whose fetch / fetch_with_eval reuse that page across the entire run. run_fetch wraps the seeds + pdfs loops in `with browser_client.session() as bc:`. Result: one focus event at session start (the user's previous tab is then immediately brought back to front via bring_to_front), zero thereafter. For non-CDP providers session() is a passthrough yielding self.
2. Iframe detection. Added _detect_iframe_block(page) helper that runs a small DOM probe checking iframe srcs (recaptcha / hcaptcha / challenges.cloudflare.com / turnstile / generic captcha) and JS challenge-API globals (window.grecaptcha / hcaptcha / turnstile). Called after parent-page text detection in all three call sites (_PersistentPageSession.fetch, .fetch_with_eval, and the legacy BrowserClient.fetch_with_eval). Best-effort: returns (False, "", "") on any error so it never breaks a successful fetch. Maps every detected family to reason="captcha" with a precise action_required string.
3. Differentiated retry policy. Rewrote _make_on_blocked() in scripts/fetch_documents.py to dispatch by reason: rate_limit → time.sleep(60) + return True (no human prompt; servers need time, not clicks); access_denied → print a one-line skip notice + return False (auth/subscription is not retry-able); captcha / login / unknown → existing banner + Telegram + input() + return True. Constants for the rate-limit backoff are top-level so they're easy to tune.
4. Anchor-based waits. Added wait_for parameter to BrowserClient.fetch_with_eval (and the session variant). When provided, page.wait_for_selector(wait_for, timeout=wait_ms) replaces the fixed sleep — typically returning in 500-1500 ms when content has rendered, with a 5000 ms hard cap before falling through. document_fetch.fetch_seed_page passes per-source result-list anchors from a new _WAIT_SELECTORS map (EBSCO: article[data-auto="search-result-item"] + legacy fallbacks; JSTOR: li.result; MUSE: .search-result, .result-item, article).

Validated end-to-end: --limit 5 against run_27f86e44394442 (post anchor-wait fix only): 40/40 articles in 26 s. --limit 20 (post anchor-wait fix): 160/160 articles (100%, was 136/160 = 85% before). Test suite: 162 → 163 (+1 iframe test), all passing.

Notes
The pause flow only retries ONCE per blocked URL — if the user's solve doesn't unblock the page (or they Ctrl-C out of input()), we save _blocked.html and move on. This avoids infinite loops at the cost of occasionally needing a second pass for stubborn pages. wait_for_selector falls through (does not raise) when the anchor never appears, so genuinely empty result pages and soft-blocked pages still get evaluated and the response is captured for inspection. The session() refactor is interface-compatible with the existing FastAPI run_fetch caller — _PersistentPageSession exposes the same fetch / fetch_with_eval / open_tabs / is_available methods as BrowserClient. A future enhancement would be page.wait_for_selector(captcha_iframe, state="detached") to wait IN-PLACE for the user to solve, avoiding the page.goto re-fetch on retry.

[2026-04-29] - Pipeline skipped CAPTCHA / login-wall pages instead of pausing for the user

Problem
During CLI fetch runs, when a provider page returned a CAPTCHA, "I'm not a robot" challenge, Cloudflare interstitial, login wall, or rate-limit notice, the pipeline emitted a "fetching/blocked" event but immediately moved on to the next URL. The user — sitting in front of the CDP Chrome window — had no chance to solve the challenge before the next navigation overwrote the page. Articles that could have been recovered with one human click were silently lost. This was observed in real runs: out of 160 expected articles at --limit 20, ~24 (15%) returned 0 extractions, some attributable to undetected CAPTCHA states.

Root Cause
Two issues:
1. Detection coverage. _BLOCK_SIGNALS in adapters/browser_client.py only matched a handful of phrases (access denied, captcha, please log in, authentication required, session expired, institutional access, not authorized). It missed the visible widget text "I'm not a robot" / "I am not a robot", Cloudflare's "Just a moment / Checking your browser" interstitial, rate-limit messages ("Too many requests", "Quota limit exceeded"), and explicit "you have been blocked" notices.
2. No retry path. fetch_seed_page detected blocked pages (when the regex matched) but only logged + saved _blocked.html and returned 0 — no callback, no pause, no retry. There was no way for the CLI to insert a human-in-the-loop step.

Solution
1. Expanded _BLOCK_SIGNALS in adapters/browser_client.py to include reCAPTCHA widget phrasing ("I'm not a robot", "I am not a robot", "verify you are human", "verify your humanity"), Cloudflare interstitials ("checking your browser", "just a moment"), rate-limit / quota wording ("too many requests", "rate limit", "quota limit exceeded", "quota violation"), and explicit-block notices ("you have been blocked", "your access has been blocked"). Each maps to a typed reason ("captcha" / "rate_limit" / "access_denied") with a useful action_required string.
2. Added an on_blocked: Optional[Callable] = None parameter to both fetch_seed_page() and run_fetch() in adapters/document_fetch.py. When a page is blocked, fetch_seed_page now calls on_blocked(item, page_result); if the handler returns True, the URL is re-fetched once. On retry success it emits a "fetching/unblocked" event. If still blocked (or no handler given), behavior is unchanged: save _blocked.html, return 0.
3. Wired up _make_on_blocked() in scripts/fetch_documents.py: when running with prompts enabled, it prints a clear "PAUSED — page blocked" banner with gap_id, source_id, URL, and action hint; sends a best-effort Telegram ping (per AGENTS.md §15, silent on failure if credentials absent); calls input("Press Enter once unblocked..."); then returns True so the library retries. With --no-prompt the on_blocked handler is None (preserves the existing skip-and-continue behavior for scripted use). Added 4 new tests in tests/test_document_fetch.py covering retry-on-True, skip-on-False, the new CAPTCHA phrases, and rate-limit detection. Total tests: 158 → 162, all passing.

Notes
The pause is per-blocked-page (one input() per blocked URL) — if many pages are blocked, the user sees one prompt per page rather than a global "everything stopped" prompt. The library still retries the URL only ONCE after unblock; if the page remains blocked after the user's intervention, the pipeline saves _blocked.html and moves on (avoids infinite loops). Telegram delivery is intentionally silent on failure so missing credentials never break the pause flow itself.

[2026-04-29] - EBSCO selectors stale after research.ebsco.com SPA migration

Problem
scripts/fetch_documents.py reported "Seed pages fetched: N / N (0 failed)" for every EBSCO query, yet "Articles extracted: 0" — silently producing no document records. Validation against run_27f86e44394442 (650 EBSCO seed URLs across 169 gaps) yielded zero markdown files even with a fully authenticated CDP browser session.

Root Cause
EBSCOhost migrated its post-login UI from search.ebscohost.com (legacy DOM with .result-list-item, [data-auto="record"], etc.) to a Next.js SPA at research.ebsco.com that uses CSS-module class names and a renamed data-auto-* attribute scheme (article[data-auto="search-result-item"], [data-auto="result-item-title__link"], [data-auto="result-item-metadata-content--contributors"], [data-auto="abstract-content"], [data-auto="result-item-metadata-content--published"], [data-auto="result-item-metadata-content--database"]). The _EBSCO_JS extractor in adapters/document_fetch.py still queried the legacy selectors only, so it walked an empty NodeList on every page. Page navigation and HTML save still succeeded, masking the failure.

Solution
Updated _EBSCO_JS in adapters/document_fetch.py: container query now matches the new article[data-auto="search-result-item"] first (legacy selectors retained as fallbacks for older skins). Per-field selectors prepend the new data-auto-* attributes ahead of legacy selectors. Added a new "database" field (Academic Search Ultimate, etc.) and an absolute "url" field built from the title link's href via new URL(href, location.origin) so downstream consumers get clickable links instead of relative SPA paths. Updated _write_ebsco_records to emit the new Database and URL lines in the saved markdown when present. Validated end-to-end: --limit 5 → 40 articles extracted (8/page); --limit 20 → 136 articles extracted (~85% rate). All 158 existing tests still pass.

Notes
About 15% of pages in the --limit 20 run returned 0 articles for back-to-back queries against the same source — appears to be a transient SPA render race, not a wait-time issue (a fresh manual probe of the same query returned 20 articles within 1500 ms). Re-running the script picks up the gaps because empty extractions don't write a slug.md, only search_results.html (which is skipped on subsequent runs by _save_html). A future enhancement could add a one-shot retry inside fetch_seed_page when eval_result is empty and not blocked. JSTOR and Project MUSE selectors in the same file have not been verified against their current live DOM and may need a similar pass.

[2026-04-29] - CLI auto-launches Chrome with CDP

Problem
scripts/fetch_documents.py printed a _CHROME_HELP block and blocked on input("Press Enter once Chrome is running...") when CDP was unreachable. Users had to manually copy the launch command into another terminal, which was error-prone and prevented scripted/non-interactive use even with --no-prompt.

Root Cause
The script had no mechanism to spawn Chrome itself. It could only detect whether Chrome was already running and print guidance if not.

Solution
Added _launch_chrome(port) — a new function in scripts/fetch_documents.py (~40 lines) that resolves the Chrome executable (macOS app bundle path first, then google-chrome/chromium on PATH via shutil.which), spawns Chrome with subprocess.Popen(start_new_session=True) pointing at a dedicated ~/.research_henchman_chrome user-data-dir, and returns the PID. Added _cdp_poll_until_ready(cdp_url) that polls /json/version every 0.5 s for up to 15 s using the same urllib + Host-header pattern as BrowserClient._playwright_cdp_ping(). The main() sign-in gate now auto-launches and polls by default; passing --no-launch restores the previous print-help-and-wait behavior. Added 2 new tests in tests/test_fetch_cli.py: one asserts Popen is never called with --no-launch; the other asserts Popen is called with --remote-debugging-port= and --user-data-dir= in default mode.

Notes
The ~/.research_henchman_chrome profile is dedicated and separate from the user's normal Chrome profile — no tab collisions. Library logins persist across CLI runs in that profile. If no Chrome executable is found, the script prints a clear error and exits non-zero. The --no-launch flag preserves full backwards compatibility for scripted or externally-managed environments.

[2026-04-29] - CLI Refactor: fetch_documents.py Rewritten as Thin Wrapper over document_fetch Library

Problem
scripts/fetch_documents.py duplicated virtually all logic already present in adapters/document_fetch.py: record classification, abstract saving, seed-page extraction (including the JS extractors for EBSCO/JSTOR/MUSE), PDF downloading, CDP ping, and tab-opening. This created two out-of-sync implementations where a bug fix or provider-DOM change would need to be applied in two places.

Root Cause
The CLI script was written before adapters/document_fetch.py existed as a standalone library. When the library was extracted for API use, the script was left intact rather than refactored to delegate, creating the duplication.

Solution
Rewrote scripts/fetch_documents.py as a thin CLI wrapper (~200 lines vs ~610 before). All fetch logic is now delegated to library functions: collect_fetch_items() for item collection, run_fetch() for the full fetch orchestration (seed extraction, PDF download, abstract saving), and make_browser_client(settings) for browser construction — exactly mirroring how main.py uses these functions. The script retains: run resolution (--run-id flag, API fallback, disk fallback), Chrome launch guidance, the interactive login-prompt gate (with new --no-prompt flag to skip all input() calls for scripted/non-interactive use), and a structured emit() callback that prints [stage/status] message lines. All existing flags (--run-id, --gap-id, --limit, --dry-run, --cdp-url) are preserved with identical semantics. Added 6 new tests in tests/test_fetch_cli.py covering --dry-run, --no-prompt, emit formatting, and port parsing.

Notes
The old script used plain dicts for fetch items; the library uses FetchItem dataclasses. The CLI now accesses fields as attributes (item.fetch_type, item.gap_id) rather than dict keys. No library files were changed.

[2026-04-29] - Post-Run Document Fetch: Full Article Retrieval via API and CDP Browser

Problem
Pipeline runs produced seed-only results for browser-backed sources (JSTOR, EBSCO, ProQuest, Gale). Users could see search-URL placeholders but had no in-app way to fetch the actual article content. The standalone CLI script (fetch_documents.py) used input() prompts which required a real terminal and could not be triggered from the web UI, creating a permissions/access blockage.

Root Cause
Document fetching required interactive terminal prompts (input() calls) for the Chrome CDP sign-in gate and the "press enter when done" confirmation. BrowserClient had no method to run JS expressions on a navigated page (needed for EBSCO/JSTOR DOM extraction). The sign-in infrastructure already existed in the web UI but was not wired to a post-run fetch action.

Solution
1. Added fetch_with_eval(url, js_expr, wait_ms) to BrowserClient — connects via CDP, navigates, waits for JS rendering, runs page.evaluate(js_expr), returns (PageResult, eval_result). Enables EBSCO, JSTOR, and Project MUSE DOM extraction through the existing authenticated browser session.
2. New adapters/document_fetch.py library — no input() calls, uses BrowserClient. Provides: collect_fetch_items(), preview_counts(), save_abstract(), fetch_seed_page() with per-provider JS extractors (EBSCO, JSTOR, Muse, generic HTML fallback), download_pdf() with direct HTTP then CDP fallback, run_fetch() orchestrator with structured emit events.
3. FetchDocumentsResult dataclass added to contracts.py; fetch_status and fetch_result fields added to RunRecord (backward-compatible defaults).
4. GET /api/orchestrator/runs/{run_id}/fetch_items — returns seed/pdf/abstract counts and CDP availability for UI preview.
5. POST /api/orchestrator/runs/{run_id}/fetch_documents — triggers background fetch task, emits progress events to the run's existing event stream, persists fetch_status/fetch_result on run record.
6. "Fetch Documents" panel added to static/index.html — appears after run completion (complete/partial), with Preview Items, Sign In to Databases (reuses existing /signin/open endpoint), and Fetch Documents buttons. Progress shown via existing live log. Polls fetch_status for completion summary.
7. 23 new regression tests in tests/test_document_fetch.py; full suite: 150 passed.

Notes
Blocked pages (CAPTCHA/login walls) are detected, emitted as fetching/blocked events with action hints, and saved as _blocked.html for manual inspection. Existing fetch_documents.py CLI script preserved for headless/scripted use. fetch_with_eval() falls back gracefully on HTTP provider (returns None eval result).

[2026-04-25] - Layer 6: Chart Generation from Data Pull Artifacts

Problem
Raw pull outputs were JSON files only. No visual representation of data; user had no way to quickly understand what BLS, FRED, World Bank, or EBSCO pulls actually returned.

Root Cause
Pipeline had no rendering stage. All data artifacts stayed as machine-readable JSON in pull_outputs/.

Solution
Added Layer 6 render stage: `layers/render.py`, `RenderResult` contract, `RunStatus.RENDERING`, `ORCH_AUTO_RENDER_CHARTS` setting. Pipeline runs render after fit, before export. Charts are written as PNG into the same source directory as JSON artifacts so artifact_export.py picks them up automatically.

Chart types per source:
- bls: line chart of time-series CPI/economic data points (year+period → value)
- fred: horizontal timeline bar chart showing observation span per series, colored by popularity
- ebsco_api/ebscohost: stacked bar chart of publication year distribution by quality label (high/medium/seed)
- world_bank: horizontal bar chart of indicator count by topic
- bea/census/ilostat/oecd: skipped (return metadata only, no plottable values)

Gap _README.md files now embed chart images via markdown `![caption](documents/<source>/chart_*.png)` via new `_chart_section()` in artifact_export.py.

Notes
12 new tests in tests/test_render.py cover all source types, empty data, unresolvable gaps, and skipped sources. matplotlib Agg backend used for headless rendering (no display required).

[2026-04-25] - Gap Analysis: Batched Paragraph-Level LLM Analysis Replaces Single Manuscript Call

Problem
Gap analysis found only 10 gaps max on a 15-chapter manuscript. The per-section LLM call hit context limits and returned a thin sample.

Root Cause
Analysis made one LLM call per manuscript section, each containing many paragraphs. Context limits (~8K tokens) caused the model to truncate or skip most content. The model also received vague "Consider its position in the argument" framing that allowed rationalizing away gaps.

Solution (per Opus architecture review)
1. `_annotate_paragraphs()`: Splits full manuscript on blank lines, annotates each block with its current chapter heading. Preserves paragraph boundaries and chapter attribution.
2. `_score_paragraph()`: Heuristic 0-100 suspicion score using regex signals — causal language, statistics, superlatives, hedges, and explicit markers. Citations reduce but don't eliminate score.
3. `_MAX_LLM_PARAGRAPHS = 40`: Heuristic pre-filter selects top 40 most suspicious paragraphs, reducing LLM call count.
4. `_build_batch_prompt()`: Batches 4 paragraphs per LLM call. Context = thesis sentence + chapter title + preceding paragraph only. Adversarial fact-checker framing: enumerate ALL assertions, mechanical citation test, anti-rationalization default. Requires `paragraph_index` field (1-4) in each output row.
5. `_analyze_with_ollama()` Stage 3 rewrite: Groups top_sorted paragraphs by chapter → chunks into batches of 4 → prev_para lookup uses `{id(p): i}` position map (not `text.index()` which breaks on duplicate text) → routes response rows back to source paragraph via `paragraph_index`.
6. Dropped per-chapter role summarization (15 extra LLM calls that hurt recall by providing narrative framing excuses).
7. Expected: ~11 total LLM calls × 30s = 330s for a 15-chapter manuscript.

Notes
`original_indices = {id(p): i for i, p in enumerate(annotated)}` built in Stage 2 and reused in Stage 3 for prev_para and top_sorted document-order restoration. Text-match lookup via `annotated_texts.index()` was replaced because duplicate paragraph text would return the wrong position.

Opus architecture review recommendations (documented):
- Keep qwen2.5:7b — gap detection is lexical/structural, not reasoning-heavy; 32B too slow for pipeline budget
- Drop chapter role summaries — small models use chapter framing as excuse to skip claims
- Batch 4 paragraphs per call — reduces 80 calls to 10, fits 40 paragraphs in one context window pass
- Adversarial fact-checker prompt — forces enumeration, mechanical citation test, anti-rationalization

[2026-04-24] - Test False Positive: Empty Per-Source Pull Directory Treated as Failure
Problem
`scripts/test_run.py` reported FAIL with "NO FILES in .../oecd" and "NO FILES in .../ilostat" even though the pipeline ran correctly. OECD and ILOSTAT legitimately return no results for e-commerce history queries — they are the wrong domain for that manuscript.
Root Cause
`check_pull_artifacts` added a warning for every source run-directory that contained zero files, and the caller added all warnings to the failures list. A per-source empty result is not a system failure — it means the source had no relevant content.
Solution
Separated the return value of `check_pull_artifacts` into `hard_failures` (total zero artifacts across all sources — a real system failure) and `soft_notes` (per-source empty directories — informational only). Main function now only adds hard failures to the test failures list; soft notes are printed with "(note)" prefix.
Notes
A full-zero artifact run is still a hard failure. Per-source empty is expected and fine.

[2026-04-24] - EBSCO EIT REST API and Playwright CDP Adapter Implemented
Problem
EBSCO pulls only returned seed click-through links. Two new retrieval paths (EIT REST API and Playwright CDP) were needed to fetch real article records.
Root Cause
EIT API requires a 3-part profile string `<account_id>.<group>.<profile_id>` — the library only provided `eitws2` (the profile ID). EDS API credentials are web-UI logins, not provisioned EDS API profiles. Both require separate provisioning from JHU library IT.
Solution
1. Added `_eit_search()` / `_parse_eit_xml()` to `EbscoApiAdapter` in `adapters/keyed_apis.py`. EIT REST endpoint: `http://eit.ebscohost.com/Services/SearchService.asmx/Search`. Profile built from `EBSCO_ACCOUNT_ID.EBSCO_GROUP_ID.EBSCO_PROFILE_ID`. `pull()` now tries EIT → EDS → seed fallback.
2. Implemented `EbscohostPlaywrightAdapter` in `adapters/playwright_adapters.py` with CDP connect (`http://localhost:9222`), JS DOM extraction via `page.evaluate()`, multi-selector fallbacks for EBSCOhost HTML, detail-page abstract fetching, and era-date URL params (`DT1`/`DT2`).
3. Saved EIT WSDL to `docs/ebsco_eit_wsdl.xml` and credentials/endpoint documentation to `docs/ebsco_eit_api.md`.
Notes
EIT is blocked until JHU library IT provides the account ID prefix (e.g. `s8875689`) so full profile `s8875689.main.eitws2` resolves. Playwright CDP activates once Chrome is launched with `--remote-debugging-port=9222` and user is logged into EBSCOhost.

[2026-04-24] - EbscoApiAdapter Was Not Calling the EDS API
Problem
Every EBSCO pull produced only seed links (search.ebscohost.com click-through URLs). No article titles, abstracts, or full text were ever retrieved.
Root Cause
The adapter never called the EBSCO Discovery Service (EDS) API at all. It only called build_link_rows() which constructs provider search URLs. The credentials in .env (EBSCO_PROF, EBSCO_PWD, EBSCO_PROFILE_ID) were completely unused.
Solution
Replaced the adapter with a full EDS API implementation:
  1. POST /authservice/rest/uidauth → AuthToken
  2. GET  /edsapi/rest/createsession → SessionToken
  3. GET  /edsapi/rest/search (with query + DT1/DT2 era limiters) → records
  4. GET  /edsapi/rest/retrieve (DbId + AN) → full-text HTML or PDF links per record
  5. GET  /edsapi/rest/endsession
Records are parsed for title, authors, journal, abstract, DOI, PDF URL, and full-text HTML. Quality label is "high" (full text), "medium" (abstract), or "seed" (link only). When EDS auth fails (invalid/missing credentials) the adapter falls back to seed click-through URLs with a clear api_error field in stats.
Notes
The credentials in .env return EDS auth error 1102 "Invalid Credentials" — the web UI login (lhyman6@jh.edu) is not an EDS API profile. JHU library IT needs to provision an EDS API profile in EBSCOadmin, or IP-based authentication can be configured. The code is fully ready for valid credentials.

[2026-04-24] - Export Bundle Race Condition: Status Set Before Files Written
Problem
End-to-end test reported _INDEX.md, _BIBLIOGRAPHY.md, and _README.md missing from the export bundle even though the files existed on disk at the time of inspection.
Root Cause
pipeline.py called `save(final_status, "Run complete")` to set status to "complete" BEFORE calling `export_run_bundle()`. The test (and any external caller) polls for "complete", then immediately checks the bundle — but the export hadn't started yet, so the files appeared missing.
Solution
Moved `export_run_bundle(rec, settings)` to run before `save(final_status, ...)` in pipeline.py so the bundle is fully written to disk before the status transitions to "complete".
Notes
The race only affected callers that check bundle contents right after polling for terminal status.

[2026-04-24] - Reset Wrote Wrong Empty Value to events.json
Problem
After clicking Reset in the sidebar, all new runs would get stuck in "queued" state forever with no events appearing.
Root Cause
The reset endpoint wrote `{}` (empty dict) to both `runs.json` and `events.json`. But `runs.json` is a dict keyed by run_id while `events.json` is a flat list. `store.append_event()` calls `events.append(event)` — which fails with `AttributeError: 'dict' object has no attribute 'append'` on a dict. Since this happens inside the background thread before any status update, the run stays in "queued" silently.
Solution
Reset endpoint now writes `{}` for `runs.json` and `[]` for `events.json`, matching the shape each file expects.
Notes
Runs created before the fix can be unblocked by hitting the Retry button.

[2026-04-22] - Fix Settings API Shape Mismatch and Add Provider Dropdowns
Problem
React settings modal showed empty LLM Provider and Browser Provider fields. Selecting a provider value had no effect.
Root Cause
`fetchSettings()` in `api.ts` was returning the raw `/connections/values` response (`{env_path, values[]}`) unmodified. The modal was reading `settings.llm_provider` which is never a key in that shape. Also used free-text inputs for enum-valued fields (ollama/claude/openai, playwright_cdp/http/claude_cu).
Solution
Updated `fetchSettings()` to transform the values array into a flat `key→value` dict and add `llm_provider`/`browser_provider`/`library_system` aliases. Changed Settings modal LLM/browser provider inputs to `<select>` dropdowns with the valid enum options.
Notes
All credential fields still work since they read `settings['ORCH_*']` which is now populated from the flat dict.

[2026-04-22] - Historian Overhaul: LLM + Browser Abstractions, Export Redesign, React Frontend
Problem
App had LLM calls scattered across 4 layer files (each with its own _call_ollama function), browser/Playwright calls hardcoded in adapters with no provider abstraction, a flat artifact export structure using opaque gap_id folder names, and a 2,000-line monolithic vanilla JS frontend.
Root Cause
Original design grew organically without provider abstraction layers. Each layer had its own HTTP call to Ollama. Browser automation was tightly coupled to Playwright/CDP. Export structure used internal IDs instead of human-readable names. Frontend had no component system.
Solution
1. Created `layers/llm_client.py`: LLMClient abstraction with Ollama (default), Claude, and OpenAI backends. `complete()` → str, `complete_json()` → dict with retry. Config selects provider via ORCH_LLM_PROVIDER. All 4 call sites (analysis, reflection, search_policy, fit) now use make_llm_client(settings).
2. Created `adapters/browser_client.py`: BrowserClient abstraction with PlaywrightCDP (default), HTTP, and Claude Computer Use (stub) backends. PageResult envelope with blocked-page detection. `fetch()`, `probe_login()`, `open_tabs()`. Config selects via ORCH_BROWSER_PROVIDER. Updated seed_url_fetch.py and main.py sign-in open to delegate to BrowserClient.
3. Added `llm_provider` and `browser_provider` fields to OrchestratorSettings.
4. Rewrote `artifact_export.py` for historian-friendly output: human-readable gap folder slugs from chapter+claim text, `_README.md` per gap (claim, context, sources table, Ollama synthesis), `_INDEX.md` master cross-reference table, `_BIBLIOGRAPHY.md` deduplicated URLs, `by_chapter/` mirror, `synthesis/` Ollama-generated "what was found / what's missing" summaries. Documents moved to `gaps/<slug>/documents/<source>/` instead of `gaps/<id>/related_documents/<source>/`.
5. Added SSE streaming endpoint `/api/orchestrator/runs/{id}/stream` via sse-starlette (keeps polling endpoint for backwards compat).
6. Built complete React frontend at `frontend/` (Vite + React 18 + TypeScript + Tailwind). FastAPI serves `frontend/dist/` with SPA fallback route. Components: PipelineRail, GapCard with ConfidenceBar + AccordionLadder, EvidencePanel (framer-motion slide-in), SignInSplash, SettingsModal.
7. Created 3 historian test manuscripts in `Manuscript/`: labor_history_new_deal.md, civil_rights_voting_rights.md, federal_reserve_early_history.md.
Notes
Backwards compat: `_call_ollama` kept as shim in analysis.py (reflection.py imports it). Polling events endpoint kept alongside SSE. Legacy static/index.html preserved alongside React build. All 114 tests pass after updating monkeypatching targets to new function names.

[2026-04-05] - Open Sign-In Splash Tabs in Active Playwright CDP Session
Problem
Clicking `Open Sign-In Pages` from the sign-in splash could open tabs only in the current UI browser window, which did not always match the browser session used for Playwright login tests/pulls. Users then had to sign in again.
Root Cause
Frontend splash action used `window.open(...)` only, so sign-in links were not opened through the attached CDP browser context that powers `Test Login` and seed URL pulls.
Solution
Added `POST /api/orchestrator/signin/open` (`SignInOpenInput`) in `main.py` to open selected sign-in URLs in the active CDP browser session. Updated sign-in splash UI (`static/index.html`) so `Open Sign-In Pages` calls this endpoint first and logs open counts; if CDP opening fails, it falls back to local `window.open(...)` tabs.
Notes
This preserves existing workflows while aligning sign-in actions with the authenticated Playwright session whenever CDP is available.

[2026-04-05] - Add Mandatory Sign-In Splash Before Login Tests
Problem
Users could click `Test Login` without a clear, explicit pre-check instruction to sign into university/provider systems first, causing confusing blocked results.
Root Cause
Login tests launched immediately from run/settings UI actions without an interstitial prompt that emphasized required sign-in behavior.
Solution
Updated `static/index.html` to add a blocking sign-in splash modal shown before both run-level and per-database login tests. The splash lists target providers, includes `Open Sign-In Pages`, and requires explicit continue/cancel before test execution.
Notes
This is a UI workflow clarity change only; sign-in test API contracts are unchanged.

[2026-04-05] - Reduce Playwright Focus Stealing with Background-First CDP Fetch
Problem
Automated Playwright checks could pull browser focus by opening tabs while testing/pulling provider URLs.
Root Cause
CDP retrieval used direct page navigation as the primary path, which may create/focus transient tabs in attached browser sessions.
Solution
Updated `adapters/seed_url_fetch.py` CDP flow to try a storage-state request-context fetch first (authenticated background request) and only fall back to opening a transient page when needed.
Notes
This is a best-effort focus reduction; some provider flows may still require page fallback depending on site behavior.

[2026-04-05] - Prevent CDP Login Tests From Closing User Browser Session
Problem
Clicking login/test actions could make the Chrome debug window disappear, interrupting sign-in and causing follow-up CDP connection failures.
Root Cause
CDP fetch helper (`_fetch_via_cdp`) called `browser.close()` after `connect_over_cdp(...)`, which can close the attached user browser session instead of only cleaning up transient test artifacts.
Solution
Updated `adapters/seed_url_fetch.py` CDP fetch flow to avoid closing the connected browser. The helper now closes only a temporary context when one is explicitly created, and leaves the user’s existing browser session intact. Added regression coverage in `tests/test_seed_url_fetch.py` to ensure browser close is not invoked for attached CDP sessions.
Notes
This is a runtime behavior fix only; API contracts and pull outputs are unchanged.

[2026-04-05] - Add Per-Database Login Test Controls in Settings
Problem
Users could test login readiness only from the run preflight flow, but there was no quick way in Settings to verify individual library databases and see pass/fail state per provider.
Root Cause
Settings database rows were informational only; they did not include source-specific login probe actions or persistent row-level status indicators.
Solution
Updated `static/index.html` Settings database rendering to include a `Test Login` button on each row and row-level status badges. Wired these actions to `POST /api/orchestrator/signin/test` with `source_ids=[source_id]`, and display green for pass (`ok`) and red for blocked/unreachable outcomes, including diagnostic messages.
Notes
This is an additive UI enhancement. It reuses existing sign-in test backend contracts and does not change pipeline execution behavior.

[2026-04-05] - Add Semantic Workflow Colors for Ready/Blocked/Completed States
Problem
Workflow status cues were visually inconsistent, making it harder to quickly tell whether a stage was ready to proceed, blocked, or fully completed.
Root Cause
Status text and stage cards used mixed styles without a strict semantic mapping, and sign-in/launch state transitions did not consistently apply explicit state classes.
Solution
Updated `static/index.html` workflow styling and state transitions so semantic colors are enforced across launch/sign-in/stage surfaces: green for `ready`, red for `blocked`, and black for `completed`. Added explicit status helpers in UI logic to apply consistent state classes and synced sign-in box border styling with the same state model.
Notes
This is a frontend UX clarity improvement only; orchestration contracts and backend pipeline behavior remain unchanged.

[2026-04-05] - Make Sign-In Checklist Manuscript-Aware via Analysis Preflight
Problem
Pre-run login checklist was generated from profile-level availability only, so users could be asked to sign into providers not actually needed for the selected manuscript run.
Root Cause
Sign-in target generation happened before manuscript-specific analysis/reflection planning, so it lacked knowledge of planned provider routes.
Solution
Added `POST /api/orchestrator/signin/preflight` to run analysis+reflection preflight for the selected manuscript and derive sign-in targets from planned source IDs. Updated frontend sign-in stage to require `Analyze Sources` before login confirmation, and wired `Test Login` to probe those manuscript-derived targets.
Notes
This is additive and contract-safe. Full run pipeline stages are unchanged; this only improves pre-run targeting precision for login checks.

[2026-04-05] - Add Pre-Run "Test Login" Provider Access Probe
Problem
Users could mark pre-run sign-in complete without any direct verification that their active browser/library session could access required provider platforms.
Root Cause
The sign-in stage only rendered checklist links and manual confirmation; there was no automated provider-access probe tied to active library profile and source availability.
Solution
Added `POST /api/orchestrator/signin/test` in `main.py` to probe active provider sign-in URLs and return per-source status (`ok`, `blocked`, `unreachable`) with fetch mode, blocked reason, and action hints. Added `probe_sign_in_access(...)` in `adapters/seed_url_fetch.py` (CDP-first with direct-HTTP fallback) and wired a new `Test Login` button in `static/index.html` to run this probe and render status per platform before launch.
Notes
This is additive and contract-safe. Run launch gating remains user-confirmed (`Mark Sign-In Complete`), while `Test Login` provides explicit readiness diagnostics.

[2026-04-05] - Include Playwright Python Client in Docker Runtime
Problem
Dockerized runs could report Playwright source availability but still never perform browser-backed seed URL fetches, leaving pull output at seed links only.
Root Cause
`adapters/seed_url_fetch.py` uses `playwright.sync_api` for CDP-backed fetch fallback, but the Docker image dependencies did not include the Playwright Python package. Import failed and fetch silently returned empty.
Solution
Added `playwright==1.54.0` to `requirements.txt` so container runtime includes the Playwright client needed for `connect_over_cdp(...)` calls during seed URL resolution.
Notes
This does not require bundled browser binaries for current usage because runtime attaches to an external Chrome CDP session.

[2026-04-05] - Normalize Docker CDP Hostname for Playwright Browser Attach
Problem
Docker runs reported Playwright/CDP as unavailable even when Chrome remote debugging was active on the host, so browser-backed source pulls could not execute.
Root Cause
When `ORCH_PLAYWRIGHT_CDP_URL` used `host.docker.internal`, Chrome DevTools returned HTTP 500 because the request Host header was a hostname rather than `localhost`/IP. Availability probe and CDP fetch code used the hostname directly.
Solution
Added `adapters/cdp_utils.py` with `effective_cdp_url(...)` to normalize `host.docker.internal` to its resolved IP before probing/connecting. Wired this into both `check_cdp_endpoint(...)` and seed URL CDP fetch (`_fetch_via_cdp(...)`) so health checks and real browser pulls share the same fix path. Added regression tests in `tests/test_cdp_utils.py`.
Notes
This is contract-safe and runtime-focused. Existing `.env` values remain valid; Docker Playwright attach is now resilient to Chrome host-header constraints.

[2026-04-05] - Add Pre-Run Sign-In Stage and Launch Gate in Run Workflow
Problem
Users could start runs immediately without an explicit login step, which made authentication-dependent pulls fail later (or produce blocked pages) without a clear pre-run operator action point.
Root Cause
The Run UI had no dedicated preflight stage for platform authentication and no launch gate requiring users to confirm they had signed into required library/provider systems.
Solution
Updated `static/index.html` to add a `Pre-Run Sign-In Stage` in the Launch panel. The stage loads sign-in checklist entries from active source catalog + health availability, renders open-platform links, and requires explicit `Mark Sign-In Complete` confirmation before `Run Research` can start. Also added a visible `signin` stage in the stage rail and reset sign-in readiness on manuscript changes/uploads.
Notes
This is a frontend workflow/control-plane change only; backend run contracts are unchanged.

[2026-04-05] - Surface CAPTCHA/Login Blockers and Prefer API-Family Sources
Problem
Runs could report successful pulls while actually storing blocked login/challenge HTML snapshots, and users were not explicitly told when manual CAPTCHA/login bypass was required. Source selection could also spend effort on same-family Playwright routes (for example `ebscohost`) even when keyed API routes (`ebsco_api`) were available.
Root Cause
Resolved snapshot artifacts were treated as medium/high pull evidence by default, with no blocked-page classification or run-event warnings. Pull source ordering had no family-level API preference pass, so browser fallback could remain in the candidate list despite an available API source.
Solution
Added blocked-page detection in `adapters/seed_url_fetch.py` for common CAPTCHA/challenge/login/access-denied signals, tagged blocked rows with `blocked_reason`/`action_required`, and demoted those rows to seed quality so they do not count as real pulled evidence. Wired blocked stats (`blocked_files`, `captcha_blocks`, `challenge_blocks`, `login_blocks`) into Playwright and keyed adapters, and emitted explicit `pulling/warn` events in accordion execution instructing users to complete provider verification/login in-browser then retry. Added source-family API preference in `layers/pull.py` so keyed sources are preferred over same-family Playwright fallbacks (for example keep `ebsco_api`, skip `ebscohost` when both are present). Exposed blocked metadata in document API rows and UI rendering/log styling.
Notes
This improves transparency and pull quality accounting without changing API contracts. Source-specific full-text extraction workflows are still needed for deeper retrieval beyond search/login pages.

[2026-04-03] - Prefer JSON Packet Links Over Raw Resolved-File Packets in Results API
Problem
Run document views could become noisy and misleading because `/runs/{run_id}/documents` treated nested `_resolved_urls`/`_fetched_urls` files as top-level packets, creating duplicate rows and inflated quality counts.
Root Cause
Packet indexing walked all files under each adapter run directory and built packets for both JSON packet files and nested resolved artifacts. Flattened rows then repeated the same source artifact through multiple packet paths.
Solution
Updated `main.py` document indexing to make JSON packet files the source of truth, skip nested resolved/fetched URL files as standalone packets, and dedupe flattened rows by stable evidence/locator keys. Added direct-file quality calibration helper and preserved link metadata (`title`, `link_type`, `source_key`) in flattened API rows. Added regression coverage in `tests/test_main_api.py` to ensure resolved artifacts are surfaced as linked docs, not duplicate packets.
Notes
This is contract-safe: API shapes remain unchanged, but packet quality/readability now better reflects actual pulled evidence.

[2026-04-03] - Resolve Seed Search URLs Into Pulled Local Artifacts During Adapter Pulls
Problem
Runs could complete with seed-only provider-search rows (for example Project MUSE/JSTOR placeholder links), so click-through often landed on broad search pages and still required manual source hunting.
Root Cause
Playwright/keyed seed adapters emitted provider/local link rows but did not execute a follow-on retrieval pass to pull concrete page/document artifacts from those seed URLs.
Solution
Added `adapters/seed_url_fetch.py` and wired it into `PlaywrightAdapter._link_seed_result(...)` and `EbscoApiAdapter.pull(...)`. Seed/provider URLs are now fetched into per-query `_resolved_urls/<query>/` folders, child links are selectively followed, and pulled artifacts are appended as `resolved_snapshot` rows with medium/high quality labels. Added tests (`tests/test_seed_url_fetch.py`, expanded `tests/test_adapter_links.py`) and verified end-to-end runs now produce non-seed pulled artifacts for previously seed-only sources.
Notes
Source-specific extraction remains improvable, but this closes the seed-only gap by ensuring adapters attempt concrete pull artifacts as part of normal run execution.

[2026-04-02] - Add Stable Evidence IDs and Snippet-Linked Document References
Problem
Users could open pulled sources, but links back to the exact support point were fragile and required re-reading full source materials to relocate relevant passages.
Root Cause
Document packet rows lacked deterministic evidence references and snippet-level metadata. Results UI links pointed to broad source URLs/files without stable quote hashes or reusable evidence lookup paths.
Solution
Added deterministic `evidence_id` generation in document indexing based on normalized locator + quote hash, attached snippet metadata (`excerpt`, `quote_hash`, `source_locator`) to linked document rows, and generated best-effort text-fragment jump links (`anchor_url`) for URL sources with excerpt text. Added evidence lookup APIs (`/api/orchestrator/runs/{run_id}/evidence/{evidence_id}` and `/api/orchestrator/evidence/{evidence_id}`) and updated UI rendering to expose stable evidence references and snippet-open actions.
Notes
This is the basic stable-linking layer only. Multi-LLM evidence arbitration remains intentionally separate as an advanced feature.

[2026-04-02] - Add Switchable Frontend Interface Variants for Run + Settings
Problem
Operators needed to compare multiple frontend interface styles quickly without branching code or losing runtime functionality while evaluating UX direction.
Root Cause
The app shipped with a single visual system in `static/index.html`, so style experiments required manual code edits and page reloads with no persistent style preference.
Solution
Added a top-level `Interface Style` selector with three variants (`editorial`, `operations`, `atlas`) and local-storage persistence (`orchestrator_v2_ui_variant`). Implemented variant-specific typography/color/spacing/layout tokens while keeping all API/run behavior unchanged. Added responsive override guards so variant desktop grids reset correctly on mobile. Documented variant theses and usage in `docs/frontend_interface_variants.md`, and updated app/docs references.
Notes
This is presentation-only and contract-safe: backend APIs, run orchestration, and settings persistence semantics are unchanged.

[2026-04-02] - Refresh Gap Export Folders Per Run and Follow Seed URLs Into Gap Artifacts
Problem
Manuscript gap folders could contain stale files from previous runs of the same manuscript, and seed-link exports often stayed at provider-search URL placeholders instead of fetching linked page/document artifacts into gap folders.
Root Cause
Bundle export reused the same manuscript-title directory across runs without clearing prior `gaps/` content, so old artifacts persisted. URL follow-up was limited to raw href traversal and could waste child fetch attempts on static assets.
Solution
Updated `artifact_export.py` to clear `manuscript_exports/<title>/gaps` at the start of each export, ensuring each run produces a fresh per-gap artifact snapshot. Added best-effort URL follow fetch from copied source JSON URLs into `_fetched_urls` and filtered child-link traversal to skip obvious static asset extensions. Added regression tests for URL-follow fetch behavior and stale-gap cleanup in `tests/test_artifact_export.py`, and documented refreshed gap export semantics in `docs/orchestrator_app.md`.
Notes
This is additive and contract-safe: report/manifest filenames remain per-run, while gap artifact folders now reflect the latest run only for that manuscript title.

[2026-04-02] - Make Repo-Root Runs Work and Refresh Saved .env Values Immediately
Problem
Fresh GitHub clones did not run locally with documented commands because source/tests expected an `app.*` package path that was not present in this checkout, and Settings saves could appear stale because process env values continued to shadow newly saved `.env` values.
Root Cause
Imports and run/test commands were still aligned to an older package layout. In addition, `.env` save flow updated the file but did not evict edited keys from `os.environ`, so subsequent reads favored stale in-process values.
Solution
Updated imports and run/test entrypoints to repo-root module paths (`main:app`, `tests/`), aligned Docker/compose path assumptions to the current checkout, and restored a first-class Settings page for library profile + credential management. Added `/api/orchestrator/library/profiles` for selectable base library systems. Updated `.env` persistence helpers to allow blank updates, and updated connection-save flow to clear edited keys from process env so refreshed values reflect saved `.env` content immediately.
Notes
Contracts remained additive. Regression coverage was added for library-profile endpoint behavior and blank-value `.env` saves. Full suite passes via `python3 -m pytest tests -q`.

[2026-04-14] - Accordion Search Model with Era Vocabulary
Problem
Historical manuscript claims were routed to wrong source families (e-commerce claims hitting macro-stat APIs). Queries used only modern vocabulary and missed the historical record that used period terminology. Zero-result queries were logged and abandoned with no systematic broadening.
Root Cause
`_claim_routing_profile` used keyword regex that missed commerce/platform/retail vocabulary, routing claims to `OTHER/MIXED` at 0.46 confidence. `_clean_queries` filtered existing queries but generated no era-equivalent vocabulary. No backoff existed to recover from zero results by trying related period terms.
Solution
New module `layers/search_policy.py` implements the accordion model:
1. One LLM call per gap (temperature=0, ~25s timeout) generates a `SynonymRing` with three vocabulary drift types (terminology_shifts, institutional_names, era_modifiers) plus a four-rung `AccordionLadder` with {PRIMARY} templates.
2. `get_accordion_move` drives execution: lateral through synonyms at current scope before widening to the next rung. Five actions: accept, lateral, widen, tighten, exhausted.
3. Synonym ring and ladder stored on `gap.query_ladder` for auditability and retry without re-calling the LLM.
4. Heuristic fallback (empty synonym ring, regex classifier) on Ollama failure.
5. All accordion state emitted as structured log events, visible in UI run log.
6. Plan cards in frontend show synonym ring categories, rung templates, and era range.
Notes
`era_start`/`era_end` extracted by LLM and stored on `SynonymRing`; see [2026-04-14] date-range faceting entry below for adapter wiring.
Subject heading pivot and archival finding-aid sources bracketed for future sprint.

[2026-04-14] - Era Date Range Faceting in Provider Search URLs and BLS
Problem
Provider click-through search URLs were era-blind: JSTOR, EBSCO, ProQuest, and other database URLs generated by adapters contained no date facets, so users clicking through landed on unfiltered results even when the LLM had already identified the claim's historical era. BLS time-series calls used a hardcoded 2019–2024 window regardless of the manuscript's period.
Root Cause
`era_start`/`era_end` were extracted by the accordion model LLM call and stored on `SynonymRing` in `gap.query_ladder`, but `provider_search_url` and `build_link_rows` in `adapters/document_links.py` had no parameter for era bounds, so adapter `pull()` calls could not forward them. BLS `BlsAdapter.pull()` had literal string values `"2019"/"2024"` that were never connected to the claim's era.
Solution
Added `era_start`/`era_end` optional params to `provider_search_url` and `build_link_rows` in `adapters/document_links.py`. Date-range URL parameters are now appended for sources that support faceting: JSTOR (`sd`/`ed`), ProQuest (`daterange=custom`, `startdate`/`enddate`), EBSCOhost (`DT1`/`DT2` in YYYYMMDD format), Gale (`startDate`/`endDate`), and Americas Historical Newspapers (`date_low`/`date_high`). Added `era_years_from_gap()` helper in `adapters/io_utils.py` to extract era bounds from `gap.query_ladder` safely. Updated `BlsAdapter.pull()` to call this helper and use `era_start`/`era_end` as `startyear`/`endyear`, falling back to `"2019"`/`"2024"` when no era is available. Updated `EbscoApiAdapter.pull()` and `PlaywrightAdapter._link_seed_result()` to extract era bounds from the gap and pass them to `build_link_rows`. Added 12 new regression tests covering URL parameter injection, `era_years_from_gap` edge cases, and adapter propagation.
Notes
No API contract changes. `provider_search_url` remains backward-compatible (era params default to None, producing identical output when omitted). Per-source noise thresholds remain a follow-up item.

[2026-04-02] - Load Local .env From Repository Root in API Runtime
Problem
API health and runs showed keyed APIs as unavailable (`missing_keys`) even when valid credentials existed in the repository `.env`, leading to avoidable zero-result routing quality in local runs.
Root Cause
Runtime settings in `main.py` defaulted `ORCH_WORKSPACE` to the parent of the repository root, so `load_runtime_env(...)` read the wrong path and skipped the project `.env`.
Solution
Updated `_settings()` to default workspace to `BASE_DIR` (repository root), ensuring local `.env` is loaded consistently when `ORCH_WORKSPACE` is unset. Added regression test asserting default workspace equals repo root.
Notes
This is contract-safe and local-runtime focused. Explicit `ORCH_WORKSPACE` still overrides default behavior.
