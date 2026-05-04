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

## Library mode (writing companion)

A second top-level UI mode (`/write/...`) lets the user browse the
already-pulled corpus by gap, with per-gap dossiers rendered live from
SQLite (no markdown intermediate). The sibling `/runs` mode is unchanged.

### API endpoints — `routers/library.py`
- `GET /api/library/index` — chapter-grouped gap list + corpus stats.
  Returns `{chapters: [{slug, title, gap_count, gaps: [GapTreeRow]}], corpus_total_rows, corpus_scored_rows, sources}`.
- `GET /api/library/gaps` — flat gap list with article counts joined.
  Query params: `chapter`, `gap_type` (CSV), `tier`, `status`,
  `detector_pass`, `parent_gap_id` (`<root>` finds top-level rows).
- `GET /api/library/gaps/{gap_id}` — single gap_tree row + counts.
- `GET /api/library/gaps/{gap_id}/dossier` — structured dossier
  rendered by `layers.dossier_render.assemble_dossier`. Shape:
  `{gap, summary: {total_rows, consolidated, tier_counts}, tiers: {"3"|"2"|"1"|"0"|"unscored": [DossierEntry]}}`.
- `GET /api/library/articles/search` (Wave 2) — full-text search via
  SQLite FTS5. Query params: `q` (required), `source_id` (CSV),
  `score_min` (0–3), `gap_id`, `year_from`, `year_to`, `has_pdf`,
  `limit` (≤200), `offset`. Returns `{total, results: [DossierEntry &
  {gap_id, snippet}]}` with `<mark>`-highlighted excerpts. Empty `q`
  yields 400 (no full-table dump). Reserved FTS5 chars in user input
  are stripped before MATCH; tokens are wrapped in phrase quotes so
  user punctuation never reaches the FTS5 parser as operators.
- `GET /api/library/characters` (Wave 2) — main-characters dashboard
  data: company-profile gaps with `top_tier3_titles` (up to 3) and
  `tier_histogram` aliasing `tier_counts`. Sorted by tier-3 count
  desc, then `evidence_target` desc.
- `GET /api/library/manuscript/structure?docx=<path>` (v3) — parsed
  manuscript structure. Returns `{chapters: [{title, slug, sections:
  [{heading, paragraphs: [{para_id, text, is_heading, heading_level,
  footnote_count, bracketed_todos, gap_ids}]}]}]}`. Served from an
  on-disk cache at `data/.manuscript_cache/<filename>.json` keyed by
  file mtime+size; re-parsed only when the docx changes. `docx` param
  defaults to the project manuscript. Parser is in
  `layers/manuscript_parse.py`.
- `GET /api/library/manuscript/paragraph/{para_id}?docx=<path>` (v3) —
  single paragraph detail with `gap_ids` and `gap_rows` (resolved
  gap_tree dicts).
- `POST /api/library/marks` (v3) — upsert a star/read/note mark.
  Body: `{article_id, starred?, read?, note?}`. Returns the resulting
  mark row. Idempotent.
- `GET /api/library/marks?starred=&read=` (v3) — bulk fetch marks with
  optional boolean filters. Returns `{marks: [MarkRow]}`.
- `POST /api/library/articles/resolve_gaps` (v3) — resolve article IDs
  to their primary gap. Body: `{article_ids: [int]}`. Returns
  `{mapping: {article_id_str: [gap_id]}}`. Uses `articles.gap_id`
  (primary ingest gap).

#### Marks DB schema (v3)
```sql
CREATE TABLE user_marks (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id),
    starred    INTEGER NOT NULL DEFAULT 0,
    read       INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```
Helpers in `adapters/article_index.py`: `ensure_marks_schema`,
`set_mark`, `get_marks`, `list_starred`.

