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
- `POST /api/orchestrator/manuscripts/upload`
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
- `Plan`: manuscript selector, local manuscript upload, and chapter/gap layout.
- `Strategy`: automatic research or narrow question.
- `Results`: run list + event timeline + retry.
- `Settings`: inspect `.env` values and add/update keys (including API keys).
- `Settings` also lists:
  - free APIs in use
  - closed/keyed APIs in use
  - university database set used by pull workflows

## Manuscript-aware gap behavior
- Gap layout endpoint accepts selected manuscript path and refresh flag.
- For Add-to-Cart manuscripts, canonical mapped gaps are used.
- For other manuscripts:
  - use manuscript sidecar gap map if present
  - otherwise auto-generate and persist a manuscript-specific gap map.
- Gap analysis generation now prefers Ollama (`ORCH_GAP_ANALYSIS_USE_OLLAMA=true`) with smart-model selection and structured JSON output.
- If Ollama is unavailable or errors, the app falls back to heuristic gap analysis and reports the fallback reason in extraction metadata.
- Generated-map cache keys include manuscript file fingerprint (path + size + mtime) so swapping file contents at the same path triggers fresh analysis.
- Legacy one-row placeholder maps or maps without metadata are auto-regenerated.
- Gap layout response now includes manuscript extraction diagnostics:
  - status
  - char/line counts
  - detected chapter-heading candidates
  - analysis method/model and LLM error fallback state
  - fallback reason when no headings are detected.

## Run locally
```bash
uvicorn app.main:app --reload --port 8876
```
