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

[2026-04-02] - Load Local .env From Repository Root in API Runtime
Problem
API health and runs showed keyed APIs as unavailable (`missing_keys`) even when valid credentials existed in the repository `.env`, leading to avoidable zero-result routing quality in local runs.
Root Cause
Runtime settings in `main.py` defaulted `ORCH_WORKSPACE` to the parent of the repository root, so `load_runtime_env(...)` read the wrong path and skipped the project `.env`.
Solution
Updated `_settings()` to default workspace to `BASE_DIR` (repository root), ensuring local `.env` is loaded consistently when `ORCH_WORKSPACE` is unset. Added regression test asserting default workspace equals repo root.
Notes
This is contract-safe and local-runtime focused. Explicit `ORCH_WORKSPACE` still overrides default behavior.
