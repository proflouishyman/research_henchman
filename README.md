# Research Orchestrator (v2)

Contract-enforced automated pipeline for manuscript research runs.

## What changed from MVP
- Intent objects are removed.
- Manual strategy/gap endpoints are removed.
- One run record now owns full state: analysis -> reflection -> pull -> ingest -> fit.
- Frontend is a single launch + live monitor page.
- Frontend now includes a switchable `Interface Style` control (`editorial`, `operations`, `atlas`) for live UI comparison without changing functionality.
- Run workflow now includes an explicit pre-run sign-in stage that loads required platform logins from active library/profile availability and blocks run start until user confirmation.
- Pre-run sign-in stage now includes `Test Login`, which probes provider URLs using your active browser session and reports per-source access status before launch.
- Sign-in checklist generation is now manuscript-aware: `Analyze Sources` runs analysis+planning first, then derives login targets from planned providers.
- Workflow statuses now use explicit semantic colors in Run UI: green = ready, red = blocked, black = completed.
- Settings now provides per-database `Test Login` actions with row-level green/red status badges for quick provider-access checks.
- Clicking `Test Login` now opens a sign-in splash prompt first, so users explicitly sign into university/provider systems before checks run.
- CDP retrieval now uses a background request path first (session-state request context) to reduce browser focus stealing during automated checks.
- UI highlights progress with active-stage color pulse and run heartbeat.
- Completed runs expose click-through artifact files in a document panel.
- Results document panel now renders collapsible source packets and prioritizes extracted linked documents (PDF/HTML/DOI/record URLs) over raw JSON artifact filenames.
- Linked document rows now include stable evidence references (`evidence_id`) plus quote/snippet metadata so manuscript claims can be linked back to specific supporting passages.
- Plan routing is now claim-aware: historical/scholarly gaps are routed away from macro-stat APIs unless they semantically fit.
- Query execution now uses bounded backoff attempts (specific -> broader terms) so failed tight queries can recover without manual reruns.
- EBSCO/Playwright seed adapters now emit clickable provider/local document links so pulled-document panels show actionable links instead of packet-only placeholders.
- Seed/search links now run a best-effort follow-on pull pass that stores resolved local artifacts (`_resolved_urls`) and surfaces them as medium/high evidence rows.
- Run document packet indexing now prefers JSON packet links over nested resolved-file packets to avoid duplicate click-through rows and inflated quality counts.
- Pull runs now emit explicit warning events when retrieved pages are blocked by CAPTCHA/login/challenge and include user-action hints for bypass + retry.
- Source selection now prefers keyed/API sources over same-family Playwright fallbacks (for example `ebsco_api` ahead of `ebscohost` when both are available).

## Core architecture
- `contracts.py`: layer dataclasses and enums.
- `layers/analysis.py`: Layer 1 (`manuscript_path -> GapMap`).
- `layers/reflection.py`: Layer 2 (`GapMap + SourceAvailability -> ResearchPlan`) plus claim/evidence typing and routing quality gates.
- `layers/pull.py`: Layer 3 source router + `SOURCE_REGISTRY` + `SOURCE_CAPABILITIES`.
- `layers/ingest.py`: Layer 4 ingest (`GapPullResult -> IngestResult`).
- `layers/fit.py`: Layer 5 fit (`IngestResult -> FitResult`).
- `pipeline.py`: stage sequencer and structured events.

## Frontend interface variants
- Variant notes: `docs/frontend_interface_variants.md`
- Toggle from the top-right `Interface Style` control in the app.

## API surface
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
- `POST /api/orchestrator/signin/preflight`
- `POST /api/orchestrator/signin/test`

## Extension point
Add a new source by:
1. creating one adapter class in `adapters/`
2. registering it in `layers/pull.py` `SOURCE_REGISTRY`
3. declaring claim/evidence capability tags in `SOURCE_CAPABILITIES`

No pipeline or API rewrite required.

