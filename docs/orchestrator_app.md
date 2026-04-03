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
- `layers/analysis.py`: Layer 1 analysis (Ollama-first with heuristic fallback, fingerprint cache).
- `layers/search_policy.py`: Accordion search-policy layer (claim classification, era synonym ring, ladder generation, move-state logic, hash cache).
- `layers/reflection.py`: Layer 2 reflection + policy gates (claim typing, evidence typing, query-quality gate, ladder persistence on `PlannedGap.query_ladder`, local review pass for low-confidence routes).
- `layers/pull.py`: Layer 3 router + `SOURCE_REGISTRY` + `SOURCE_CAPABILITIES` semantic routing table.
- `layers/ingest.py`: Layer 4 ingest, tags artifacts with `gap_id` and `source_id`.
- `layers/fit.py`: Layer 5 fit, per-gap scoring and idempotent skip of already-scored links.
- `pipeline.py`: stage sequencer, run persistence, structured events.

## API Endpoints
- `GET /api/orchestrator/health`
- `GET /api/orchestrator/manuscripts`
- `POST /api/orchestrator/manuscripts/upload`
- `POST /api/orchestrator/runs`
- `GET /api/orchestrator/runs`
- `GET /api/orchestrator/runs/{run_id}`
- `GET /api/orchestrator/runs/{run_id}/events`
- `GET /api/orchestrator/runs/{run_id}/documents`
- `GET /api/orchestrator/runs/{run_id}/evidence/{evidence_id}`
- `GET /api/orchestrator/evidence/{evidence_id}`
- `GET /api/orchestrator/files?path=...`
- `POST /api/orchestrator/runs/{run_id}/retry`
- `GET /api/orchestrator/connections/values`
- `POST /api/orchestrator/connections/save`
- `GET /api/orchestrator/library/profiles`
- `GET /api/orchestrator/sources/catalog`

## Removed MVP concepts
- `Intent` endpoints and intent state are removed.
- Manual strategy-preview endpoint is removed.
- Manual gap-layout endpoint is removed.
- Frontend tabs/wizard flow is replaced by a two-page single app surface (`Run`, `Settings`).

## Frontend behavior
- Single page (`static/index.html`):
  - manuscript select/upload
  - single `Run Research` button
  - top-level `Interface Style` selector (`editorial`, `operations`, `atlas`) with local preference persistence
  - plan panel appears once `research_plan` is available
  - live stage tracker and event log polling every 3s
  - active-stage pulse + heartbeat indicator while run is in progress
  - auto-expanded log while active with live event count/stage header
  - post-run document list with click-through links to pulled artifact files
  - pulled documents shown as collapsible source packets; packet JSON is parsed for linked document targets so users see source docs (PDF/web/DOI) first
  - linked document rows include stable evidence references (`evidence_id`), excerpt previews, quote hashes, and best-effort snippet jump links (`anchor_url`) when a URL + excerpt are available
  - evidence lookup endpoints resolve stable references back to source packet/document metadata for manuscript-to-source traceability
  - linked documents are quality-ranked (`high`, `medium`, `seed`) so direct/local PDFs and strong document links appear above provider-search seed links
  - plan cards show route details (`claim_kind`, `evidence_need`, confidence, review status) plus ladder/synonym-ring context when available
  - Settings page supports library-profile selection, database discovery, and credential save-to-`.env`
  - run completion exports a manuscript bundle under `ORCH_DATA_ROOT/manuscript_exports/<manuscript title>/` containing:
    - copied manuscript file
    - `gap_report_<run_id>.md` with coded gaps + snippets + quality mix (`high`/`medium`/`seed`) and remediation notes when retrieval is seed-only
    - refreshed `gaps/` artifacts per run (stale gap files from prior runs are cleared before export)
    - `gaps/<gap_id>/related_documents/<source_id>/...` copied pull artifacts
    - `gaps/<gap_id>/related_documents/<source_id>/_fetched_urls/...` best-effort fetched artifacts from seed URLs (HTML/PDF/bin)
    - `gaps/<gap_id>/related_urls.txt` extracted URL references from JSON artifacts (when available)

## Configuration
Environment controls all behavior (`config.py`):
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
- Seed adapters for EBSCO/Playwright now emit normalized click-through links (`url` and best-effort local `path`) so packet extraction can render document links even before full site-specific automation is complete.
- Seed adapters now also perform best-effort URL resolution: provider-search links are fetched into local `_resolved_urls/<query>/` artifacts (HTML/PDF when available), and those pulled files are emitted as medium/high-quality document rows alongside seed links.
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

## Local run
```bash
uvicorn main:app --reload --port 8876
```

## Docker runtime config
- `docker-compose.yml` loads project-root `.env` through `env_file`.
- Container runtime keeps `ORCH_WORKSPACE=/workspace` and mounts repository root at `/workspace`.
- Set `ORCH_PLAYWRIGHT_CDP_URL` in `.env` when needed; compose falls back to `http://host.docker.internal:9222`.

## Tests
```bash
python3 -m pytest tests -q
```
