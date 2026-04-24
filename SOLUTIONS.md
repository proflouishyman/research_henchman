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
