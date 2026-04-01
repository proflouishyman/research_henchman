# Interactive Research Orchestrator Design

> Legacy MVP design reference. Current implementation guide is `app/docs/orchestrator_app.md` (v2 contract pipeline).

## Status
Proposed architecture for unifying puller -> ingestor -> LLM-fit -> frontend into one interactive app.

## Objectives
- Build one orchestrated app that runs the existing workflow end-to-end.
- Let users start from either:
  - a manuscript file, or
  - a structured search plan.
- Support puller execution via API or Playwright (user-selectable).
- Automatically chain stages after pull:
  - pull complete -> ingest run -> LLM fit enrichment.
- Make model/provider choices pluggable, while defaulting to current working setup.
- Drive all runtime choices from `.env` variables.
- Keep strict contracts between stages so components are swappable.

## Non-Goals
- Rewriting existing ingestion logic or evidence contracts.
- Replacing Evidence Hub data model.
- Forcing a single LLM vendor.

## Current Baseline (Already Working)
- Storage/API/UI: `codex/evidence_hub/app.py` + JSON store in `codex/evidence_hub/data`.
- Ingestion orchestration: `codex/evidence_hub/ingest_all_materials.py` and incremental run ingesters.
- LLM-fit worker (Ollama): `codex/evidence_hub/generate_llm_fit_evidence.py`.
- Default LLM settings in current system:
  - model: `qwen2.5:7b`
  - base URL: `http://127.0.0.1:11434`
  - ctx: `1024`
  - source char cap: `1600`

## Target System

### High-Level Components
1. Frontend Workbench
- Intake wizard for manuscript/search plan.
- Connection setup UI for APIs and Playwright login/CDP.
- Pipeline run view (job status, logs, outputs by stage).
- LLM backend/model selector (default preselected).

2. Orchestrator API (new control plane)
- Owns job state machine.
- Validates inputs and credentials.
- Selects pull adapter (`api` or `playwright`).
- Triggers ingest automatically on pull success.
- Triggers LLM fit automatically on ingest success.

3. Pull Adapters
- `ApiPullAdapter`: provider API pulls.
- `PlaywrightPullAdapter`: browser-driven pulls and downloads.
- Both emit a shared `PullRunArtifact` contract for ingestion.

4. Ingest Runner
- Wraps existing ingesters with standardized invocation.
- Consumes pull artifacts by `run_id`.
- Writes only through current evidence hub contracts.

5. LLM Fit Runner
- Uses a provider abstraction (`LlmFitAdapter`).
- Defaults to current Ollama worker path and defaults.
- Allows slot-in/out of alternate model backends.

6. Evidence Hub API + Data Store
- Remains source of truth for `searches`, `results`, `documents`, `fit_links`, `ingest_runs`, `tasks`.

### Storage Decision (Clarification)
- Yes: the current system is NoSQL.
- Default persistence remains the existing JSON document store (`codex/evidence_hub/data/*.json`) with collection-level locks.
- Orchestrator must treat this NoSQL layer as canonical in v1, not bypass it.
- Any future database backend (for scale/concurrency) must be introduced behind a repository adapter and preserve existing collection contracts.

## Frontend Design Revision (Using Frontend-Skill)

### Visual Thesis
One calm research operations surface: editorial precision + lab-console clarity, with manuscript context always visible and one strong action color for pipeline control.

### Content Plan (App Surface)
1. Workspace
- Central pipeline timeline and run controls.
2. Evidence Operations
- Pull setup, credential status, and ingest/LLM stage health.
3. Document Intelligence
- Gap coverage, provenance, and fit quality inspection.
4. Final Action
- Exportable evidence packet / manuscript-ready citation bundle.

### Interaction Thesis
1. Stage progression motion
- Pipeline stages animate left-to-right as states change (`queued -> pulling -> ingesting -> llm_processing`).
2. Context-preserving panel transitions
- Manuscript/search-plan context remains pinned while detail panes slide/expand.
3. Provenance reveal
- Hover/focus on a linked document reveals source lineage (`search -> result -> file -> fit`) with minimal latency.

### UX Principles
- App-first, not marketing-first: utility copy and operational clarity over hero-style messaging.
- Cardless-by-default layout: use panes, split columns, and list/table surfaces before introducing cards.
- One accent color for action/state emphasis, with neutral base surfaces.
- Two typefaces max:
  - primary UI sans for controls and tables
  - secondary serif or mono accent for manuscript/context cues.
