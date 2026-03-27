# Interactive Orchestrator App

## Location
- All app implementation lives under `app/` as requested.

## What this MVP does
- Creates orchestrator intents from manuscript/search-plan inputs.
- Accepts manuscript files from either:
  - workspace `Manuscript/` directory, or
  - local computer upload via Plan tab.
- Gap layout is manuscript-aware:
  - Add-to-Cart manuscripts use canonical mapped gaps.
  - Other manuscripts use sidecar maps when present.
  - If missing, the app auto-generates and stores a gap map.
- Plan tab now shows manuscript read diagnostics (status, char count, detected headings) so it is clear when parsing succeeded or fell back.
- Stores orchestrator runs/events in `app/data`.
- Supports pull mode routing (`api`, `playwright`, `auto`) through adapter contracts.
- Automatically runs:
  - pull -> ingest -> llm fit
- Exposes connection schema + `.env` save endpoints.
- Provides tabbed UI:
  - `Plan`: manuscript selector + gap layout
  - `Strategy`: automatic vs narrow research scope
  - `Results`: run timeline and retry
  - `Settings`: view/edit `.env` values, add API keys, and view free/closed APIs + university databases in use

## Project Docs
- `docs/interactive_orchestrator_design.md`
- `docs/orchestrator_app.md`

## Run
From repository root:

```bash
uvicorn app.main:app --reload --port 8876
```

Open:
- http://127.0.0.1:8876

## Docker
From `app/` directory:

```bash
docker compose up --build -d
```

Open:
- http://localhost:8876

Stop:

```bash
docker compose down
```

Notes:
- Compose mounts `../` into `/workspace` so existing `codex/evidence_hub` scripts remain available.
- `ORCH_WORKSPACE` is set to `/workspace` inside container.
- Ollama default points to host: `http://host.docker.internal:11434`.

## Pull adapter behavior
- If `existing_run_id` + `existing_run_dir` are supplied in run request, pull stage uses handoff mode and skips command execution.
- Otherwise it executes mode-specific commands from `.env`:
  - `ORCH_API_PULL_COMMAND`
  - `ORCH_PLAYWRIGHT_PULL_COMMAND`

Commands must print JSON artifact containing:
- `run_id`
- `run_dir`
- Optional: `provider`, `artifact_type`, `status`, `stats`

## Required stage scripts (defaults)
- `codex/evidence_hub/ingest_ebsco_runs.py`
- `codex/evidence_hub/ingest_external_run.py`
- `codex/evidence_hub/generate_llm_fit_evidence.py`

## Core env vars
- `ORCH_WORKSPACE`
- `ORCH_PULL_MODE`
- `ORCH_PULL_PROVIDER`
- `ORCH_AUTO_INGEST`
- `ORCH_AUTO_LLM_FIT`
- `ORCH_API_PULL_COMMAND`
- `ORCH_PLAYWRIGHT_PULL_COMMAND`
- `ORCH_LLM_BACKEND`
- `ORCH_LLM_MODEL`
- `ORCH_OLLAMA_BASE_URL`
# research_henchman
