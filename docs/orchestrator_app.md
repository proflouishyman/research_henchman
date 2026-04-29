# Orchestrator App (v2)

## Purpose
Provide a contract-enforced research pipeline where the user selects a manuscript and starts one run. The run record owns the full lifecycle:
- manuscript analysis
- LLM plan reflection
- source pulls
- ingest
- LLM fit

## Architecture
- `contracts.py`: all layer dataclasses and enums (`GapMap`, `ResearchPlan`, `GapPullResult`, `IngestResult`, `FitResult`, `RunRecord`).
- `layers/analysis.py`: Layer 1 analysis (provider-abstracted LLM with heuristic fallback, fingerprint cache).
- `layers/search_policy.py`: Accordion search-policy layer (claim classification, era synonym ring, ladder generation, move-state logic, hash cache).
- `layers/reflection.py`: Layer 2 reflection + policy gates (claim typing, evidence typing, query-quality gate, ladder persistence on `PlannedGap.query_ladder`, local review pass for low-confidence routes).
- `layers/pull.py`: Layer 3 router + `SOURCE_REGISTRY` + `SOURCE_CAPABILITIES` semantic routing table.
- `layers/ingest.py`: Layer 4 ingest, tags artifacts with `gap_id` and `source_id`.
- `layers/fit.py`: Layer 5 fit, per-gap scoring and idempotent skip of already-scored links.
- `pipeline.py`: stage sequencer, run persistence, structured events.
- `layers/llm_client.py`: LLM provider abstraction (`LLMClient`, `LLMProvider`, `make_llm_client()`). Supports `ollama` (default), `claude` (Anthropic SDK), and `openai`. Selected via `ORCH_LLM_PROVIDER`.
- `adapters/browser_client.py`: Browser/HTTP provider abstraction (`BrowserClient`, `BrowserProvider`, `PageResult`, `make_browser_client()`). Supports `playwright_cdp` (default), `http` (urllib fallback), `claude_cu` (stub for future Anthropic Computer Use API). Selected via `ORCH_BROWSER_PROVIDER`.

## API Endpoints
- `GET /api/orchestrator/health`
- `GET /api/orchestrator/manuscripts`
- `POST /api/orchestrator/manuscripts/upload`
- `POST /api/orchestrator/runs`
- `GET /api/orchestrator/runs`
- `GET /api/orchestrator/runs/{run_id}`
- `GET /api/orchestrator/runs/{run_id}/events`
- `GET /api/orchestrator/runs/{run_id}/stream` (SSE — live event stream via `EventSource`)
- `GET /api/orchestrator/runs/{run_id}/documents`
- `GET /api/orchestrator/runs/{run_id}/evidence/{evidence_id}`
- `GET /api/orchestrator/evidence/{evidence_id}`
- `GET /api/orchestrator/files?path=...`
- `POST /api/orchestrator/runs/{run_id}/retry`
- `GET /api/orchestrator/connections/values`
- `POST /api/orchestrator/connections/save`
- `GET /api/orchestrator/library/profiles`
- `GET /api/orchestrator/sources/catalog`
- `POST /api/orchestrator/signin/preflight`
- `POST /api/orchestrator/signin/test`
- `POST /api/orchestrator/signin/open`

## Removed MVP concepts
- `Intent` endpoints and intent state are removed.
- Manual strategy-preview endpoint is removed.
- Manual gap-layout endpoint is removed.
- Frontend tabs/wizard flow is replaced by a two-page single app surface (`Run`, `Settings`).

## Frontend behavior

### React app (primary — `frontend/`)
The primary frontend is a React 18 + Vite + TypeScript app served from `frontend/dist/` (built with `npm run build`). The legacy `static/index.html` is retained as a fallback only.

Stack: React 18, Vite, TypeScript, Tailwind v3, Tanstack Query v5, Zustand, Framer Motion, Lucide React.

