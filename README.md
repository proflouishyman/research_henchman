# Research Orchestrator (v2)

Contract-enforced automated pipeline for manuscript research runs.

## What changed from MVP
- Intent objects are removed.
- Manual strategy/gap endpoints are removed.
- One run record now owns full state: analysis -> reflection -> pull -> ingest -> fit.
- Frontend is a single launch + live monitor page.
- UI highlights progress with active-stage color pulse and run heartbeat.
- Completed runs expose click-through artifact files in a document panel.

## Core architecture
- `app/contracts.py`: layer dataclasses and enums.
- `app/layers/analysis.py`: Layer 1 (`manuscript_path -> GapMap`).
- `app/layers/reflection.py`: Layer 2 (`GapMap + SourceAvailability -> ResearchPlan`).
- `app/layers/pull.py`: Layer 3 source router + `SOURCE_REGISTRY`.
- `app/layers/ingest.py`: Layer 4 ingest (`GapPullResult -> IngestResult`).
- `app/layers/fit.py`: Layer 5 fit (`IngestResult -> FitResult`).
- `app/pipeline.py`: stage sequencer and structured events.

## API surface
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

## Extension point
Add a new source by:
1. creating one adapter class in `app/adapters/`
2. registering it in `app/layers/pull.py` `SOURCE_REGISTRY`

No pipeline or API rewrite required.

## Run locally
From repository root:

```bash
uvicorn app.main:app --reload --port 8876
```

Open: <http://localhost:8876>

## Tests
```bash
python3 -m pytest app/tests -q
```

## Docker
From `app/` directory:

```bash
docker compose up --build -d
```

Open: <http://localhost:8876>