#### Manuscript parser cache
`layers/manuscript_parse.py` `parse_manuscript(docx_path)` caches the
flat paragraph list at `data/.manuscript_cache/<filename>.json`. Cache
key = `f"{mtime}:{size}"`. On cache hit the JSON is returned as-is;
on miss (stale or absent) the docx is re-parsed with python-docx.
`paragraph_gap_links(paras, conn)` returns `{para_id: [gap_id]}` via
three heuristics: heading-path substring overlap, 60-char claim-text
prefix match in paragraph body, bracketed-TODO vs Pass-B claim prefix.

### Shared dossier-render layer — `layers/dossier_render.py`
The markdown writer (`scripts/generate_dossiers.py`) and the new API
endpoint share `assemble_dossier(conn, gap_id) -> dict`. The shape
returned is identical for both surfaces; the markdown writer is now a
thin wrapper that consumes the same helpers (`norm_title`,
`dedupe_within_gap`, `pick_primary`, `build_cross_gap_index`,
`absolutize_url`, `render_url_or_pdf`).

### Frontend — `/write` route tree (ship-this-week revision, 2026-05-04)

**Default route:** `/write` → redirect to `/write/manuscript` (was `/write/gaps`).
The writing companion opens manuscript-first since that is the primary daily workflow.

- `/write/manuscript` (v3, now default) → manuscript reader. Three-pane layout:
  - Left (w-52): `<ManuscriptOutline>` — chapter navigator.
  - Center: `<ChapterScroll>` + `<ParagraphRow>` — paragraph render with
    left-gutter chips (⚓N footnotes; ⚠ cite?; gap badges; TODO chip).
    Default chapter selection skips preamble/title-only chapters: first
    chapter whose non-heading paragraphs exceed 200 words total is selected.
  - Right (fixed, w-96): `<DossierSidePanel>` — all tier sections now use
    `compact=true` (panel is 384px wide). When opened via paragraph click,
    the panel shows the paragraph text at the top under "Citing this passage:"
    label (italicised) so the user confirms the source list applies to the
    passage they're working on. Multi-gap paragraphs get a tab strip. ESC
    closes; URL updates to `/write/manuscript/:chapterSlug/:paraId`.
  Keyboard shortcuts: `j`/`k` next/prev paragraph; URL is bookmarkable.

- `/write/gaps` → chapter-grouped gap list. Null/empty chapter titles render
  as "Cross-chapter theses" with a tooltip explaining these are intro promises
  whose chapter wasn't auto-paired.

- `/write/gaps/:gapId` → live dossier view:
  - Header: gap metadata + "Pull more sources →" link (→ `/runs/new?gap=<id>`)
    shown when tier-3 count is 0 OR gap is `intro_promise` with < 5 tier-3 entries.
  - **TopPicks strip**: above filter controls, shows the 3 most citation-ready
    tier-3 entries (sorted by has_pdf_path, source priority, pub_date_desc).
    Falls back to tier-2 if no tier-3 exist. Hidden entirely when tier-3 = 0.
  - Filter strip (source toggles · score floor · has-PDF only).
  - Four collapsible tier sections (tier 3/2 default-open, tier 1/0 collapsed).
  Each entry is a `<SourceCard>` with:
    - Per-card score badge reads "score N" (not "tier N"; "tier" is reserved
      for gap-level priority badges in headers and DossierHeader).
    - Drag handle visible at opacity-40 at rest (was opacity-0).
    - Primary "Open" CTA prefers PDF when available; secondary "(or open URL)"
      link shown when both PDF and URL exist.
    - `also_in_sources` moved from inline meta to hover popover only ("Also in: …").
    - Read (Eye) toggle removed from UI; underlying `read` state preserved in DB.
    - Hover popover (300 ms) shows full WHY + abstract + also_in + doi/url.
    - **Drag onboarding**: first-ever hover on the drag handle shows a tooltip
      "Drag to Word — drops Chicago citation" that auto-dismisses after 5 s or
      on × click. Persisted to `localStorage['library.onboarding.drag_seen']`.
    - Cite dropdown copies Chicago / `(Author Year)` / link to clipboard with toast.
    - Cards are draggable via `attachDragCitations` (dataTransfer MIME types).

