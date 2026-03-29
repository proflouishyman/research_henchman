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
- Gap analysis prefers Ollama smart-model generation (with heuristic fallback on error).
- Gap Analysis tab shows manuscript read diagnostics (status, char count, detected headings) so it is clear when parsing succeeded or fell back.
- Stores orchestrator runs/events in `app/data`.
- Supports pull mode routing (`api`, `playwright`, `auto`) through adapter contracts.
- Workflow UI blocks run launch when required env keys are missing and points users to Settings.
- Workflow persists operator state in-browser:
  - last selected manuscript
  - custom manuscript path
  - search plan path
  - active tab
- Analyze action now provides explicit progress and reuse messaging (for example, when analysis already exists and cached map is reused).
- Run launch now provides live visual state:
  - start button switches to in-progress state/color
  - run status badge updates by stage
- Workflow is now split into step tabs (one step per page): `1 Manuscript` -> `2 Gap Analysis` -> `3 Strategy`, with intent creation handled automatically when starting a run.
- Backend activity log is now a persistent bottom dock so run progress remains visible from all tabs.
- Activity log now prints a run plan at launch and prefixes stage events with step progress (`N/Total`) so users can track where the run is in the plan.
- Run monitor now includes a heartbeat indicator (pulsing dot + last backend check age) and periodic “still running” log lines during long stages.
- Live log rows now include structured metadata from stage events (for example pull mode/provider, pull command, run directory, and pull stats/fallback notes) so API/pull behavior is visible during execution.
- Strategy tab now includes a `Live Activity` monitor that shows:
  - current stage/status
  - current action message
  - pull/search metadata details when available
- Automatically runs:
  - pull -> ingest -> llm fit
- Prevents duplicate concurrent runs by default:
  - new run requests reuse the currently active run unless `force=true`.
- Includes stale-run watchdog:
  - active runs older than timeout-based cutoff are auto-marked failed so they do not block new runs indefinitely.
- Avoids repeat ingest work for already-ingested artifacts:
  - if `codex/evidence_hub/data/ingest_runs.json` already contains the pulled `run_id`, ingest stage is skipped (unless `force=true`).
- Exposes connection schema + `.env` save endpoints.
- Provides tabbed UI:
  - `1 Manuscript`: manuscript selector/upload and path controls
  - `2 Gap Analysis`: analysis trigger + gap layout output
  - `3 Strategy`: pull mode/provider + run launch + live activity
  - `Results`: collapsible runs and run-events panels
  - `Settings`: view/edit `.env` values, add API keys, and view free/closed APIs + university databases in use
  - `Settings` env table now shows value source (`process_env` vs `.env`) so Docker-injected runtime values are visible.

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

## Tests
From repository root:

```bash
python3 -m pytest app/tests/test_orchestrator_e2e.py -q
```

What this verifies:
- `.docx` manuscript text is read and gap analysis is generated.
- Full orchestrator stage chain runs (`pull -> ingest -> llm_fit`) and emits stage events with metadata.
- Run-creation guard reuses an active run and stale-run watchdog marks orphaned active runs as failed.

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
- If `ORCH_API_PULL_COMMAND` is not set and provider is `ebscohost`, orchestrator uses built-in fallback command (`app/default_api_pull.py`) that returns the newest compatible existing EBSCO run folder.
- For a live upstream API pull (net-new retrieval), set `ORCH_API_PULL_COMMAND` explicitly.

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
- `ORCH_GAP_ANALYSIS_USE_OLLAMA`
- `ORCH_GAP_ANALYSIS_MODEL`
- `ORCH_GAP_ANALYSIS_OLLAMA_BASE_URL`
# research_henchman