Design aesthetic: warm off-white background, amber accent, clean serif/sans typography, card-based layout. Dark mode toggleable and persisted to `localStorage`.

Key components:
- `ManuscriptSelector` — pick or upload a manuscript from the workspace
- `RunLauncher` — pre-run sign-in gate → `Run Research` button
- `PipelineRail` — horizontal stage pills with pulsing dot for the active stage
- `PlanPanel` — per-gap plan cards (claim kind, evidence need, confidence, ladder/synonym-ring context)
- `EvidencePanel` — slide-in drawer (Framer Motion) with full source packet detail, quality-ranked document rows, excerpt previews, and anchor jump links
- `ConfidenceBar` — green ≥75%, amber 50–74%, red <50%
- `EventLog` — live log driven by SSE stream (`/api/orchestrator/runs/{id}/stream`) with auto-reconnect
- `SettingsPage` — library profile, database discovery, credential save

### Legacy static page (`static/index.html`)
Still served at `/` when `frontend/dist/` is not built. Retains all existing behavior:
  - manuscript select/upload
  - explicit pre-run sign-in stage (manuscript-aware analysis preflight + platform checklist + login test + user confirmation gate) before `Run Research`
  - single `Run Research` button
  - top-level `Interface Style` selector (`editorial`, `operations`, `atlas`) with local preference persistence
  - plan panel appears once `research_plan` is available
  - live stage tracker and event log polling every 3s
  - active-stage pulse + heartbeat indicator while run is in progress
  - run launch is blocked until pre-run sign-in stage is marked complete
  - `Analyze Sources` runs analysis/reflection preflight for the selected manuscript and derives sign-in targets from planned providers
  - `Test Login` probes each derived provider URL and reports per-source `ok` / `blocked` / `unreachable` status with action hints
  - `Test Login` now opens a blocking sign-in splash prompt first so users explicitly sign into university/provider systems before checks continue
  - sign-in splash `Open Sign-In Pages` now opens tabs through the attached CDP browser session used by Playwright login tests/pulls (with local-tab fallback if CDP is unavailable)
  - status colors are semantic and consistent across workflow UI: green (`ready`), red (`blocked`), black (`completed`)
  - Settings `Detected Library Databases` rows include per-database `Test Login` actions with row-level pass/fail badges
  - auto-expanded log while active with live event count/stage header
  - post-run document list with click-through links to pulled artifact files
  - pulled documents shown as collapsible source packets; packet JSON is parsed for linked document targets so users see source docs (PDF/web/DOI) first
  - linked document rows include stable evidence references (`evidence_id`), excerpt previews, quote hashes, and best-effort snippet jump links (`anchor_url`) when a URL + excerpt are available
  - evidence lookup endpoints resolve stable references back to source packet/document metadata for manuscript-to-source traceability
  - linked documents are quality-ranked (`high`, `medium`, `seed`) so direct/local PDFs and strong document links appear above provider-search seed links
  - plan cards show route details (`claim_kind`, `evidence_need`, confidence, review status) plus ladder/synonym-ring context when available
  - Settings page supports library-profile selection, database discovery, and credential save-to-`.env`

### Run export bundle (historian-friendly)
Run completion exports a manuscript bundle under `ORCH_DATA_ROOT/manuscript_exports/<manuscript title>/`.

New structure (v3):
- copied manuscript file
- `_INDEX.md` — master table of all gaps with chapter, claim, sources, quality, and synthesis
- `_BIBLIOGRAPHY.md` — all unique URLs and document references collected across the run
- `by_chapter/<chapter-slug>.md` — per-chapter gap summaries for chapter-by-chapter review
- `gaps/<ch{N}--<claim-slug>/` — one folder per gap, named for chapter + claim for immediate readability
  - `_README.md` — prose summary: claim, excerpt, source table, synthesis, next steps
  - `_SOURCES.md` — URL list
  - `related_urls.txt` — URLs extracted from JSON artifacts
  - `documents/<source_id>/` — copied pull artifacts (packet JSON, PDFs, fetched HTML/PDF)
  - `documents/<source_id>/_fetched_urls/` — best-effort fetched artifacts from seed URLs (HTML/PDF only)