- `/write/search` → corpus search. On mount, reads `?q=` URL param and seeds
  both the query input and fires the search immediately. Enables linking to
  `/write/search?q=China` from external surfaces.

- `/write/characters` → main-characters dashboard (unchanged from Wave 2).

- `/write/queue` → reading queue. Uses `POST /api/library/articles/resolve_gaps`
  on mount to map starred article IDs → gap IDs server-side. The "visit the
  dossier once to populate" message is removed. Articles with no resolved gap
  (data integrity issue) appear under a "(no gap mapping)" group.

The shared `<SourceCard>` action row: Copy dropdown (Chicago / short / link),
Star. Read toggle removed from UI. In v3, Star writes to `POST /api/library/marks`
(optimistic update in Zustand). On app start, `hydrateMarks()` migrates any
legacy `localStorage['library.marks']` to the DB then clears localStorage.

### UI smoke tests (Playwright)

Local end-to-end smoke tests run against the live uvicorn server on port 8000.
They verify structural correctness — all four dossier tier sections visible,
correct entry counts, expandable cards — and catch regressions before manual
browsing.

**Prerequisites**: uvicorn already running on `http://127.0.0.1:8000`.

**Run all tests:**
```bash
cd frontend
npm run test:e2e
```

**Run a single spec:**
```bash
cd frontend
npm run test:e2e -- tests/e2e/dossier.spec.ts
```

**Test files:**
- `frontend/tests/e2e/dossier.spec.ts` — 8 tests for `/write/gaps/TODO9`:
  gap ID visible, 91-entry summary, all 4 tier headers, count badges (1/1/1/88),
  Tier 0 expand+cards, drag handle opacity > 0, TopPicks strip visible, screenshot.
- `frontend/tests/e2e/routes.spec.ts` — 7 route smoke tests covering all
  `/write/*` surfaces and `/runs`. `/write` redirect now asserts `/write/manuscript`.
- `frontend/tests/e2e/search-and-queue.spec.ts` — 2 tests: `?q=China` returns
  results; starred card visible in queue without visiting dossier.
- `frontend/tests/e2e/manuscript.spec.ts` — 2 tests: default chapter is not
  preamble (>50 paragraphs visible); gap badge click opens side panel with
  "Citing this passage:" label.

Screenshots are gitignored (`frontend/tests/e2e/screenshots/`).
Playwright HTML reports land in `frontend/playwright-report/` (gitignored).

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

## Query normalization workflow (`scripts/normalize_seed_queries.py`)

When a completed run surfaces low-yield gaps — gaps where EBSCO returned few or zero articles — running `normalize_seed_queries.py` before a re-fetch is the primary recovery path. The script reads each seed JSON record's `bquery` field, sends it to an LLM with a detailed EBSCO-syntax system prompt, and writes the result back as `bquery_normalized: List[str]` (N variants, one per vocabulary angle). The original `bquery` is preserved unmodified and also mirrored to `bquery_original` for explicit rollback. The script is idempotent by default — it skips records where `bquery_normalized` is already a non-empty list of the requested length — and `--force` overrides that gate.

To run normalization on all low-yield gaps in a run, then re-fetch:
```bash
python3 scripts/normalize_seed_queries.py \
    --run-id <run_id> \
    --variants 3 \
    --model gpt-oss:20b \
    --force

python3 scripts/fetch_documents.py --run-id <run_id>
```

Use `--gap-id AUTO-NNN-G1` to restrict to a single gap, `--dry-run` to preview without writing, and `--limit N` to cap the number of records processed. The `--model` flag overrides `ORCH_LLM_MODEL` for this invocation only and accepts any model string recognized by the configured LLM provider (`ORCH_LLM_PROVIDER`).