- Keep chrome minimal; emphasize scanability of stages, status, and evidence quality.

### Visual System Specification

#### Design Tokens (CSS Variables)
```css
:root {
  /* Typography */
  --font-ui: "Suisse Int'l", "Space Grotesk", "Avenir Next", sans-serif;
  --font-technical: "IBM Plex Mono", "SF Mono", "Menlo", monospace;

  /* Type scale */
  --text-2xs: 11px;
  --text-xs: 12px;
  --text-sm: 13px;
  --text-md: 15px;
  --text-lg: 18px;
  --text-xl: 24px;
  --text-2xl: 32px;

  /* Spacing scale */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 24px;
  --space-6: 32px;
  --space-7: 48px;

  /* Radius */
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;

  /* Surface + text */
  --bg-canvas: #0a0d12;
  --bg-pane: #10161f;
  --bg-elevated: #151d29;
  --line-subtle: #213042;
  --line-strong: #314862;
  --text-primary: #e7eef6;
  --text-muted: #9db0c3;

  /* Accent (single primary accent) */
  --accent: #52c7ff;
  --accent-strong: #2baeea;
  --accent-soft: rgba(82, 199, 255, 0.18);

  /* Semantic status */
  --ok: #4bd18b;
  --warn: #ffbf5e;
  --error: #ff6f7d;
  --info: #7cb7ff;

  /* Focus */
  --focus-ring: 0 0 0 3px rgba(82, 199, 255, 0.35);
}
```

#### Typography Rules
- Product name/title uses `--text-xl` or `--text-2xl`, semibold.
- Operational section headers use `--text-xs` uppercase with letter spacing.
- Table/body content defaults to `--text-sm`.
- Logs, IDs, paths, and model signatures use `--font-technical`.

#### Spacing and Density
- Use `--space-4` as default internal pane padding.
- Vertical rhythm for operational lists/tables: `--space-2` to `--space-3`.
- Avoid dense crowding in primary timeline; keep minimum row height 36px.
- Support two density modes:
  - `comfortable` (default)
  - `compact` for heavy log inspection.

#### Borders and Elevation
- Primary pane separation via 1px border (`--line-subtle`), not heavy shadows.
- Elevated overlays (drawers/modals) use `--bg-elevated` + one soft shadow only.
- Keep shadow use minimal; rely on contrast and spacing first.

#### Data Visualization Palette
- Coverage/quality visuals use semantic colors only:
  - `covered` -> `--ok`
  - `weak` -> `--warn`
  - `uncovered` -> `--error`
- Avoid rainbow palettes; use tints of neutral + semantic overlays.

#### Responsive Breakpoints
- `sm`: 0-767px (stacked panes, drawer inspector)
- `md`: 768-1199px (collapsed nav rail, two-pane)
- `lg`: 1200px+ (full three-pane workspace)

#### Core Component Primitives
1. Nav Rail
- Fixed width (72px icon mode / 220px expanded mode).
- Clear active indicator with accent bar, not filled pills.

2. Stage Timeline
- Horizontal rail with discrete stage nodes and connecting progress line.
- Node states: idle, active, success, failed, blocked.

3. Run Log Stream
- Monospace rows grouped by stage.
- Sticky stage filters; color only for status severity.

4. Evidence Table
- Sticky header, row hover highlight, keyboard row selection.
- Inline provenance preview in inspector, not modal-first.

5. Inspector Panel
- 320-380px width desktop.
- Tabs: `Summary`, `Provenance`, `LLM Fit`, `Artifacts`.

#### Motion Tokens
```css
:root {
  --ease-standard: cubic-bezier(0.2, 0.8, 0.2, 1);
  --ease-emphasis: cubic-bezier(0.22, 1, 0.36, 1);
  --dur-fast: 140ms;
  --dur-mid: 220ms;
  --dur-slow: 320ms;
}
```

- Use `--dur-mid` for panel transitions.
- Use `--dur-fast` for hover/focus affordances.
- Use `--dur-slow` only for stage progression transitions.
- Respect reduced motion: replace transforms with opacity-only transitions.

#### Accessibility Tokens
- Minimum contrast target: WCAG AA for all text and controls.
- Minimum target size: 36px interactive height.
- Distinguish status by icon/label + color (never color-only meaning).