- `gap_report_<run_id>.md` — legacy flat report (backwards compat)
- `bundle_manifest_<run_id>.json` — machine-readable manifest (backwards compat)

Gap folder slug format: `ch{chapter-number}--{claim-slug}` (e.g. `ch2--flsa-wage-claims`). Chapter prefix is derived by extracting the leading ordinal word from the chapter heading.

## Configuration
Environment controls all behavior (`config.py`):
- **LLM provider**: `ORCH_LLM_PROVIDER` — `ollama` (default) | `claude` | `openai`. Selects which `LLMClient` backend all layers use.
- **Browser provider**: `ORCH_BROWSER_PROVIDER` — `playwright_cdp` (default) | `http` | `claude_cu` (stub). Selects which `BrowserClient` backend seed-URL fetch and sign-in probing use.
- analysis: `ORCH_GAP_ANALYSIS_*`
- reflection: `ORCH_REFLECTION_*`
- search policy cache: `ORCH_REFLECTION_*` + `search_policy_cache` directory under `ORCH_DATA_ROOT`
- routing/review gates: `ORCH_ROUTING_MIN_CONFIDENCE`, `ORCH_PLAN_REVIEW_USE_OLLAMA`, `ORCH_PLAN_REVIEW_MODEL`, `ORCH_PLAN_REVIEW_TIMEOUT_SECONDS`
- pull/router: `ORCH_PULL_TIMEOUT_SECONDS`, `ORCH_PULL_OUTPUT_ROOT`, `ORCH_PLAYWRIGHT_CDP_URL`, `ORCH_PULL_MAX_QUERY_ATTEMPTS`, `ORCH_PULL_SYNONYM_CAP`, `ORCH_PULL_NOISE_THRESHOLD*`
- pull acceptance floor: `ORCH_PULL_MIN_ACCEPT_DOCS` (minimum per-query hits before accordion stops widening; default `2`)
- pull early-stop floor: `ORCH_PULL_EARLY_ACCEPT_DOCS` (if primary query returns >= N docs, skip synonym traversal; default `0` = disabled)
- library profile routing: `ORCH_LIBRARY_SYSTEM`, `ORCH_LIBRARY_PROFILES_PATH`, `ORCH_PLAYWRIGHT_EXTRA_SOURCES`
- ingest/fit: `ORCH_AUTO_INGEST`, `ORCH_AUTO_LLM_FIT`, `ORCH_LLM_*`, `ORCH_OLLAMA_BASE_URL`
- keyed credential aliases: `BLS_REGISTRATION_KEY` can substitute for `BLS_API_KEY`; EBSCO profile credentials (`EBSCO_PROF` + `EBSCO_PWD`, or `EBSCO_PROFILE_ID` + `EBSCO_PROFILE_PASSWORD`) can satisfy `ebsco_api` availability.