The fetch pipeline consumes `bquery_normalized` in `adapters/document_fetch.py`: `_classify_record` detects the list field and returns one `FetchItem` per variant (each with a distinct spliced EBSCO search URL and a `variant_index`). All variant results land in the same `<gap_id>/<source>/fetched/` directory; article-slug collisions (the same article found by two variants) are silently skipped by the existing file-exists check, keeping the directory clean. Ingest and fit layers see the union of all variant results without requiring any changes.

**Model recommendation** (from 2026-05-01 A/B experiment — see `logs/model_bench_report.md` and `SOLUTIONS.md`):

| Use case | Model | Mean time/call | Notes |
|---|---|---|---|
| One-time gap recovery (best yield) | `gpt-oss:20b` | ~33s | +59% PDFs/seed vs llama3.1:8b; verbose, parses correctly |
| Regular pipeline / speed-sensitive | `qwen2.5:7b` | ~3s | Pipeline default; functional queries, fast |
| Avoid | `llama3.1:8b` | ~4s | Sometimes outputs prompt scaffolding as queries; year-range literals |
| Avoid | `qwen3.5:27b` | 120s+ | All calls timed out at default limit; unusable without raising timeout |

`gpt-oss:20b` is "boring but reliable": verbose, well-structured, enumerates all brand-name spellings, writes year ranges as OR-enumerated lists that EBSCO can parse, and adds adjacent-concept framing. For one-time recovery passes where wall-clock is not urgent this tradeoff is clearly worthwhile. For online or scheduled pipeline runs where latency matters, `qwen2.5:7b` is the right default.

## Local run
```bash
uvicorn main:app --reload --port 8876
```

## Docker runtime config
- `docker-compose.yml` loads project-root `.env` through `env_file`.
- Container runtime keeps `ORCH_WORKSPACE=/workspace` and mounts repository root at `/workspace`.
- Set `ORCH_PLAYWRIGHT_CDP_URL` in `.env` when needed; compose falls back to `http://host.docker.internal:9222` and runtime normalizes this hostname for Chrome CDP compatibility in Docker.
- Docker image now includes the Python Playwright client so CDP-backed seed URL fetch fallback can execute in containerized runs.

## Article Index (SQLite + FTS5)

A searchable, file-based index of all fetched article metadata is maintained in
`data/article_index.sqlite` (gitignored — under `data/`).  It is built from the
`data/pull_outputs/<run_id>/` directories by a standalone module
(`adapters/article_index.py`) and two CLI scripts.  The live run directory is
never modified.

### Build the index

```bash
# Index a completed (or in-progress) run:
python scripts/index_articles.py --run-id run_27f86e44394442

# Re-run is idempotent — only newly-fetched articles are added:
python scripts/index_articles.py --run-id run_27f86e44394442

# Also run DOI-based deduplication after ingest:
python scripts/index_articles.py --run-id run_27f86e44394442 --dedupe

# Index a single gap only (incremental):
python scripts/index_articles.py --run-id run_27f86e44394442 --gap-id AUTO-01-G1

# Drop and re-index all rows for this run:
python scripts/index_articles.py --run-id run_27f86e44394442 --rebuild
```

### Query the index

```bash
# What sources are represented? (with PDF counts)
python scripts/query_articles.py --sources

# Top gaps by article count (with PDF counts):
python scripts/query_articles.py --gaps --limit 20

# Gaps that returned 0 PDFs:
python scripts/query_articles.py --zero-pdf-gaps

# Full-text search (porter-stemmed — finds inflected forms):
python scripts/query_articles.py --search "e-commerce India"

# All articles for one gap:
python scripts/query_articles.py --gap AUTO-01-G1

# List DOIs that appear in more than one row (cross-source duplicates):
python scripts/query_articles.py --doi-duplicates
```

### Example: `--sources` output

```
Source                   Total  With PDF  Metadata-only
--------------------------------------------------------
ebsco_api                 4245       548           3697
```