### Layout System
Three-pane desktop layout:
1. Left Nav Rail
- Intake, Runs, Pull Config, Ingestion, LLM Fit, Evidence, Settings.
2. Primary Workspace
- Active run timeline, logs, retry controls, and stage metrics.
3. Right Inspector
- Selected item details (gap, doc provenance, quote confidence, source path).

Mobile layout:
- Same primitives in stacked sequence.
- Inspector becomes bottom sheet/drawer.
- Stage timeline becomes horizontal scroll strip with sticky active stage.

### Key Screens
1. Intake Screen
- Accept manuscript upload/path and/or search plan upload/path.
- Show parsed summary and derived `ResearchIntent`.
- Allow quick scope constraints (`gap_id`, max queries, provider scope).

2. Connections Screen
- Provider/mode chooser:
  - pull mode: `api`, `playwright`, `auto`
  - provider: `ebscohost`, `statista`, `custom`
- Credential fields generated from adapter contract.
- Save-to-`.env` action with secret masking and verification badge.

3. Run Builder Screen
- Query plan table (editable before execution).
- Duplicate-search precheck preview (`fingerprint` conflicts).
- Single primary CTA: `Start Orchestrated Run`.

4. Pipeline Run Screen (Primary Surface)
- Stage timeline with live status and durations.
- Structured logs grouped by stage.
- Pause/retry/resume from failed stage.
- Run artifact links (`run_dir`, manifests, ingest summary, LLM metrics).

5. Evidence Inspection Screen
- Gap table with quality states (`uncovered`, `weak`, `covered`).
- Document list for selected gap with provenance and LLM/heuristic quote status.
- Fast filters: chapter, provider, quality state, model used.

### Motion and Feedback Rules
- Motion duration targets:
  - micro transitions: 120-180ms
  - panel transitions: 180-240ms
  - stage state shifts: 220-320ms
- Avoid ornamental effects; every animation must improve orientation.
- Status colors reserved for meaning:
  - success, warning, error, in-progress.
- Use skeleton loading for log/table regions; never blank flashes.

### Copy Rules for UI
- Headings must describe operation or state.
- Avoid aspirational language.
- Every helper line should answer one of:
  - what this affects,
  - what ran,
  - what failed,
  - what to do next.

### Accessibility and Readability
- WCAG AA contrast minimum across status colors and text.
- Keyboard-first workflow for run start/retry and table inspection.
- Persist reduced-motion mode via user setting.
- Preserve readable widths for logs and provenance text.

### Frontend Contract Additions
Orchestrator UI expects these new endpoints/events in addition to existing Evidence Hub read APIs:
- `POST /api/orchestrator/intents`
- `POST /api/orchestrator/runs`
- `GET /api/orchestrator/runs/{run_id}`
- `POST /api/orchestrator/runs/{run_id}/retry`
- `GET /api/orchestrator/runs/{run_id}/events`
- `GET /api/orchestrator/connections/schema`
- `POST /api/orchestrator/connections/save`

Event payload shape (stream/poll):
```json
{
  "run_id": "run_...",
  "stage": "planning|pulling|ingesting|llm_processing",
  "status": "started|progress|completed|failed",
  "message": "human-readable summary",
  "meta": {
    "provider": "ebscohost",
    "pull_mode": "api"
  },
  "ts_utc": "2026-03-26T20:10:00Z"
}
```

## Pipeline Flow
1. User submits manuscript and/or search plan.
2. Orchestrator normalizes into a `ResearchIntent`.
3. Orchestrator generates/loads query tasks.
4. Pull stage executes via selected adapter.
5. On successful pull run, orchestrator auto-invokes ingestion.
6. On successful ingestion, orchestrator auto-invokes LLM-fit generation.
7. Frontend refreshes from Evidence Hub APIs.

## Input Contracts

### `ResearchIntent` (normalized intake)
```json
{
  "intent_id": "intent_...",
  "input_mode": "manuscript|search_plan|both",
  "manuscript": {
    "path": "Manuscript/Add To Cart -- main manuscript -- 2026.docx",
    "format": "docx"
  },
  "search_plan": {
    "path": "codex/evidence_hub/data/pull_backlog_by_gap.csv",
    "format": "csv"
  },
  "constraints": {
    "gap_ids": ["C2-G4"],
    "max_queries": 100
  }
}
```

### `SearchPlan` minimum schema
- `gap_id`
- `query_row1` or `query_text`
- optional: `query_row2`, `provider`, `notes`, `priority`