## Notes on adapters
`SOURCE_REGISTRY` in `layers/pull.py` is the extension point.
- Add source: one adapter class + one registry entry.
- Source-specific query translation logic can be implemented per adapter ticket without changing pipeline contracts.
- Source semantics are declared in `SOURCE_CAPABILITIES`; add/update capability tags so routing can match claim type to source family.
- Pull execution includes accordion traversal using rung/synonym state (`lateral`, `widen`, `tighten`, `accept`, `exhausted`) with bounded attempts, per-source noise thresholds, minimum-hit acceptance floor, and a final entity-only retry before marking needs-review.
- Capability ranking now applies light provider diversity constraints so one source family does not dominate all selected routes when equally strong alternatives are available.
- Route confidence includes a seed-source penalty for discovery-only adapters (for example `ebsco_api`) so plan confidence reflects retrieval uncertainty when full-text sources are unavailable.
- Provider click-through URLs generated by EBSCO, JSTOR, ProQuest, Gale, and Americas Historical Newspapers adapters now include era date-range facet parameters (`DT1`/`DT2`, `sd`/`ed`, `daterange`, etc.) when the accordion model has extracted `era_start`/`era_end` from the claim. BLS time-series calls use the same era bounds as `startyear`/`endyear` instead of hardcoded values.
- Seed adapters for EBSCO/Playwright now emit normalized click-through links (`url` and best-effort local `path`) so packet extraction can render document links even before full site-specific automation is complete.
- Seed adapters now also perform best-effort URL resolution: provider-search links are fetched into local `_resolved_urls/<query>/` artifacts (HTML/PDF when available), and those pulled files are emitted as medium/high-quality document rows alongside seed links.
- Seed URL resolution now detects blocked pages (CAPTCHA/challenge/login/access-denied), tags those rows with `blocked_reason` + `action_required`, and emits `pulling/warn` events so users know to complete provider verification/login before retry.
- Blocked snapshots are demoted to seed quality and excluded from `pulled_docs` counts used for pull-status quality accounting.
- Pull source selection prefers keyed/API sources over same-family Playwright fallbacks when both are available (for example prefer `ebsco_api` over `ebscohost`).
- CDP seed fetch now attempts a storage-state-backed request-context pull before opening a transient browser page, reducing focus-stealing tab activity during automated checks.
- Docker CDP attach now normalizes `host.docker.internal` to a resolved IP before probe/connect because Chrome DevTools can reject hostname Host headers with HTTP 500.
- Document indexing preserves adapter-provided quality metadata (`quality_rank`, `quality_label`) and sorts flattened run-document rows by quality so high-confidence links remain first even when mixed with raw artifact files.
- Results packet indexing is JSON-first: nested `_resolved_urls`/`_fetched_urls` artifacts are surfaced through packet-linked rows (not as duplicate standalone packets), and flattened rows dedupe by stable evidence/locator keys.

## University profile coverage
- Playwright adapter IDs currently implemented for history/library workflows:
  - `jstor`
  - `project_muse`
  - `ebscohost`
  - `proquest_historical_newspapers`
  - `americas_historical_newspapers`
  - `gale_primary_sources`
- Default profile file: `library_profiles.default.json` (contains `jhu`, `harvard`, `yale`, `stanford`, `nypl`, and `generic` examples).
- `GET /api/orchestrator/sources/catalog` returns active-profile `university_databases` rows (`name`, `source_id`, `url`, `categories`, `claim_kinds`, `evidence_needs`) plus `library_system` metadata.
- Runtime routing uses profile metadata to constrain Playwright availability by active university system, while API sources remain global/config-driven.

## Post-run document fetch CLI (`scripts/fetch_documents.py`)

The CLI auto-launches Chrome with CDP when it is not already reachable, using a
dedicated `~/.research_henchman_chrome` profile so it does not disturb the user's
normal Chrome tabs or profile.  Library logins are persisted in the profile across
runs — sign in once and the session stays live on subsequent fetches.  Pass
`--no-launch` to opt out of auto-launch and get the original "print help and wait"
behavior (useful when Chrome is managed externally or in scripted CI environments).

## Local run
```bash
uvicorn main:app --reload --port 8876
```

## Docker runtime config
- `docker-compose.yml` loads project-root `.env` through `env_file`.
- Container runtime keeps `ORCH_WORKSPACE=/workspace` and mounts repository root at `/workspace`.
- Set `ORCH_PLAYWRIGHT_CDP_URL` in `.env` when needed; compose falls back to `http://host.docker.internal:9222` and runtime normalizes this hostname for Chrome CDP compatibility in Docker.
- Docker image now includes the Python Playwright client so CDP-backed seed URL fetch fallback can execute in containerized runs.

## Tests
```bash
python3 -m pytest tests -q
```