### Schema overview

The `articles` table stores one row per fetched markdown file with:
- Full citation metadata: title, authors, journal, pub_date, abstract, url, pdf_path, doi
- Provenance: run_id, gap_id, source_id, database_name
- Search context: bquery_original, bquery_normalized (JSON list), variant_index
- Gap context: gap_research_question (claim_text from the manuscript), gap_topic (chapter)
- Dedup: canonical_id (FK to articles.id — set on duplicate DOI rows; NULL on canonical)

The `articles_fts` FTS5 virtual table (porter tokenizer) indexes title, authors,
abstract, journal, and gap_research_question and is kept in sync via INSERT/DELETE/UPDATE
triggers.  Dedup (`--dedupe`) picks one canonical row per DOI (preference: has PDF >
source priority > earliest indexed_at) and sets `canonical_id` on all others; no rows
or files are deleted.

### DOI availability note

DOIs are **not present** in the existing fetched markdown files.  The EBSCO
`_write_ebsco_records` writer extracts data from the search-results DOM, which
does not include DOIs.  DOIs are available on article detail pages (visited
during the click-in PDF fetch) but the current writer does not capture them.
As a result, DOI dedup fires only for future data where DOIs are explicitly
written to the markdown.  Backfilling DOIs from saved HTML pages is a follow-up
task.

## Staged gap-recovery workflow

When a run surfaces gaps with low PDF counts (0–1 PDFs), a multi-phase staged recovery
improves yield without re-running the full pipeline.  The pattern is:

1. **Low-yield pass** — normalize + re-fetch gaps with 0–1 PDFs, then re-index.
2. **Re-index** — run `python3 scripts/index_articles.py --run-id <run_id> --dedupe`
   after each phase so the article index reflects the latest state.  The `--dedupe` flag
   resolves DOI-level duplicates introduced by multi-variant fetch (one article found by
   two query variants lands as two rows; dedup marks the weaker row via `canonical_id`).
3. **Medium-yield pass** — normalize + re-fetch gaps that now have exactly 2 PDFs; many
   of these gain 3–5 more articles.
4. (Optional) **High-yield pass** — gaps with 3–5 PDFs; diminishing returns, skip unless
   the corpus is still thin.

### Entry points

| Script | Purpose |
|---|---|
| `scripts/_yield_recovery.sh <gap_list> <label> [model]` | Generic recovery: normalize → fetch → re-index for any gap list |
| `scripts/_low_yield_recovery.sh` | Thin wrapper calling `_yield_recovery.sh` with `/tmp/low_yield_gaps.txt` + `low_yield` label |
| `scripts/_orchestrate_recovery.sh` | Dispatcher: polls for low-yield PID, re-indexes, snapshots medium-yield gaps fresh, then runs medium-yield |

### Launching the dispatcher (background)

```bash
nohup bash scripts/_orchestrate_recovery.sh >> logs/orchestrate_recovery.log 2>&1 &
```

The dispatcher is safe to launch while the low-yield pass is still running — it simply
polls `/tmp/low_yield_recovery_pid` every 60 s until the process exits, then proceeds.
If the PID file is missing (e.g. low-yield already finished), the dispatcher skips
directly to the re-index and medium-yield steps.

**Important**: the medium-yield gap list is re-snapshotted fresh inside the dispatcher
*after* low-yield completes, not from `/tmp/medium_yield_gaps.txt` (which was computed
earlier and is now stale).  This ensures the final PDF counts — not a mid-run estimate —
drive the medium-yield gap selection.

### Re-indexing manually

```bash
python3 scripts/index_articles.py --run-id run_27f86e44394442 --dedupe
```

Re-indexing is idempotent (existing rows are skipped via `UNIQUE(run_id, gap_id,
source_id, title)`).  `--dedupe` runs a pure SQL UPDATE pass — no files are modified.

## Tests
```bash
python3 -m pytest tests -q
```
