# Orchestrator App (MVP)

## Purpose
Provide an interactive control plane that coordinates:
- puller
- ingestor
- LLM fit enrichment

while preserving existing Evidence Hub data contracts.

## Directory
- `app/`

## API Endpoints
- `GET /api/orchestrator/health`
- `GET /api/orchestrator/manuscripts`
- `GET /api/orchestrator/gaps/layout`
- `POST /api/orchestrator/intents`
- `GET /api/orchestrator/intents/{intent_id}`
- `GET /api/orchestrator/connections/schema`
- `GET /api/orchestrator/connections/values`
- `GET /api/orchestrator/sources/catalog`
- `POST /api/orchestrator/connections/save`
- `POST /api/orchestrator/runs`
- `GET /api/orchestrator/runs`
- `GET /api/orchestrator/runs/{run_id}`
- `GET /api/orchestrator/runs/{run_id}/events`
- `POST /api/orchestrator/runs/{run_id}/retry`

## Stage flow
1. Validate config and required env fields.
2. Plan run payload.
3. Pull stage (`api|playwright|auto` adapter route).
4. Ingest stage (artifact-type based routing).
5. LLM fit stage (default Ollama backend).

## Hiccup coverage in MVP
- Runtime `.env` preflight validation before pulling.
- Artifact-type routing supports:
  - `ebsco_manifest_pair`
  - `external_packet`
- Structured stage events and run-state persistence.
- Idempotent downstream ingestion relies on existing Evidence Hub dedupe contracts.
- LLM stage invoked only after successful ingest when auto-chain enabled.

## Frontend surface
- `Plan`: manuscript selector and chapter/gap layout.
- `Strategy`: automatic research or narrow question.
- `Results`: run list + event timeline + retry.
- `Settings`: inspect `.env` values and add/update keys (including API keys).
- `Settings` also lists:
  - free APIs in use
  - closed/keyed APIs in use
  - university database set used by pull workflows

## Run locally
```bash
uvicorn app.main:app --reload --port 8876
```