### Manuscript Intake Behavior
- Accept `.docx`, `.md`, `.txt` first; optional `.pdf`.
- Extract candidate claims/keywords.
- Map candidate claims to existing `gap_id` where possible.
- Produce draft pull tasks that user can edit before execution.

## Stage Contracts

### Pull Adapter Contract
Both pull adapters must return:
```json
{
  "run_id": "ebsco_api_run_20260326_190500",
  "provider": "ebscohost",
  "run_dir": "codex/add_to_cart_audit/external_sources/ebsco_api_run_20260326_190500",
  "artifact_type": "ebsco_manifest_pair|external_packet",
  "artifacts": [
    "ebsco_search_results.csv",
    "ebsco_document_manifest.csv"
  ],
  "stats": {
    "queries_executed": 12,
    "results_rows": 241
  },
  "status": "completed|partial|failed"
}
```

### Ingest Contract
- Input: `PullRunArtifact`
- Behavior:
  - if `artifact_type=ebsco_manifest_pair`, run incremental EBSCO ingester by `run_id`
  - if `artifact_type=external_packet`, run external packet ingester by `run_id`
- Output:
```json
{
  "run_id": "ebsco_api_run_20260326_190500",
  "ingested": true,
  "documents_upserted": 89,
  "results_upserted": 241,
  "fit_links_upserted": 44
}
```

### LLM Fit Contract
- Input: ingestion output + scope (`gap_id`/all).
- Output persisted to `fit_links.llm_*` fields using existing contract consumed by `GET /api/gaps/{gap_id}`.

## Abstractions

### `PullAdapter` interface
- `validate_credentials() -> ValidationResult`
- `plan_queries(intent: ResearchIntent) -> QueryBatch`
- `run(query_batch: QueryBatch) -> PullRunArtifact`

### `IngestAdapter` interface
- `ingest(run_artifact: PullRunArtifact) -> IngestResult`

### `LlmFitAdapter` interface
- `validate_model() -> ValidationResult`
- `enrich(scope: LlmScope) -> LlmRunResult`

### `CredentialProvider` interface
- `required_fields(adapter_type, provider) -> [EnvFieldSpec]`
- `write_env(updates) -> WriteResult`
- `mask_for_logs(value) -> string`

## Environment-Driven Configuration
Every runtime choice must be represented as `.env`.

### Orchestrator
- `ORCH_ENABLED=true`
- `ORCH_WORKSPACE=/abs/workspace/path`
- `ORCH_AUTO_INGEST=true`
- `ORCH_AUTO_LLM_FIT=true`
- `ORCH_FAIL_FAST=false`

### Intake
- `ORCH_INTENT_MODE=manuscript|search_plan|both`
- `ORCH_MANUSCRIPT_PATH=...`
- `ORCH_SEARCH_PLAN_PATH=...`
- `ORCH_DEFAULT_MAX_QUERIES=100`

### Puller Routing
- `ORCH_PULL_PROVIDER=ebscohost|statista|custom`
- `ORCH_PULL_MODE=api|playwright|auto`
- `ORCH_PLAYWRIGHT_CDP_URL=http://127.0.0.1:9222`
- `ORCH_PULL_OUTPUT_ROOT=codex/add_to_cart_audit/external_sources`

### Puller Credentials (examples)
- `EBSCO_PROFILE_ID=...`
- `EBSCO_PROFILE_PASSWORD=...`
- `EBSCO_API_KEY=...` (if API path needs key)
- `STATISTA_SESSION_HINT=...` (optional, browser-session metadata only)

### Ingestion
- `ORCH_INGEST_EBSCO_SCRIPT=codex/evidence_hub/ingest_ebsco_runs.py`
- `ORCH_INGEST_EXTERNAL_SCRIPT=codex/evidence_hub/ingest_external_run.py`
- `ORCH_INGEST_TIMEOUT_SECONDS=1800`

### LLM Fit Backend Routing
- `ORCH_LLM_BACKEND=ollama|openai_compatible|none`
- `ORCH_LLM_MODEL=qwen2.5:7b`
- `ORCH_LLM_CTX=1024`
- `ORCH_LLM_SOURCE_CHAR_CAP=1600`
- `ORCH_LLM_TIMEOUT_SECONDS=90`
- `ORCH_LLM_TEMPERATURE=0.1`

