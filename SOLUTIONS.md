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