## Library-history routing
Playwright source IDs now include JHU-oriented history databases:
- `jstor`
- `project_muse`
- `ebscohost`
- `proquest_historical_newspapers`
- `americas_historical_newspapers`
- `gale_primary_sources`

For historical narrative gaps, routing prefers these scholarly/archive sources over macro-stat APIs.

## Routing/Review env vars
- `ORCH_ROUTING_MIN_CONFIDENCE` (default `0.67`)
- `ORCH_PLAN_REVIEW_USE_OLLAMA` (default `true`)
- `ORCH_PLAN_REVIEW_MODEL` (default `ORCH_REFLECTION_MODEL`)
- `ORCH_PLAN_REVIEW_TIMEOUT_SECONDS` (default `90`)

## University library profiles (Playwright sources)
- `ORCH_LIBRARY_SYSTEM` selects the active university profile (default `jhu`).
- `ORCH_LIBRARY_PROFILES_PATH` points to profile JSON (default `library_profiles.default.json`).
- `ORCH_PLAYWRIGHT_EXTRA_SOURCES` optionally appends comma-separated source IDs.
- Bundled example systems now include: `jhu`, `harvard`, `yale`, `stanford`, `nypl`, `generic`.

`/api/orchestrator/sources/catalog` now reads `university_databases` from the active profile, including `categories`, `claim_kinds`, and `evidence_needs`. This replaces hardcoded university database lists so other institutions can adapt by editing profile JSON only.

### Prompt Template For Generating A New Library Profile
Copy/paste this to an AI agent and replace placeholders:

```text
You are editing a JSON file at `library_profiles.default.json`.

Task:
Generate one new library profile object under `systems` with key `<PROFILE_KEY>` and name `<LIBRARY_NAME>`.

Requirements:
1. Output valid JSON for only the new profile object (do not include markdown).
2. Use this exact schema:
   {
     "name": "Library Display Name",
     "databases": [
       {
         "source_id": "<SUPPORTED_SOURCE_ID>",
         "name": "Database Display Name",
         "url": "https://...",
         "source_type": "playwright",
         "categories": ["history", "newspapers"],
         "claim_kinds": ["historical_narrative", "company_operations", "biographical", "legal_regulatory", "other"],
         "evidence_needs": ["scholarly_secondary", "primary_source", "news_archive", "legal_text", "mixed"]
       }
     ]
   }
3. Only use currently supported Playwright source IDs:
   - jstor
   - project_muse
   - ebscohost
   - proquest_historical_newspapers
   - americas_historical_newspapers
   - gale_primary_sources
   - statista
4. Include 4-7 realistic databases for the library.
5. Reuse claim_kinds/evidence_needs patterns consistent with existing profiles.
6. Keep all strings lowercase for `source_id` and list values.

Context:
- This profile is used by orchestrator routing and the Settings UI.
- Unknown `source_id` values will not be runnable until adapters exist.
```

## Key alias support
- BLS credentials accept either `BLS_API_KEY` or `BLS_REGISTRATION_KEY`.
- EBSCO API routing accepts either `EBSCO_API_KEY` or profile credential pairs (`EBSCO_PROF` + `EBSCO_PWD`, or `EBSCO_PROFILE_ID` + `EBSCO_PROFILE_PASSWORD`).

## Run locally
From repository root:

```bash
uvicorn main:app --reload --port 8876
```

Open: <http://localhost:8876>

## Tests
```bash
python3 -m pytest tests -q
```

## Docker
From repository root:

```bash
docker compose up --build -d
```

Runtime config is loaded from project-root `.env` via `env_file` plus `ORCH_WORKSPACE=/workspace` inside the container.

Playwright note:
- In Docker, `ORCH_PLAYWRIGHT_CDP_URL=http://host.docker.internal:9222` is supported.
- Runtime now normalizes this hostname to an IP before CDP probe/connect because Chrome can reject hostname Host headers for DevTools endpoints.
- Docker runtime includes the Python Playwright client so CDP-backed seed URL fallback can run inside the container.

Open: <http://localhost:8876>