### Ollama Backend (default working)
- `ORCH_OLLAMA_BASE_URL=http://127.0.0.1:11434`

### Frontend Defaults
- `ORCH_UI_DEFAULT_TAB=pipeline`
- `ORCH_UI_SHOW_ADVANCED=false`

## Credential UX and `.env` Management
- Frontend shows required fields based on selected adapter/provider.
- User submits credentials once in "Connections".
- Orchestrator writes updates to `.env` and reloads runtime config.
- Mask secret values in logs and API responses.
- Enforce file permissions for `.env` (owner read/write).
- Never print raw secrets in job events.

## Job State Machine
- `queued`
- `validating_config`
- `planning`
- `pulling`
- `ingesting`
- `llm_processing`
- `completed`
- `failed`
- `partial_completed`

Each transition emits structured event logs so UI can render progress and retry options.

## Error Handling and Idempotency
- Pull retries use bounded backoff and preserve partial artifacts.
- Ingest is idempotent by existing dedupe contracts (`canonical_signature`, stable link hash).
- LLM stage skips already `ok` links unless `force=true`.
- Failed stage can be resumed from last successful stage.

## Hiccup Safeguards (Required From Pilot Learnings)

### 1) Large-field CSV ingestion failures
- Requirement:
  - All CSV ingestion paths must use the shared large-field-safe loader behavior.
- Guardrail:
  - Contract test with oversized field fixtures must pass before release.

### 2) Non-EBSCO run format ingestion gaps
- Requirement:
  - Ingest router must support `ebsco_manifest_pair` and `external_packet` artifact types.
- Guardrail:
  - Unknown `artifact_type` must fail with structured diagnostics and remediation hint.

### 3) `.env` keys present but not loaded at runtime
- Requirement:
  - Orchestrator startup must load and validate `.env` before task planning.
- Guardrail:
  - Preflight endpoint reports missing required env vars by selected adapter/backend.
  - Runs cannot start when required credentials are absent.

### 4) API quota/rate-limit data loss risk
- Requirement:
  - Pull adapters must preserve last-known-good artifacts when all live pulls fail.
- Guardrail:
  - Never overwrite existing non-empty output with an empty dataset from failed pulls.
  - Emit `partial_completed` with quota diagnostics when applicable.

### 5) Duplicate work across agents/runs
- Requirement:
  - Query registration fingerprint check is mandatory pre-pull.
  - Task claiming must remain atomic.
- Guardrail:
  - Orchestrator rejects run plans containing unresolved duplicate query fingerprints unless `force=true`.

### 6) Weak observability during multi-stage failures
- Requirement:
  - Every stage transition emits structured events with stage, status, message, and metadata.
- Guardrail:
  - UI must show per-stage error context and one-click retry from failed stage.

### 7) Provenance degradation in downstream outputs
- Requirement:
  - Source lineage (`search -> result -> doc -> fit`) must remain queryable for every surfaced quote/rationale.
- Guardrail:
  - LLM-fit output without traceable `doc_id` + source pointer is invalid and must be marked failed/fallback.

### 8) LLM output quality variability
- Requirement:
  - Quote verification against source text stays mandatory for `status=ok`.
- Guardrail:
  - Unverified quote forces `fallback`/`failed` status and does not overwrite stable legacy evidence fields as `ok`.

## Swappable LLM Strategy
- Keep `LlmFitAdapter` backend-agnostic.
- Default adapter: Ollama using current worker semantics.
- Alternate adapters can be added without changing frontend contract, as long as they populate existing `llm_*` fields.
- Model switch is purely config/UI choice, persisted to `.env`.

## Recommended Initial Implementation Phases
1. Orchestrator wrapper phase
- Build control-plane API and job state machine.
- Call existing scripts as subprocesses first.

2. Unified intake phase
- Add manuscript/search-plan intake and query planner UI.

3. Adapter hardening phase
- Add strict contract validators and adapter-specific health checks.

4. Provider expansion phase
- Add more pull providers and optional LLM backends.

## Acceptance Criteria
- User can submit manuscript or search plan from UI.
- User can choose pull mode (`api` or `playwright`) from UI/config.
- User can set credentials in UI and persist to `.env`.
- Pull completion automatically triggers ingest.
- Ingest completion automatically triggers LLM fit.
- User can change LLM backend/model without changing downstream contracts.
- Default run works out-of-the-box with current setup (`ollama + qwen2.5:7b`).
