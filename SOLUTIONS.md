[2026-04-02] - Make Repo-Root Runs Work and Refresh Saved .env Values Immediately
Problem
Fresh GitHub clones did not run locally with documented commands because source/tests expected an `app.*` package path that was not present in this checkout, and Settings saves could appear stale because process env values continued to shadow newly saved `.env` values.
Root Cause
Imports and run/test commands were still aligned to an older package layout. In addition, `.env` save flow updated the file but did not evict edited keys from `os.environ`, so subsequent reads favored stale in-process values.
Solution
Updated imports and run/test entrypoints to repo-root module paths (`main:app`, `tests/`), aligned Docker/compose path assumptions to the current checkout, and restored a first-class Settings page for library profile + credential management. Added `/api/orchestrator/library/profiles` for selectable base library systems. Updated `.env` persistence helpers to allow blank updates, and updated connection-save flow to clear edited keys from process env so refreshed values reflect saved `.env` content immediately.
Notes
Contracts remained additive. Regression coverage was added for library-profile endpoint behavior and blank-value `.env` saves. Full suite passes via `python3 -m pytest tests -q`.
