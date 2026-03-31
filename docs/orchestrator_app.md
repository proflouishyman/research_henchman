# Orchestrator App (v2)

## Purpose
Provide a contract-enforced research pipeline where the user selects a manuscript and starts one run. The run record owns the full lifecycle:
- manuscript analysis
- LLM plan reflection
- source pulls
- ingest
- LLM fit

## Architecture
- `app/contracts.py`: all layer dataclasses and enums (`GapMap`, `ResearchPlan`, `GapPullResult`, `IngestResult`, `FitResult`, `RunRecord`).
- `app/layers/analysis.py`: Layer 1 analysis (Ollama-first with heuristic fallback, fingerprint cache).
- `app/layers/reflection.py`: Layer 2 reflection + policy gates (claim typing, evidence typing, query-quality gate, local review pass for low-confidence routes).
- `app/layers/pull.py`: Layer 3 router + `SOURCE_REGISTRY` + `SOURCE_CAPABILITIES` semantic routing table.
- `app/layers/ingest.py`: Layer 4 ingest, tags artifacts with `gap_id` and `source_id`.
- `app/layers/fit.py`: Layer 5 fit, per-gap scoring and idempotent skip of already-scored links.
- `app/pipeline.py`: stage sequencer, run persistence, structured events.

## API Endpoints
- `GET /api/orchestrator/health`
- `GET /api/orchestrator/manuscripts`
- `POST /api/orchestrator/manuscripts/upload`
- `POST /api/orchestrator/runs`
- `GET /api/orchestrator/runs`
- `GET /api/orchestrator/runs/{run_id}`
- `GET /api/orchestrator/runs/{run_id}/events`
- `GET /api/orchestrator/runs/{run_id}/documents`
- `GET /api/orchestrator/files?path=...`
- `POST /api/orchestrator/runs/{run_id}/retry`
- `GET /api/orchestrator/connections/values`
- `POST /api/orchestrator/connections/save`
- `GET /api/orchestrator/sources/catalog`

## Removed MVP concepts
- `Intent` endpoints and intent state are removed.
- Manual strategy-preview endpoint is removed.
- Manual gap-layout endpoint is removed.
- Frontend tabs/wizard flow is replaced by one launch+monitor page.

## Frontend behavior
- Single page (`app/static/index.html`):
  - manuscript select/upload
  - single `Run Research` button
  - plan panel appears once `research_plan` is available
  - live stage tracker and event log polling every 3s
  - active-stage pulse + heartbeat indicator while run is in progress
  - auto-expanded log while active with live event count/stage header
  - post-run document list with click-through links to pulled artifact files
  - plan cards show route details (`claim_kind`, `evidence_need`, confidence, review status)

## Configuration
Environment controls all behavior (`app/config.py`):
- analysis: `ORCH_GAP_ANALYSIS_*`
- reflection: `ORCH_REFLECTION_*`
- routing/review gates: `ORCH_ROUTING_MIN_CONFIDENCE`, `ORCH_PLAN_REVIEW_USE_OLLAMA`, `ORCH_PLAN_REVIEW_MODEL`, `ORCH_PLAN_REVIEW_TIMEOUT_SECONDS`
- pull/router: `ORCH_PULL_TIMEOUT_SECONDS`, `ORCH_PULL_OUTPUT_ROOT`, `ORCH_PLAYWRIGHT_CDP_URL`
- ingest/fit: `ORCH_AUTO_INGEST`, `ORCH_AUTO_LLM_FIT`, `ORCH_LLM_*`, `ORCH_OLLAMA_BASE_URL`

## Notes on adapters
`SOURCE_REGISTRY` in `app/layers/pull.py` is the extension point.
- Add source: one adapter class + one registry entry.
- Source-specific query translation logic can be implemented per adapter ticket without changing pipeline contracts.
- Source semantics are declared in `SOURCE_CAPABILITIES`; add/update capability tags so routing can match claim type to source family.

## Local run
```bash
uvicorn app.main:app --reload --port 8876
```

## Tests
```bash
python3 -m pytest app/tests -q
```
