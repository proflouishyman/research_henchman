[2026-05-04] - Architecture-level UI improvements (writing companion)

Problem
Several high-value usability gaps remained after the ship-this-week pass:
  1. No "addressed" status for gaps — no way to distinguish written-up vs pending gaps.
  2. Manuscript parser cache could not be manually refreshed from the UI.
  3. Null-chapter intro-promise gaps (23) were only visible in the gap library, not the
     manuscript reader outline.
  4. "(no chapter)" label appeared in the sidebar but not "Cross-chapter theses".
  5. Dossier side panel closed on outside-click; panel not persistent.
  6. No keyboard shortcut to open dossier for current paragraph (only j/k existed).
  7. No help overlay listing shortcuts.

Root Cause
  1. `footnote_count` was available in `manuscript_parse.py` but never exposed as a
     per-gap "addressed" signal; no backend helper existed.
  2. `parse_manuscript` always returned the cache-hit; no bypass path existed.
  3. `ManuscriptOutline` only rendered `data.chapters` from the manuscript structure;
     null-chapter gaps from the index were never fetched there.
  4. The CROSS_CHAPTER_LABEL constant was local to `ChapterGroupedGapList`; not shared
     to `WriteShell` sidebar chapter filter.
  5. Side panel is only gated on `selectedParaId` being truthy; the panel already
     persisted in v3 — just needed the render condition relaxed.
  6. Only `j/k/Escape` were wired; `o` key handler was missing.
  7. No `?` key handler or help modal existed.

Solution
  Backend:
    - `adapters/gap_tree.py`: new `gap_addressed_status(conn, paragraphs, gap_links) →
      Dict[gap_id, bool]`. Pure-Python computation over flat paragraph list; no extra SQL.
    - `routers/library.py`: `GET /api/library/gaps` now includes `addressed: bool` per gap.
    - `GET /api/library/index` now includes `gap_count_addressed` per chapter.
    - `POST /api/library/manuscript/refresh`: deletes the cache file then re-parses;
      returns `{paragraph_count, gap_link_count, last_modified}`.

  Frontend types:
    - `GapTreeRow.addressed?: boolean` added to types.
    - `LibraryChapter.gap_count_addressed?: number` added.
    - `refreshManuscript()` API function added to `library_api.ts`.

  Components:
    - `ChapterGroupedGapList`: addressed gaps rendered at `opacity-50` + ✓ badge;
      "Show addressed" toggle (default OFF) hides addressed gaps; promoted
      `CROSS_CHAPTER_LABEL` as an export.
    - `WriteShell`: Refresh button (↻) in sidebar header (only on manuscript route),
      spinner during refresh, toast on completion; "?" HelpCircle icon; `HelpModal`
      component with shortcut table + workflow hint; sidebar chapter list shows
      "X un-addressed" count in amber when addressed > 0; "Reading queue" nav
      renamed to "Starred".
    - `ManuscriptOutline`: fetches index query; "Cross-chapter theses" virtual button
      at top of outline; per-chapter addressed/un-addressed annotation in stat line;
      "★ Starred (N)" toggle for the starredFilter prop.
    - `ManuscriptReader`: wires starredFilter state; adds `CrossChapterPane` for the
      virtual chapter; `o` key handler opens dossier for current paragraph; panel
      highlight (200ms border accent) on paragraph change via keyboard; side panel
      render condition relaxed (no longer requires `selectedParaId` to be set, works
      for cross-chapter gap clicks too).
    - `CrossChapterPane` (new): renders null-chapter gaps as paragraph-shaped rows with
      gap badge in gutter; clicking opens the side panel dossier.

  Tests:
    - `dossier.spec.ts`: added "score N badge" test; updated to check no card-level
      "tier N" badge inside source-card spans.
    - `routes.spec.ts`: updated queue test to assert URL stays on `/write/queue`.
    - `architecture.spec.ts` (new): 8 tests — help modal, j navigation, refresh button
      visibility, cross-chapter virtual chapter, orphan gap click, o key handler.

Notes
  - `gap_addressed_status` is pure-Python over the already-parsed paragraph list;
    no extra SQL query is needed per request.
  - The `addressed` computation is best-effort: wrapped in try/except in the API
    handler; if the docx is missing or parse fails, all gaps default to False.
  - The manuscript refresh endpoint is `POST /api/library/manuscript/refresh` — it
    invalidates the cache by deleting the JSON file, then calls `parse_manuscript`
    (which repopulates the cache). The react-query caches for
    `['manuscript-structure']` and `['library-index']` are both invalidated after.
  - Side panel `selectedParaId` requirement relaxed: cross-chapter gap clicks open
    the panel without a paragraph URL param. The `paraId` prop to `DossierSidePanel`
    now has a fallback sentinel `'panel'`.
  - Playwright: 28/28 pass (was 13/13); backend: 378/378 pass (unchanged).

[2026-05-04] - UI ship-this-week fixes (writing companion)

Problem
Multiple bugs and UX gaps blocked daily use of the writing companion:
  1. `/write/search?q=China` ignored the ?q= URL param — results never appeared.
  2. `/write/queue` showed "visit the dossier once to populate" for all starred
     articles because it relied on a localStorage article→gap cache that was only
     populated when the user had previously opened each dossier view.
  3. `/write` landed on the gap list, not the manuscript reader.
  4. `DossierSidePanel` rendered full-width cards in a 384px panel, stacking 5x tall.
  5. Drag handle was invisible at rest (opacity-0) — users couldn't discover dragging.
  6. The per-card "tier N" badge conflated card-level score with gap-level tier labels.
  7. No paragraph context was shown in the side panel — user couldn't confirm which
     passage the source list applied to.
  8. Null chapter buckets in the gap list showed nothing meaningful.

Root Cause
  1. `SearchPage` never read `useSearchParams()` on mount.
  2. `QueuePage.useArticleGapMap()` was a pure localStorage read; only populated
     when `DossierView.stampArticleGap()` had run (i.e., the dossier was visited).
     The server-side `resolve_gaps` endpoint existed but was unused here.
  3. `App.tsx` Navigate index → "gaps"; no redirect to "manuscript".
  4. `DossierSidePanel` passed `compact={false}` (implicitly) for Tier 3 entries.
  5. `DragHandle` CSS had `opacity-0` instead of `opacity-40`.
  6. Full-card score badge text was "tier N" — same word as gap-level tier badge.
  7. `DossierSidePanel` had no paragraph context props or UI block.
  8. `ChapterBlock` rendered `chapter.title` verbatim; empty string → blank heading.

Solution
  A1. `SearchPage`: import `useSearchParams`, add one-shot `useEffect` on mount
      that reads `?q=` and calls `setSearchQuery()`.
  A2. `QueuePage`: replace `useArticleGapMap` (localStorage) with a `useEffect`
      that calls `resolveGaps(starredIds)` on mount and on starred set change.
      Invert the mapping (gap_id → [article_ids]) to (article_id → gap_id).
      Articles with no resolved gap show under "(no gap mapping)" group.
  A3. `App.tsx`: change Navigate index from "gaps" to "manuscript". In
      `ManuscriptReader`, compute `firstBodyChapter` — first chapter whose
      non-heading paragraphs exceed 200 words — as the default when no slug in URL.
  A4. `SourceCard`: primary "Open" CTA now prefers PDF; adds secondary "(or open URL)"
      link when both pdf_path and url exist.
  B5. `DragHandle`: `opacity-0` → `opacity-40`; added `title` attribute.
  B6. `DossierSidePanel`: all `TierSection` calls now pass `compact` prop.
  B7. `DossierView`: added `<TopPicks>` strip above filters — 3 most cite-ready
      tier-3 entries sorted by (has_pdf, source_priority, pub_date_desc). Falls back
      to tier-2 if no tier-3. Hidden when tier-3 count is 0.
  B8. `DossierSidePanel`: added `paragraphText?: string` prop. Passed from
      `ManuscriptReader.handleGapClick` via `para?.text`. Panel renders a
      "Citing this passage:" block at the top when prop is present.
  B9. Full card score badge: "tier N" → "score N".
  B10. `ChapterBlock`: null/empty title → "Cross-chapter theses" with tooltip.
  B11. `DossierHeader`: added "Pull more sources →" link when tier-3=0 or
       intro_promise gap with < 5 tier-3 entries.
  B13. `also_in_sources` removed from inline meta; added to `HoverPopover`.
  C12. `Eye` (Read) import and button removed from `CardActions`.
  D14. `DragHandle`: onboarding hint tooltip shown on first hover, dismissed
       on drag-start, × click, or 5 s timeout; persisted to localStorage.
  E15. `dossier.spec.ts`: added drag handle opacity and TopPicks strip tests.
  E15. `routes.spec.ts`: updated `/write` redirect assertion to "manuscript".
  E16. New `search-and-queue.spec.ts`: ?q= seeding + queue without dossier visit.
  E17. New `manuscript.spec.ts`: default chapter has >50 paragraphs; side panel
       shows "Citing this passage:" label.

Notes
  - `stampArticleGap` function retained in QueuePage (exported) for DossierView
    backward compatibility; its localStorage cache is still populated on dossier
    render but is no longer used by QueuePage itself.
  - `resolve_gaps` returns `{mapping: {article_id_str: [gap_id, ...]}}`. The
    frontend inverts to `article_id_number → first_gap_id`. (Note: key is
    article_id, not gap_id — iterate `Object.entries(mapping)` as
    `[articleIdStr, gapIds]`, not `[gapId, ids]`.)
  - The "Pull more sources" link points to `/runs/new?gap=<id>`; the /runs side
    does not yet consume this param — the link establishes the bridge for a future ship.
  - Read state in `library.ts` and `user_marks` DB table is preserved; only the
    Eye UI is hidden. Can be re-added without schema changes.

[2026-05-04] - Playwright UI smoke tests + Tier 0 visibility fix

Problem
Running `curl .../gaps/TODO9/dossier` confirmed 91 consolidated entries
(tier-3:1, tier-2:1, tier-1:1, tier-0:88), but the user reported seeing only
3 tier sections in the rendered UI. Tier 0 appeared to be missing.

Root Cause
Two combined issues:

  1. The `TierSection` component contained a dead empty `if` block:
       if (entries.length === 0 && !open) {
         // Still render the header so the user can expand and see "0 entries".
       }
     This comment implied the section would be skipped in some cases, but
     the empty block was a no-op — the component always rendered. The real
     problem was visual: Tier 0 (88 entries, compact, collapsed-by-default)
     had no visual differentiation from a normal tier, so its collapsed
     header blended in at the bottom of a long list. Playwright tests
     confirmed the header WAS in the DOM — the user likely missed it visually.

  2. `SourceCard` lacked a `data-testid` attribute, making it impossible to
     confirm via automated test that clicking a tier header actually expanded
     and showed cards. Without this anchor, the first Playwright test run
     falsely appeared to confirm the bug on "source cards not visible."

Solution
  - Removed the dead empty `if` block from `TierSection`; added `opacity-70`
    to bucket "0" sections to visually de-emphasize them (false positives)
    while keeping them clearly present.
  - Added `data-testid="source-card"` to both the compact (`<div>`) and full
    (`<article>`) SourceCard rendering paths.
  - Installed `@playwright/test` + chromium in `frontend/`.
  - Created `frontend/playwright.config.ts` (headless, baseURL=:8000, 1 worker).
  - Created `frontend/tests/e2e/dossier.spec.ts` — 6 tests covering:
      * gap ID in header, summary count (91), all 4 tier headers visible,
        count badges (1/1/1/88), Tier 0 expand+cards, screenshot.
  - Created `frontend/tests/e2e/routes.spec.ts` — 7 smoke tests covering
    all /write/* routes and /runs.
  - Added `npm run test:e2e` script to `frontend/package.json`.
  - Built fresh dist; all 13 Playwright tests pass; 378 backend tests pass.

Notes
  - The empty `if` block in TierSection was likely a remnant of an early
    design that considered hiding zero-entry sections; the comment was
    left when the return-early was removed. The section always rendered.
  - `opacity-70` is applied at the `<section>` level so both the header
    and expanded content appear muted. Hover states remain fully visible.
  - Screenshots are gitignored (`frontend/tests/e2e/screenshots/`).

[2026-05-04] - Writing-companion UI v3 — manuscript reader + marks DB migration

Problem
After Wave 2 (search + characters + marks via localStorage), two surfaces
were still missing for the daily writing workflow:

  1. No in-browser manuscript reader — the user had to keep the .docx open
     separately and manually cross-reference gap IDs. No per-paragraph gap
     overlay existed.
  2. Marks (star/read) lived only in localStorage — lost on clear, not
     shareable, no server persistence.

Root Cause
Wave 2 deliberately deferred both features ("v3 follow-up"). The server-side
parsing infrastructure (python-docx walks, footnote detection, bracketed-TODO
regex) had never been wired to an HTTP layer. The marks DB table was
placeholder-commented in SOLUTIONS.md.

Solution
Eight pieces shipped together as v3:

  1. ``layers/manuscript_parse.py`` — new module. Walks the .docx via
     python-docx, emitting one dict per paragraph with: para_id (SHA1 of
     positional key + first 80 chars), chapter, heading_path, text,
     is_heading, heading_level, footnote_count (w:footnoteReference count),
     bracketed_todos (regex), char_offset. Disk cache keyed by (mtime, size)
     at ``data/.manuscript_cache/<filename>.json`` — re-parses only when
     the docx changes. ``paragraph_gap_links(paras, conn)`` links paras to
     gap_ids via three heuristics: heading-path substring overlap, 60-char
     claim-text prefix match, bracketed-TODO vs Pass-B claim match.
     ``group_into_chapters(paras, gap_links)`` produces the nested
     chapters/sections/paragraphs tree consumed by the API.

  2. New API endpoints under ``/api/library/manuscript/``:
       GET /manuscript/structure?docx=<path>  → {chapters:[...]} tree
       GET /manuscript/paragraph/{para_id}    → detail + resolved gap rows
     Default docx is the project manuscript. Live: 16 chapters, intro alone
     has 34 paragraph-gap links.

  3. Marks DB table ``user_marks`` in article_index.sqlite. Helpers in
     ``adapters/article_index.py``: ``ensure_marks_schema``, ``set_mark``,
     ``get_marks``, ``list_starred``. New endpoints:
       POST /api/library/marks        — upsert (idempotent)
       GET  /api/library/marks        — bulk fetch with starred/read filters
       POST /api/library/articles/resolve_gaps — article_ids → gap_ids

  4. Frontend migration: ``hydrateMarks()`` in ``library.ts`` store runs at
     app start, migrates any legacy localStorage['library.marks'] entries
     to the DB via the POST endpoint, then clears localStorage. Subsequent
     toggleStar/toggleRead calls write to the API (optimistic update + async
     PUT). The marks table in the DB is the canonical source of truth.

  5. ``ManuscriptReader`` component (three-pane layout):
       Left: ``ManuscriptOutline`` — chapter navigator with gap/uncited chips
       Center: ``ChapterScroll`` + ``ParagraphRow`` — per-paragraph gutter
               chips (footnote count ⚓N or ⚠ cite?, gap badges by type,
               TODO highlight)
       Right: ``DossierSidePanel`` — fixed-position slide-in; reuses
              TierSection/fetchDossier; tab strip for multi-gap paragraphs.

  6. Routes: ``/write/manuscript``, ``/write/manuscript/:chapterSlug``,
     ``/write/manuscript/:chapterSlug/:paraId``. Keyboard shortcuts: j/k
     next/prev paragraph; Escape closes side panel.

  7. ``WriteShell`` sidebar: Manuscript nav link added between index and
     Gaps. Order: Manuscript · Gaps · Search · Characters · Reading queue.

  8. Tests: 31 new tests across three files — ``test_manuscript_parse.py``
     (9 cases), ``test_library_api_manuscript.py`` (7 cases),
     ``test_library_api_marks.py`` (15 cases). Suite: 362 → 378 passing.

Notes
  - ``paragraph_gap_links`` heading-path heuristic uses substring overlap;
    precision depends on how consistently heading_path is set in gap_tree.
    In practice, 34+ links in the intro alone suggests good recall.
  - The side panel uses a ``fixed`` position overlay (right-0) so it works
    inside the parent scroll container without nested-overflow issues.
  - ``resolve_gaps`` uses articles.gap_id (primary ingest gap); articles that
    appear in multiple gaps via cross_gap_refs are not yet reflected here.
    That is a v4 TODO.

[2026-05-02] - Writing-companion UI Wave 2 — search + characters + marks

Problem
After Wave 1 shipped the dossier browser (commit 61d7c74), four
non-dossier surfaces were missing for the daily writing workflow:

  1. No corpus-wide search — the user had to know which gap a quote
     belonged to in order to find it. With ~10k scored rows in the
     corpus this was a hard ceiling on usefulness.
  2. No main-characters view — the company-profile gaps (CP3 Microsoft,
     CP4 Apple, CP6 Google, …) are the dominant evidence buckets but
     were buried inside the chapter tree.
  3. Drag-to-Word polish: hover affordance was minimal (no visible drag
     handle, no toast feedback on copy).
  4. No marks system — when prepping a chapter, the user wants to star
     "I'll cite this" rows and revisit them. There was no surface.

Root Cause
Wave 1 deliberately scoped to the dossier browser; the contracts
deferred to "Wave 2 = breadth" so Wave 1 could ship clean. The
``articles_fts`` virtual table was built in Wave 0 (article_index.py)
but had never been wired to an HTTP endpoint. ``gap_tree.gap_type =
'company_profile'`` already segregated the character gaps but had no
specialized listing view. ``<SourceCard>`` already attached three MIME
types to its dataTransfer but the user-facing affordance ended at the
Cite dropdown — no clipboard toast, no star, no read state.

Solution
Six pieces shipped together as Wave 2:

  * ``GET /api/library/articles/search`` — FTS5-backed search with the
    full filter set in the spec (source_id CSV, score_min 0–3, gap_id,
    year_from/year_to, has_pdf, limit≤200, offset). Reserved FTS5
    chars (``"*+-^():``) are stripped from each whitespace-token
    before each token is wrapped in phrase quotes — this neutralises
    operator parsing while preserving multi-word AND search semantics.
    bm25 ranks results; ``snippet(articles_fts, 2, '<mark>',
    '</mark>', '…', 32)`` returns a 200-char excerpt around the hit
    in column 2 (abstract). Year filtering is post-SQL (pub_date is
    freeform text); a wider page is fetched when years are active so
    pagination still terminates correctly. Empty/sanitized-to-empty
    queries return 400. Live: ``q=Bezos`` returns 174 hits with
    ``<mark>`` excerpts, ``q=Mercado`` returns 336.

  * ``GET /api/library/characters`` — company-profile gaps with two
    extra fields per row: ``top_tier3_titles`` (up to 3, ranked by
    source priority then pub_date desc — same ordering the dossier
    uses) and ``tier_histogram`` (alias of ``tier_counts`` for the
    chart component). Sort: ``-tier_counts['3'], -evidence_target,
    gap_id``. Live: 39 character cards, top is CP3 Microsoft with
    26 tier-3 hits.

  * Frontend ``/write/search``, ``/write/characters``, ``/write/queue``
    routes added to ``App.tsx``. ``<WriteShell>`` sidebar gains a top
    nav rail (Gaps · Search · Characters · Reading queue) above the
    chapter list, plus a ``<ToastHost>`` mounted at the shell.
    ``<SourceCard>`` polished:
      - hover-only drag-handle grip icon (left edge);
      - new actions in CardActions: Star (yellow when set, persisted
        to ``localStorage['library.marks']``), Read (green when set);
      - Copy/Cite dropdown now emits a 1.6 s toast via ``showToast``;
      - new optional ``snippet`` prop renders an FTS-highlighted
        excerpt under the abstract preview. Snippet HTML is sanitized
        by escape-then-reinject: ``<mark>``/``</mark>`` go to
        sentinels first, all other ``<>&"'`` are escaped, then the
        sentinels become real ``<mark>`` tags with our own classes.
        Only ``<mark>`` survives — every other tag is rendered as
        text. Defense-in-depth even though FTS5 only ever emits
        ``<mark>`` here.
  * ``library.ts`` Zustand store extended with ``searchQuery``,
    ``searchFilters``, ``searchResults`` (Load More appends),
    ``searchLoading/Error``, plus a ``marks`` map keyed by article id
    with ``starred``, ``read``, ``addedAt``. ``toggleStar`` /
    ``toggleRead`` write through to localStorage on every change;
    ``isStarred(id)``, ``isRead(id)``, ``starredCount()``,
    ``starredIds()`` selectors round it out.

  * ``QueuePage`` resolves article→gap membership via a second
    localStorage map (``library.article_gap``) populated lazily by
    ``DossierView`` whenever the user opens a dossier — this avoids a
    server-side marks table for v2 while still letting the queue link
    each starred article back to its dossier. Articles whose gap_id
    isn't yet stamped show in a "haven't reopened yet" footer.

  * Tests: ``test_library_api_search.py`` (10 cases — basic hit,
    empty-query 400, FTS sanitization, score_min, source_id filter,
    gap_id filter, has_pdf, year range, pagination, URL absolutize)
    and ``test_library_api_characters.py`` (5 cases — only
    company_profile rows, sort order, top_tier3_titles, histogram
    matches counts, empty corpus). Both files build their own
    fixture DB including the FTS5 virtual table + INSERT trigger so
    search round-trips exercise the real path. Backend suite:
    347 → 362 passing (15 new).

Live validation:
``curl '/api/library/articles/search?q=Mercado&limit=3'`` →
``{"total":336, ...}`` with three Mercado Libre headlines and
``<mark>``-wrapped snippets. ``curl '/api/library/characters'`` →
39 cards led by CP3 Microsoft (26 tier-3 hits, 104 total).
``frontend/dist`` builds clean (438 KB JS, 29 KB CSS — up from 412 KB
JS post-Wave 1, accounting for the three new pages + Toast + marks
plumbing).

Notes
- Marks are intentionally browser-local in Wave 2. Server-side table
  is deferred to v3 — this keeps the wave to the documented surfaces
  and avoids a schema change.
- The article→gap localStorage shim (`library.article_gap`) is a
  pragmatic v2 workaround. v3 should replace it with a single round-
  trip endpoint that takes a list of article ids and returns
  ``{id: gap_id}`` resolved from the articles table.
- Year filter does Python post-filtering because SQLite has no native
  regex and pub_date is freeform; cap on candidate set is implicit
  (FTS MATCH already narrows). For pathological queries with no
  year hits, the page becomes empty rather than ever exhausting
  the candidate set — acceptable for v2.
- ``simplify`` skill was not run; the codebase shape mirrors Wave 1
  conventions (separate components per page, store-extended-not-
  forked, citations.ts reused).
- Dark mode parity: the new components rely on the same color tokens
  Wave 1 used (``bg-surface-card``, ``border-border``, ``text-ink-*``)
  which already have ``.dark`` overrides in ``index.css``. No new
  CSS-in-JS introduced.

[2026-05-02] - Writing-companion UI MVP — dossier browser

Problem
After Wave 2 finished pulling and scoring 88 gaps, the user could only
read dossiers as static markdown files written by
``scripts/generate_dossiers.py``. The two non-negotiables were
"everything must be in DB" (so dossiers stay in sync as scoring
re-runs) and the preview UX: see the LLM's WHY + the source's abstract
*before* incurring the click to open the PDF / URL.

Root Cause
``scripts/generate_dossiers.py`` owned both the data-assembly logic
(``norm_title``, ``dedupe_within_gap``, ``pick_primary``,
``build_cross_gap_index``, ``absolutize_url``) and the markdown
formatting in one module, so any second consumer (an API, a different
renderer) would have had to re-implement the assembly and would drift
out of step. The frontend also lived in a single hard-coded ``/runs``
tree with no second-mode entry point.

Solution
Three pieces shipped together as Wave 1 of the writing companion:

  * ``layers/dossier_render.py`` — extracted assembly layer. Public API
    promotes the helpers that used to be private to the script
    (``norm_title``, ``dedupe_within_gap``, ``pick_primary``,
    ``build_cross_gap_index``, ``absolutize_url``,
    ``render_url_or_pdf``) and adds the high-level
    ``assemble_dossier(conn, gap_id, *, cross_gap_idx=None) -> dict``.
    The dossier dict has a stable schema documented in the function
    docstring and mirrored in ``contracts.LibraryDossierOut``. Markdown
    writer is now a thin wrapper that imports these helpers — no
    duplicated logic. Round-trip verified: regenerated CP31 markdown
    after the refactor diffs zero against the checked-in file.

  * ``routers/library.py`` — new FastAPI router under ``/api/library/``.
    Four endpoints: ``GET /index`` (chapter-grouped sidebar payload),
    ``GET /gaps`` (flat list with chapter/gap_type/tier/status filters
    + article counts joined), ``GET /gaps/{gap_id}`` (single row +
    counts), ``GET /gaps/{gap_id}/dossier`` (structured dossier per
    ``assemble_dossier``). Mounted from main.py via include_router. New
    Pydantic models in ``contracts.py`` document the wire shape.
    Article counts are computed in one ``GROUP BY gap_id, score`` pass
    so listing all 88 gaps does not issue an N+1 query.

  * Frontend — ``/write/...`` route tree built on react-router-dom.
    TopBar gains a Runs/Write mode toggle. ``/write/gaps`` shows a
    chapter-grouped gap list with a mini tier-counts bar
    (``[3:5 · 2:24 · 1:20 · 0:46]``). ``/write/gaps/:gapId`` renders
    the dossier live: header (claim + research_question + summary
    counts), filter strip (source toggles · score floor · has-PDF
    only), four collapsible tier sections. Each ``<SourceCard>`` puts
    the LLM's WHY in a prominent amber callout block, the abstract
    preview clipped to 3 lines (with "more" expander), a one-click
    Open (PDF → ``/api/orchestrator/files``, URL → new tab), and a
    Cite dropdown that copies Chicago / ``(Last Year)`` / link to
    clipboard. Cards are draggable: ``dataTransfer`` carries all three
    citation forms under different MIME types
    (``text/plain`` = Chicago, ``text/x-citation-short`` = short,
    ``text/uri-list`` = pdf path or URL) so dropping into Word/Pages
    pastes the correct form. A 300 ms hover side-popover surfaces the
    full untruncated WHY + abstract — this is the user's
    "preview before clicking" non-negotiable.

Live validation: ``GET /api/library/gaps/CP31/dossier`` returns the
expected 5 tier-3 / 24 tier-2 / 20 tier-1 / 46 tier-0 entries with
absolutized HathiTrust URLs and full WHY text. ``GET /api/library/index``
returns 17 chapters and the same corpus_total_rows the markdown
``INDEX.md`` reports. The markdown generator is a one-line behavior
change (now imports from layers.dossier_render) and produces
byte-identical output. ``frontend/dist`` builds clean (412 KB JS,
27 KB CSS).

Tests: ``tests/test_dossier_render.py`` (8 cases — pure helpers + a
fixture-DB round-trip on ``assemble_dossier``) and
``tests/test_library_api.py`` (8 cases — TestClient against an
in-memory fixture DB exercising all four endpoints, including
gap_type CSV filter and the merged-source / cross-gap-refs path).
Full backend suite: 332 → 348 passing.

Notes
- Cross-gap refs filter (score>=1 only) intentionally excludes tier-0
  noise so the cross-gap chip on a card shows real overlap, not
  shared false positives.
- Frontend ``library_api.ts`` is a separate module from ``api.ts`` so
  changes to the orchestrator API surface don't ripple into write-mode
  callers.
- Out of scope this wave (week 2): the manuscript reader (.docx
  overlay), the corpus search page, the characters dashboard, the
  coverage matrix, and user-facing marks/starring. Wired the store
  with TODOs so adding marks is a single-slice change.

[2026-05-03] - Wave 2: SEC EDGAR + gap-type-aware pulling

Problem
After Wave 1.5 shipped 58 tier-1 + 16 tier-2 gaps in the new ``gap_tree``
table, no infrastructure existed to consume those gaps and pull
documents. The legacy pull pipeline only handled the old
``AUTO-NN-G1`` heuristic detector rows and didn't know how to route by
``gap_type`` (intro_promise vs research_gap vs company_profile vs
editorial_todo). It also had no source for SEC regulatory filings —
the obvious primary source for company_profile gaps.

Root Cause
Wave 1 deliberately scoped detector-only work; Wave 2's job was
contract-first puller infrastructure that consumes ``gap_tree`` and
dispatches per-gap-type. Three components were missing:

  1. SEC EDGAR client (lookup_cik / list_filings / fetch_filing_text)
     with rate limiting, on-disk ticker cache, and the SEC-required
     User-Agent header.
  2. Per-gap-type query planner that knows which sources to hit and
     how many queries to generate per source.
  3. Dispatcher that routes (query, source) to the right puller and
     manages resume state via gap_tree.status.

Solution
Four new modules + one tiny bug fix to the ProQuest puller:

  * ``adapters/sec_edgar.py`` — public surface lookup_cik() /
    list_filings() / fetch_filing_text(). The company-tickers JSON is
    cached at ``data/sec_edgar/company_tickers.json`` after first
    download; multiple matches resolve to the smallest CIK (older
    company wins). 100 ms throttle between requests to stay under
    SEC's 10 req/s ceiling. 12 unit tests with mocked HTTP.

  * ``layers/gap_query_planner.py`` — plan_queries(node, llm) returns
    [(query, source_id), ...]. Routing rules:

      intro_promise          → HathiTrust + EBSCO + PQ-US (broad+narrow)
      intro_promise tier 1   → ALSO PQ-International (1 query)
      research_gap           → EBSCO + HathiTrust (1 query)
      company_profile        → SEC EDGAR + EBSCO + HathiTrust + PQ-US
      editorial_todo         → skipped (returns [])

    Per-gap-type system prompts are tuned: 60-120 char Boolean for
    intro_promise/company_profile, 40-80 char tight for research_gap.
    Default model is llama3.1:8b (fast, terse, structured) — qwen3.6
    tier was deliberately avoided after the Pass A long-input stall
    found 2026-05-03. 15 unit tests.

  * ``layers/pull_dispatch.py`` — pull_gap(conn, node, ...) plans +
    dispatches every (query, source) tuple to the right puller via thin
    shims (_pull_sec_edgar, _pull_ebsco, _pull_hathitrust,
    _pull_proquest). Browser ``page`` is opened once by the caller and
    passed in so we don't pay Playwright launch cost per query.
    EBSCO reuses ``adapters.keyed_apis.EbscoApiAdapter`` (EIT API →
    EDS API → seed-link fallback chain works unchanged). HathiTrust /
    ProQuest reuse ``scripts.pull_hathitrust.search_hathitrust`` and
    ``scripts.pull_proquest_newspapers.search_proquest`` directly as
    importable functions — no orchestration duplication. 7 unit tests
    cover routing, status flips, and SEC seed-JSON write path.

  * ``scripts/pull_gap_tree.py`` — CLI mirroring score_relevance.py:
    --tier / --gap-ids selectors, --dry-run, autogenerated run_id
    (``gaptree_<YYYYMMDDhhmm>``), Telegram updates per gap, resume-safe
    via ``gap_tree.status``.

  * ProQuest puller fix (``scripts/pull_proquest_newspapers.py``):
    on US Newsstream, pressing Enter inside ``#searchTerm`` does NOT
    bubble to the form — the search never fires. Calling
    ``form.submit()`` directly works on both US and International
    Newsstream. Discovered live during Wave 2 smoke test on CP23.
    Single 5-line patch; both collections pulled 50 records after.

Validated end-to-end. Live smoke test on CP23 (Blockbuster) + IP1
(China retail) wrote 356 records:

  CP23  hathitrust_fulltext        124
  CP23  proquest_us_newsstream      65
  IP1   hathitrust_fulltext        167

Both gaps' status flipped to ``pulled``. Blockbuster is correctly
diagnosed as having no SEC CIK (defunct, delisted post-bankruptcy) —
the dispatcher treats this as 0 records, no error (status='pulled').
Sample EDGAR titles via the new client: "Amazon — 10-K — 2026-02-06"
(verified live).

Notes
- EBSCO returned only 1 record per query in the smoke test
  (provider_search seed only — EIT API auth rejected). This is a
  pre-existing issue with EBSCO credentials in the environment, not a
  Wave 2 bug. The dispatcher correctly writes the seed JSON file
  regardless; downstream document_fetch.run_fetch can browse-pull
  the actual articles when EBSCO sessions are live in the CDP browser.
- HathiTrust returned 0 once during a re-run of CP23 (rate-limit /
  random timing). Re-running succeeded. The 10-consecutive-zeros
  abort threshold in the existing puller still applies.
- Quality concerns on LLM-generated queries: llama3.1:8b is consistent
  on the company_profile prompt (always emits the synonym pattern
  ``("Wal-Mart" OR "Walmart") AND (history OR ...)``), occasionally
  generates 6+ alternatives in the inner OR group for intro_promise
  which inflates the 60-120-char target. Truncation at 200 chars
  keeps the parser safe; future tightening could use a hard 100-char
  cap for ProQuest collections specifically.

[2026-05-03] - Pass A/F detector quality fixes + --include-covered flag

Problem
After Wave 1 shipped (commit 7086523), three quality issues blocked progress to Wave 2 (pulls):

1. Pass A under-extracted: qwen3.6:35b on the 12K-char intro prompt hung indefinitely (>12 min, never returned). Even when it did return, only 5 vague claim-sentences were produced instead of the 20-40 specific topical promises the spec called for.
2. Pass F missed every user-named target (Mercado Libre, Shein, Temu, Flipkart, Alibaba, Tmall, Alipay) because (a) the legacy text-based ``_split_sections`` returns 26 sections vs 94 real docx headings — entity-dedicated empty headings like "Mercado Libre" were invisible to the section walker; (b) the "no-dedicated-section" detection path required ``in_intro=True`` which excluded all body-only protagonists (Flipkart 19 mentions, Alibaba 47 mentions, both not in intro).
3. The user later clarified that even well-developed main-character sections (FedEx 994w, UPS 1157w, Wal-Mart 1442w, Amazon, eBay, Netflix) deserve supplementary 10-K / trade-press / scholarly-history pulls — but the detector was hard-coded to skip sections >800 words.

Root Cause
1. Long-input prompts on qwen3.6:35b: the model is excellent for short-prompt analytical scoring (10s/source) but stalls indefinitely on multi-thousand-token listing prompts. Wrong tool for "list everything" extraction tasks.
2. Legacy ``layers.analysis._split_sections`` uses regex over flattened text and only catches ~28% of the manuscript's real headings; entity-dedicated empty subsections never make it into its output.
3. The original ``in_intro`` requirement was a sensible noise-filter for two-character entities (e.g. "AI") but mis-fired on companies the manuscript only develops in body chapters.

Solution
Three surgical patches to ``layers/gap_detector.py`` and ``scripts/build_gap_tree.py``:

1. Pass A primary model switched from qwen3.6:35b to llama3.1:8b. Loud lesson from today's iteration: qwen3.6 stalls on long-input listing tasks. llama3.1:8b returned 28 high-quality intro promises in seconds. The CLI ``--model`` arg can still override per-pass.
2. New ``_docx_heading_sections()`` helper reads the docx via python-docx using paragraph-style metadata (Heading 1/2/3/Title) as the truth source — returns 94 sections vs 26. ``_find_dedicated_section_docx()`` is used as a fallback inside ``detect_pass_f`` when the legacy splitter misses an entity heading. Legacy splitter unchanged for the rest of the codebase.
3. Body-only protagonists are now eligible. New constant ``MAIN_CHARACTER_BODY_FLOOR = 10``: an entity with >= 10 body mentions qualifies even without an intro reference. Catches Flipkart, Alibaba, Bezos.
4. New ``--include-covered`` CLI flag on ``build_gap_tree.py`` plus an ``include_covered`` kwarg on ``detect_pass_f``. When set, well-developed (>800 word) main-character sections also emit CP gaps with ``rationale="supplementary (covered)"`` and ``evidence_target=60``.

Validated end-to-end. Live counts after the fix: Pass A 28 IP gaps; Pass B 7 research_gap + 14 editorial_todo (auto-rejected by classifier); Pass F 39 CP gaps with --include-covered (26 tier-1, 13 tier-2). All user-named targets present.

Notes
The qwen3.6:35b-vs-llama3.1:8b split is now official: long-input listing tasks → llama3.1:8b; structured per-item analytical scoring → qwen3.6:35b. The slow agent-loop iteration where qwen3.6 hung for 12+ min on the long Pass A prompt was a clean falsification of the "always use the bigger model" intuition.

[2026-05-03] - Gap-tree schema + Pass A/B detector (Wave 1 of detector overhaul)

Problem
The legacy paragraph-level detector in layers/analysis.py emits 185
``AUTO-NN-G1`` gaps with significant redundancy (the same claim surfaces
multiple times because every paragraph mentioning it gets analyzed
independently). It also misses two structural classes of gaps a manuscript-
aware detector should catch: (a) intro-promise mismatches — topics the
author flags in the Introduction but never develops in a later chapter,
and (b) the author's own bracketed TODOs scattered through the body
(``[need section on X]``). Both are tier-1 work items the legacy detector
either drowns out or omits entirely.

Root Cause
Single-pass paragraph-level analysis cannot reason about whole-manuscript
structure. The Introduction-vs-chapter coverage check is a graph problem
(promise → matched chapter → development depth), and bracketed TODOs are
explicit author intent that should never be diluted into the same heap as
implicit hedge-language gaps.

Solution
Built a separate ``gap_tree`` SQLite table (in the same
``data/article_index.sqlite`` DB as the article index) that models gaps
as a tree, lets multiple detector passes contribute their own subtrees
without colliding, and assigns a tier + evidence_target per node.

Components shipped:
  * ``adapters/gap_tree.py`` — schema (gap_id PK, parent_gap_id self-FK,
    depth, tier, gap_type, chapter, heading_path, claim_text,
    research_question, source_locator, evidence_target, detector_pass,
    status, rationale), plus insert_node / list_nodes / count_by_pass /
    gap_exists / fetch_research_question / update_research_question
    helpers. ID prefixes: ``IPn`` for Pass A, ``TODOn`` for Pass B;
    ``AUTO-`` is reserved for the legacy detector so the two never collide.
  * ``layers/gap_detector.py`` — ``detect_pass_a`` (LLM-driven intro-
    promise extraction → deterministic heading pairing → tier-1 if
    matched section <80 words, tier-2 if 80-300, skip if >300) and
    ``detect_pass_b`` (regex over body + section headings for ``[...]``
    TODOs, single-LLM-call research_question rewrite per TODO; resume-safe
    via the DB).
  * ``scripts/build_gap_tree.py`` — CLI mirroring score_relevance.py
    style. ``--pass A,B`` selects passes, ``--review-file`` writes a
    human-readable markdown file the user edits to approve/reject each
    gap. Resume-safe: existing gap_id rows are skipped.
  * Tests: ``tests/test_gap_tree.py`` (13 cases — schema, insert/list,
    count_by_pass, resume helpers) and ``tests/test_gap_detector.py``
    (8 cases including a stubbed-LLM Pass A round-trip and a Pass B
    resume scenario).

Live validation against ``manuscript.docx``:

  Pass A: 5 IP gaps (each paired with a real section heading; 3 mapped to
          the manuscript's pre-introduction "Manuscript Body" pseudo-heading
          because the LLM extracted promises about premises stated in the
          intro itself rather than future-chapter promises — see Notes).
  Pass B: 21 TODO gaps (out of 22 raw bracket annotations the manuscript
          contains).

How to run:

  python3 scripts/build_gap_tree.py --pass A
  python3 scripts/build_gap_tree.py --pass B
  python3 scripts/build_gap_tree.py --pass A,B
  python3 scripts/build_gap_tree.py --review-file data/intro_promises_review.md

Notes
- LLM-quality concern (Pass A): qwen3.6:35b-a3b-mlx-bf16 produced only 5
  intro promises against a 2985-word intro; the spec expected 15-25. The
  prompt may need tightening (current prompt is too permissive about
  "thesis claims" — the model returns broad framing statements that
  describe what the intro itself asserts rather than what later chapters
  will cover). Surface for review before Pass C is built.
- Pass B's bracket regex was extended after first live run from
  ``[A-Za-z]...`` to ``\[\s*[A-Za-z]...`` because the manuscript has
  several TODOs prefixed with leading whitespace
  (``[       MORE ON THIS PLEASE]``). It also now scans section *headings*
  not just bodies, because ``_split_sections`` promotes ALLCAPS bracketed
  lines (e.g. ``[HOW DID IT GET TO EBAY]``) to be section headings, which
  would otherwise hide them from a body-only scan.
- ``layers/analysis.py`` was NOT modified per Wave 1 scope. The legacy
  ``AUTO-NN-G1`` detector continues to populate the existing ``articles``
  table; the new gap_tree is fully separate.
- Bracketed TODOs that would be filtered as "citations" are short
  (≤4 words ending in a 4-digit year), so genuine TODOs like
  ``[Clinton refuses to use Taft-Hartley]`` (5 words, no year) survive
  the filter.

[2026-05-02] - HathiTrust full-text book coverage puller

Problem
The manuscript's gap report covers 185 claims spanning the full pre-Internet → present sweep of e-commerce history, but the existing pull pipeline only had EBSCO + ProQuest sources. Pre-1990 retail history (mail-order catalogs, department-store credit, direct-marketing precedents to Amazon) is thin in EBSCO/ProQuest because those databases skew academic-journal and modern-newspaper. HathiTrust's 17M-volume digitized-book collection — heavy on pre-1923 public-domain works plus search-only access to 20th-century monographs — is the right surface for that historical context.

Root Cause
HathiTrust has two distinct search surfaces and they yield wildly different recall:
- catalog.hathitrust.org/Search/Home — metadata-only Solr index. DOM probe with "Sears Roebuck mail order catalog" returned 4 hits.
- babel.hathitrust.org/cgi/ls?field1=ocr — full-text OCR search inside the books. Same query returned 443,571 hits / 148,729 Full View. Same article.record DOM as the catalog.

The Bibliographic API at catalog.hathitrust.org/api/volumes/ is identifier-only (ISBN/ISSN/OCLC/LCCN) — no free-text REST endpoint exists publicly. WebFetch returns 403 (anti-bot). Selectors on common Solr-result names (li.result, div.searchResult) all returned 0 because HathiTrust uses a Bootstrap-grid layout: article.record > div.flex-grow-1 > h3.record-title plus dl.metadata > div.grid > <dt>label</dt><dd>value</dd>.

Solution
Built `scripts/pull_hathitrust.py` modeled on `pull_proquest_newspapers.py`. Behavior:

1. Reads the gap report and parses (gap_id, chapter, claim_text) tuples.
2. No region pre-filter — HathiTrust covers the whole manuscript topic well; --gap-ids and --limit allow narrow targeting when needed.
3. For each gap, generates a HathiTrust-friendly natural-language query via local Ollama (default llama3.1:8b — same terse-prompt rationale as ProQuest). System prompt steers the LLM toward period vocabulary ("mail order", "department store", "Sears Roebuck", "Montgomery Ward") and quoted phrases over deep Boolean — Solr OCR matching prefers two short concept groups over a 200-char nested expression.
4. Navigates `babel.hathitrust.org/cgi/ls?q1=<query>&field1=ocr&a=srchls&pgs=100&anchor=search` (no auth required for search; HathiTrust gates only the full-text reader for limited items). 1.5-3.5s jitter, page-1-only (100 results cap).
5. Extracts `article.record` elements using stable selectors discovered 2026-05-02 by live DOM probe: `h3.record-title` for title, `dl.metadata div.grid > dt|dd` for Published/Author/Subject/Language/Publisher pairs, `a[data-clicktype="catalog"]` and `a[data-clicktype="pt"]` for the two access links. The pt link's text ("Full view" vs "Limited (search-only)") indicates copyright access level. The cover element's `data-hdl` attribute carries the stable HathiTrust ID (e.g. "mdp.49015001020396").
6. Writes records as JSON in the same shape as ProQuest seeds (title/url/authors/journal/pub_date/abstract/doi/query/gap_id/quality_label/source/link_type) plus HathiTrust-specific extras (hathi_id, access, subject, language). The article indexer's _ingest_seed_json walks these automatically with no indexer change required.

Bot-detection / abort logic mirrors the ProQuest puller: 10 consecutive empty results trips the abort, "are you a robot" or "captcha" or "503" titles trigger clean skip, no retry-with-delay loops.

Validated end-to-end: single-gap test on AUTO-01-G1 (Amazon "everything store" claim) returned 100 records. Top hit was Brad Stone's "The everything store: Jeff Bezos and the age of Amazon" (2013) — exactly the right book. Access split: 87 Limited / 13 Full View. After indexing, the article index gained `hathitrust_fulltext` as a new source (9,826 → 9,926 rows total). Authors and pub_date extracted on 100% of records; publisher field empty on most because HathiTrust's results page shows it inconsistently — title + URL + author + date are the load-bearing fields and those are reliable.

Notes
HathiTrust full-book PDF download is intentionally out of scope for this puller — Limited items are search-only by copyright; Full View items download page-by-page via a different API (/cgi/imgsrv) that's its own scraping problem. Metadata + reader URL is the deliverable; users follow the URL when they want to read. The --full-view-only flag filters to public-domain-readable items when desired (default keeps both since Limited records still surface citation metadata for follow-up via interlibrary loan). The keyword pre-filter used in the ProQuest India/China puller was deliberately omitted here because HathiTrust's collection breadth makes it useful across the whole manuscript, not just specific regions; --gap-ids and --limit cover the targeted-retry workflow.

[2026-05-02] - Article index extended to ingest JSON seed records

Problem
ProQuest newspaper records (2,248 records across 45 JSON files) were invisible to the article indexer. `ingest_pull_output` only walked `fetched/*.md` files; ProQuest sources write JSON directly to `<gap>/<source>/*.json` with no `fetched/` subdirectory. Running `--sources` on the index showed only `ebsco_api` (7,783 rows).

Root Cause
The ingest loop skipped source directories without a `fetched/` subdirectory (`if not fetch_dir.is_dir(): continue`). No JSON article ingestion path existed.

Solution
Added `_ingest_seed_json(conn, src_dir, ...)` in `adapters/article_index.py`. The function reads `*.json` files from a source directory, skips records with `link_type == "provider_search"` (EBSCO search-parameter records), and inserts real article records with `pdf_path = NULL` and `md_path = NULL`. `ingest_pull_output` now calls this function when a source directory has no `fetched/` subdirectory; the two passes are mutually exclusive so .md files always take priority over JSON for the same source. The ProQuest `query` field is stored as `bquery_original` when no dedicated bquery fields are present. 9 new tests added covering: mixed md+JSON ingest, idempotency, .md precedence, empty pub_date tolerance, 50-record batch, source_id appearance, provider_search skip, and query→bquery_original fallback.

Notes
Live ingest on run_27f86e44394442 added 2,043 rows (7,783 → 9,826 total). ProQuest sources in index: proquest_international_newsstream (906), proquest_us_newsstream (759), proquest_historical_newspapers (378). All pdf_path values for JSON-sourced rows are NULL by design — ProQuest TOS prohibits systematic PDF downloading; metadata-only is the current deliverable.

[2026-05-02] - Two-way Telegram bridge with command dispatch

Problem
The hourly Telegram pinger (_hourly_status_telegram.sh) was one-way — it pushed status but provided no way for the user to issue commands or leave notes mid-run. The user wanted to interact with a running pipeline from their phone.

Solution
Rewrote _hourly_status_telegram.sh as a bidirectional bridge. The main bash loop wakes every 60 s to call getUpdates, advances last_update_id in logs/telegram_poll_state.json (idempotent across restarts), and dispatches recognised slash commands to Python handlers. Full hourly status is still sent on the 3600 s tick. All Telegram I/O is in a single heredoc'd Python block using stdlib urllib only.

Commands shipped:
  /status  — send fresh status immediately
  /stop    — write a stop-flag file, send confirmation, exit cleanly
  /note    — append timestamped text to logs/telegram_user_notes.log
  /runs    — pgrep + ps for known pipeline scripts, PID + elapsed
  /disk    — file counts (PDF/MD/JSON) + total MB under data/pull_outputs/
  /help    — list all commands

Security: only messages whose chat.id matches TELEGRAM_CHAT_ID are acted on; all others are silently dropped.

Notes
- State file: logs/telegram_poll_state.json — persists last_update_id so no message is reprocessed on restart.
- Stop flag: /tmp/tg_bridge_stop_<PID> — checked after every poll cycle so /stop is honoured within ~60 s.
- No new pip dependencies; no existing scripts modified.

[2026-05-02] - SKIPPED: JSTOR is reCAPTCHA-protected — needs human input

Status
Deferred. Documented per the user's "if unfixable without my input, move on" guidance.

Problem
JSTOR was on the source-expansion roadmap. Probed via JHU EZproxy (databases.library.jhu.edu/databases/proxy/JHU03294 → www.jstor.org/). Auth succeeded ("Access provided by louishyman@jhu.edu" visible). The basic-search URL `https://www.jstor.org/action/doBasicSearch?Query=...&so=rel` returns a page titled "JSTOR: Access Check" with the body text: "Our systems have detected unusual traffic activity from your network. Please complete this reCAPTCHA to demonstrate that it's you making the requests and not a robot." Block reference / VID / IP all logged in the page text.

Root Cause
JSTOR has aggressive bot-detection that triggers on direct URL search even when the user is authenticated through their institutional EZproxy. Possibly tripped by the existing `playwright_adapters.py` JSTOR adapter from earlier runs (which navigates similar URLs). The user's IP has been flagged.

Why I'm not fixing tonight
1. reCAPTCHA cannot be solved autonomously — the user has to physically click "I'm not a robot" in the CDP-attached browser.
2. The user is asleep. The autonomous-mode plan explicitly authorizes skipping unfixable sources.
3. Even after the user solves it once, JSTOR's session-rotation may re-trigger the block on subsequent automated searches. Per Opus's failure-mode advice: "If captcha triggers despite using the user's real authenticated browser, automation isn't viable today — move on."

Path forward (when user is available)
1. User opens the CDP-attached Chrome window, navigates to JSTOR via Catalyst portal, clicks the reCAPTCHA, then waits a few minutes for the IP to come off the suspicion list.
2. Probe again — if the search results page renders without the Access Check, do a one-time DOM walk (the existing `_JSTOR_JS = li.result, .title a` selectors are stale; need fresh discovery).
3. Either extend `pull_proquest_newspapers.py` to be source-agnostic (it already takes a collection arg; the JSTOR work would add a `jstor` collection with appropriate base URL + extractor) OR write a dedicated `pull_jstor.py`.

[2026-05-02] - ProQuest US Newsstream coverage via Basic Search fallback

Problem
The pull_proquest_newspapers.py script worked for International Newsstream (basic-search UI) but failed for US Newsstream and Historical Newspapers — both of those EZproxy URLs land on the Advanced Search page after auth, which lacks the `#searchTerm` textarea the script's form-submit flow expects.

Solution
Added a fallback in `search_proquest`: if `#searchTerm` is absent post-auth, find a "Basic Search" link on the page and navigate to it (its href is per-collection, e.g. `/usnews?accountid=...`). This yields the same form-fill flow that International Newsstream uses. Documented the pattern. The Historical Newspapers collection (JHU05070) lands on a different advanced-search variant and may need additional work — deferred.

Validated: US Newsstream test on AUTO-02-G1 returned 50 records. Full US Newsstream run on all 17 India/China gaps launched in background after commit 8ef7879.

[2026-05-02] - ProQuest International Newsstream coverage for India/China manuscript gaps

Problem
The manuscript on the history of e-commerce had two acknowledged coverage holes: India and China. The existing pull pipeline only routed queries to EBSCO Academic Search Ultimate + Business Source Ultimate, which are USA/Europe-leaning. Newspaper coverage from Indian and Chinese papers (Times of India, South China Morning Post, Beijing Review, Grocer's Asia coverage, Financial Times Asia) is the cleanest way to fill that hole. ProQuest International Newsstream is the right database — JHU has institutional access via EZproxy.

Root Cause
The existing playwright_adapters.py scaffolding for `proquest_historical_newspapers` was hardcoded to the New York Times Historical archive (`hnpnewyorktimes`) — wrong collection for India/China. No upstream gap-analysis routing produced ProQuest seed records. Zero ProQuest data on disk across any prior run.

Solution
Built `scripts/pull_proquest_newspapers.py` (option B from the 2026-05-02 Opus consultation: focused enrichment script over upstream-routing refactor). Behavior:

1. Reads gaps from `data/manuscript_exports/<...>/gap_report_<run_id>.md` — extracts per-gap (gap_id, chapter, claim_text) tuples.
2. Pre-filters gaps by keyword (no LLM cost) — INDIA_KEYWORDS / CHINA_KEYWORDS sets cover named cities, founders, major platforms (Flipkart, Snapdeal, Alibaba, Tmall, JD.com, etc.).
3. For each relevant gap, generates a single newspaper-flavored Boolean query via local Ollama. Default model: llama3.1:8b (gpt-oss:20b, the academic A/B-experiment winner, treats this terse prompt as chat and asks for clarifications — wrong tool for newspaper queries). System prompt emphasizes period vocabulary, named entities, localizers, simpler Boolean (2-3 concepts AND'd).
4. Drives the CDP-attached Chrome through JHU's EZproxy (`databases.library.jhu.edu/databases/proxy/JHU07220` for International Newsstream), establishes session, fills `#searchTerm` textarea, submits via Enter (the form has no clickable submit button — Enter triggers the form's onsubmit handler).
5. Extracts results from `li.resultItem` containers using selectors discovered by walking the live DOM: `.truncatedResultsTitle` for full title, `a[id^="citationDocTitleLink_"]` for the canonical detail URL (NOT the first `a.previewTitle` — that's the thumbnail anchor with empty text), `.scholUnivAuthors` for byline, `.jnlArticle` for the citation block.
6. `_parse_pub_info` splits ProQuest's citation format ("Grocer; Crawley (Mar 21, 2026): 30,31...") into journal+date by extracting paren-wrapped dates first.
7. Writes records as JSON into `<gap_id>/proquest_international_newsstream/<query_slug>.json` matching the existing seed-record schema so the downstream fetch pipeline (`_classify_record`) can pick them up naturally.

Anti-bot countermeasures per Opus's failure-mode list:
- 2-5s random jitter between gap searches
- Detect `sessionexpired` / login redirects / captcha titles → skip cleanly, never retry
- Bail after 5 consecutive empty/error results (likely session lost or selector breakage)
- Cap at 50 results per query (volume shock prevention; ProQuest TOS)
- Concurrency = 1 (no parallel workers; respectful pace)

Validated end-to-end: full corpus run produced 850 records across all 17 India/China-relevant gaps (50/gap = the cap, indicating recall headroom). Top newspapers in the data: FT.com, Beijing Review, Grocer, Portafolio (Spanish-language Latin American coverage). Sample article titles: "Alibaba and Pinduoduo battle", "China's midyear shopping festival pulls in record online sales", "How Jenny Lee became Asia's Iron Woman of venture capital", "Alibaba y Jack Ma no paran su expansión".

Notes
22/50 records per query had full journal+date metadata extracted; the rest had empty journal/date because they use slightly different DOM blocks (e.g. magazine articles, dissertations). Title + URL are 100% — those are the load-bearing fields for the seed-record contract; metadata is bonus and can be backfilled by clicking into detail pages later. Records are SEED quality (metadata only); the click-in PDF / full-text fetch via ProQuest's content delivery URLs is the next iteration — Opus warned "ProQuest's TOS prohibits systematic downloading; if PDF flow proves unreliable, treat metadata-only as the deliverable" — that's the current state. Re-running the script is idempotent in effect because the existing JSON files get overwritten with fresh data; if the user wants merge semantics later, the article index is the right place for it.

Open follow-ups (deferred): (a) extend the indexer to ingest ProQuest seed JSON before the click-through fetch runs, so the user can search the new newspaper coverage immediately; (b) write a similar puller for ProQuest US Newsstream (broader US newspaper coverage); (c) Chinese Newspapers Collection (`hnpchinesecollection`) needs a separate query strategy since it indexes Chinese-language papers; English queries return 0 — translation-aware queries are a future enhancement.

[2026-05-02] - FIXED: EBSCO query truncation breaks Boolean syntax mid-clause

Status
RESOLVED. Implemented after the 2026-05-01 medium-yield recovery completed (1139 PDFs on disk). Replaces the open issue documented below.

Solution
1. New ``_balanced_truncate(query, max_chars)`` helper in scripts/normalize_seed_queries.py. Walks the input character-by-character tracking paren depth and quote state. Cut strategy in priority order:
   (a) Latest balanced ``)`` before max_chars (drops a trailing AND-clause cleanly).
   (b) If no balanced ``)``, latest ``AND`` / ``OR`` boundary at depth 0 with balanced quotes — cut BEFORE the operator so the truncated query isn't left with a dangling boolean op.
   (c) Final fallback: walk forward, recording the last position where both depth==0 and !in_quote; cut there.
   Output is guaranteed balanced (or an empty string in pathological cases).
2. Bumped _QUERY_MAX_CHARS from 200 → 500. The legacy 200-char limit was a guess; modern research.ebsco.com URLs accept 400+ chars in practice. 500 gives gpt-oss:20b's verbose multi-OR queries room and the safe-truncate fallback only activates on outliers.
3. System-prompt update: replaced ``Maximum ~200 characters per query`` with explicit ``Hard cap: each query MUST be ≤ 400 characters (preferably ≤ 300). Long queries get safe-truncated and lose their tail clauses — bad for recall. Be concise: prefer truncation operators (e.g. retail*) over long synonym lists.``  Tells the LLM the consequence of overshooting.
4. New TestBalancedTruncate class with 5 regression tests: short-passes-through, drops-trailing-AND-clause-at-balanced-paren, never-leaves-unbalanced-quotes-or-parens-at-multiple-cap-sizes, falls-back-to-AND-OR-boundary-when-no-paren, doesn't-crash-on-pathological-input. Updated the existing ``test_truncates_each_variant_to_200_chars`` to use _QUERY_MAX_CHARS rather than hardcoded 200.

Tests: 234 → 246 (+12), all passing.

Notes
The ~286 PDFs added during medium-yield (2026-05-01 run) and the ~373 from low-yield are on disk and indexed even though they were collected with the old broken truncator. Some are mis-categorised (e.g. medical articles in non-medical gaps from the truncation issue); the SQLite article index makes filtering after the fact straightforward (FTS5 search + cross-check title vs gap_research_question). A future cleanup pass can flag obviously-irrelevant entries — but this is optional, the corpus still has 1139 high-quality PDFs net of any noise. The truncation fix prevents the same issue on any future runs against new gap data, which was the immediate goal.

Open follow-up: post-hoc relevance filter for the existing 7783-article index — flag rows where title shares zero keyword overlap with the gap's bquery_original concepts. Not blocking; mark as low-priority cleanup.

[2026-05-01] - HISTORICAL (now resolved): EBSCO query truncation breaks Boolean syntax mid-clause

Status
RESOLVED 2026-05-02 (see entry above). Original problem description preserved below for context.

Problem
``normalize_seed_queries.py`` truncates each LLM-generated bquery at 200 chars (``line[:200]`` in ``_parse_numbered_list``). The A/B-experiment-winning model gpt-oss:20b commonly produces 250-350 char queries — Boolean expressions with multiple parenthesized OR-groups, year enumerations, and extra synonym layers. Mid-character cuts at 200 leave dangling quotes / unclosed parens. EBSCO receives malformed Boolean, drops the broken tail, and falls back to keyword-soup matching against the surviving tokens. Result: irrelevant articles from Academic Search Ultimate (which is broad enough to include medical / scientific journals) sneak into research gaps where they don't belong.

Concrete example (observed in AUTO-45-G1 — UPS Los Angeles expansion gap):
- Stored variant 1: ``("United Parcel Service" OR UPS) AND (expansion OR growth OR extension) AND ("Los Angeles" OR LA) AN`` (cut mid-word "AND")
- Stored variant 2: ``...AND ("common carrier" OR "carrier service`` (unclosed quote)
- Result: article ``Effects of Spinal Cord Stimulation in Patients with Small Fiber...`` saved to the UPS gap. Spinal-cord paper ranked into top 8 because keyword fallback matched ``expansion`` / generic medical-paper-sized vocabulary against fragments.

Root Cause
The 200-char limit was a guess at "EBSCO's practical limit" with no margin for Boolean-safe truncation. The system prompt asks for ``Maximum ~200 characters per query`` but gpt-oss:20b doesn't strictly respect it (the small qwen2.5:7b did). The truncation site does a naive ``[:200]`` slice, ignoring quote and paren balance.

Solution (planned, NOT YET IMPLEMENTED)
1. Tighten the prompt: replace ``Maximum ~200 characters`` with explicit ``MAX 250 chars per query — be concise, you can always use truncation operators (e.g. retail*) instead of long synonym lists``.
2. Boolean-safe truncation helper: walk backwards from char 200 to the last balanced ``)`` or the last ``AND``/``OR`` boundary; drop everything after. Preserves valid Boolean parse for EBSCO.
3. Regression test: feed a known >250-char input through the truncator, assert the output parses with balanced quotes / parens (or is shorter than the input but ends at a clean boundary).
4. Optional: bump max length to 500 chars and rely on Boolean-safe truncation alone — gives recall headroom for genuinely complex multi-concept queries.

A FIXME comment is in place at scripts/normalize_seed_queries.py around the ``line[:200]`` site pointing at this entry.

Notes
The current run's output is usable but has measurable noise. The new SQLite article index (commit 2fc68ea) makes filtering after the fact straightforward — once DOIs are populated and indexing is rebuilt, articles whose title shares zero overlap with their gap's bquery concepts can be flagged via SQL. That post-hoc filter is a reasonable bridge until the truncation fix lands.

Resurface trigger: before the next full recovery run, OR before re-running normalization on any new pull_output dir, OR when the user reads the article index and sees clearly-irrelevant titles.

[2026-05-01] - Generalized yield-recovery script and medium-yield orchestrator

Problem
The low-yield recovery script (_low_yield_recovery.sh) was hardcoded for the 56 low-yield
gaps and could not be reused for medium-yield (exactly 2 PDFs, ~37 gaps) or future high-yield
passes without copy-pasting the entire script and editing constants.  There was also no
mechanism to automatically start the medium-yield pass once the low-yield run finished.

Root Cause
_low_yield_recovery.sh had the gap list path, run label, log filename, and Telegram ping
strings all hardcoded.  No dispatcher existed to chain phases.

Solution
Option A (generalization via positional args):
1. scripts/_yield_recovery.sh — new generic script accepting <gap_list_file> <run_label>
   [normalize_model].  Runs the same 3-phase pattern (normalize → fetch → re-index with
   --dedupe) for any gap list.  Log file is named logs/<label>_recovery.log.  Telegram
   pings use the label.  Re-indexing (Phase 3) is an added step not present in the old
   script — the article index is updated after every recovery pass.
2. scripts/_low_yield_recovery.sh — replaced with a 4-line thin wrapper that calls
   _yield_recovery.sh with /tmp/low_yield_gaps.txt and label "low_yield".  Existing
   invocations continue to work without change.
3. scripts/_orchestrate_recovery.sh — dispatcher that polls /tmp/low_yield_recovery_pid
   via kill -0 every 60 s, re-indexes after low-yield exits, snapshots medium-yield gaps
   FRESH (re-computed from on-disk PDF counts at that moment), pings Telegram, then runs
   the medium-yield recovery.  High-yield step is present but commented out.
4. /tmp/medium_yield_gaps.txt — written with 37 gap IDs (exactly 2 PDFs, snapshotted
   2026-05-01T11:59:45 — stale by design; the dispatcher re-snapshots at dispatch time).
5. tests/test_yield_recovery_scripts.py — 6 new tests: bash -n syntax checks for all 3
   scripts, exit-code checks for missing-arg / missing-file error paths, banner-label
   assertion using a PATH-stubbed python3.

Notes
The medium-yield snapshot count is 37 (slightly more than the expected ~36 from baseline
analysis — the live low-yield run had already moved some 1-PDF gaps up to 2-PDF by the
time of the snapshot).  The dispatcher always re-snapshots, so this number will differ
again by the time medium-yield actually starts.  This is documented in a comment in
_orchestrate_recovery.sh.

[2026-04-29] - SQLite + FTS5 article index for searchable corpus metadata

Problem
After each fetch run, articles are scattered across thousands of per-gap per-source .md
files with no way to answer corpus-level questions: what sources are represented, how
many PDFs vs metadata-only, which gaps got 0 PDFs, or to search across all article titles
and abstracts.

Root Cause
The pipeline was designed as a producer of files; there was no consumer-side index. Each
fetched article exists only as a standalone markdown file in
data/pull_outputs/<run_id>/<gap_id>/<source>/fetched/. Answering cross-gap queries
required traversing thousands of files on every question.

Solution
Added a SQLite + FTS5 article index (no new pip dependencies — stdlib sqlite3):

1. adapters/article_index.py — core module:
   - open_index(db_path): creates schema on first call; idempotent (all DDL uses
     IF NOT EXISTS). Schema: articles table + articles_fts virtual table (porter
     tokenizer) + INSERT/DELETE/UPDATE triggers + indexes on source_id, gap_id, run_id,
     doi, canonical_id.
   - ingest_pull_output(conn, pull_root, run_id): walks the run directory, parses
     markdown files using _parse_markdown_article(), extracts bquery context from seed
     JSON files, reads gap research question and chapter from data/runs.json via
     gap_context_for(). Idempotent via UNIQUE(run_id, gap_id, source_id, title) — re-runs
     skip existing rows and only insert new articles.
   - dedupe_by_doi(conn, run_id): for rows with non-null DOI, picks one canonical row per
     DOI (prefer has-PDF > source priority > earliest indexed_at), sets canonical_id on
     others. Never deletes rows or files.
   - gap_context_for(run_id, gap_id, runs_json_path): reads data/runs.json and returns
     (claim_text, chapter) for a gap — stored as gap_research_question and gap_topic.

2. scripts/index_articles.py — CLI builder:
   --run-id (required), --db, --pull-root, --gap-id, --rebuild, --dedupe.
   Re-running is idempotent by default.

3. scripts/query_articles.py — convenience query CLI:
   --sources, --gaps, --zero-pdf-gaps, --search <query>, --gap <gap_id>,
   --doi-duplicates, --limit.

Design choice: SQLite + FTS5 (not Elasticsearch, ChromaDB, or Postgres):
  - Zero new pip dependencies (sqlite3 is stdlib).
  - Single file at data/article_index.sqlite (gitignored under data/).
  - Rebuilds in seconds from existing markdown files — no network, no daemon.
  - FTS5 with porter stemming gives full-text search across title/abstract/authors/
    research question with ranking.
  - DOI dedup is a pure SQL UPDATE pass — no file changes on disk.

Notes
DOIs are not present in the existing fetched markdown files. The EBSCO
_write_ebsco_records writer does not capture DOIs from search-results DOM (DOIs are on
detail pages). DOI dedup fires only for future data where DOIs are written into markdown.
Backfilling DOIs from saved HTML is a follow-up task.

Tests added: 206 → 234 (+28) in tests/test_article_index.py covering: markdown parser
(5 tests), ingest correctness and idempotency (8 tests), FTS5 search (4 tests), DOI
dedup (4 tests), source-count query (1 test), gap_context_for (4 tests), zero-PDF gap
detection (2 tests).

[2026-05-01] - Model selection for query normalization: gpt-oss:20b chosen over llama3.1:8b based on A/B experiment

Problem
With multi-variant normalization shipping (--variants 3), the default model (qwen2.5:7b, ~3s/call) was fast but untested for retrieval quality. Open questions: do larger/slower models produce meaningfully better EBSCO queries? Is the extra wall-clock time justified for recovery passes on low-yield gaps?

Root Cause
No empirical comparison existed between the candidate models. Qualitative inspection of qwen2.5:7b output showed functional but minimal queries — single brand-name spelling, year ranges written as literals ("1998-2014") that EBSCO cannot parse, no adjacent-concept vocabulary. There was no data on whether slower models with more capacity actually improved PDF yield enough to matter.

Solution
Ran a controlled A/B experiment on 2026-05-01 against run_27f86e44394442:
- Arm A: 5 low-yield gaps normalized with llama3.1:8b (mean 3.8s/call), then re-fetched.
- Arm B: 5 low-yield gaps normalized with gpt-oss:20b (mean 32.9s/call), then re-fetched.

Results:
  Arm A (llama3.1:8b): +15 PDFs across 9 records — 1.67 PDFs/seed — normalize 15s
  Arm B (gpt-oss:20b): +53 PDFs across 20 records — 2.65 PDFs/seed — normalize 684s

gpt-oss:20b yields +59% more PDFs per seed record than llama3.1:8b. For one-time low-yield recovery passes where wall-clock is not urgent, the quality gain clearly outweighs the 45× normalization time cost.

Key qualitative differences observed:
- llama3.1:8b occasionally outputs prompt scaffolding as literal queries (e.g. the string "DIRECT angle — core terms:" appears as a query variant), destroying one of three variants for that record.
- llama3.1:8b writes year ranges as literals ("1998-2014") that EBSCO cannot parse; gpt-oss:20b enumerates individual years with OR: (1998 OR 1999 OR ... OR 2014).
- gpt-oss:20b enumerates all known brand-name spellings: ("Barnes & Noble" OR "Barnes and Noble" OR "B&N"). llama3.1:8b typically picks one.
- gpt-oss:20b adds genuine adjacent-concept framing that captures strategic intent, not just entity names.

Model timing benchmark (5 gaps × 1 record, 3 variants each, 2026-05-01):
  qwen2.5:7b:       3.1s mean — viable default for online/regular pipeline use
  llama3.1:8b:      3.8s mean — similar speed to qwen2.5:7b but worse output quality
  gpt-oss:20b:     32.9s mean — best output quality; use for recovery passes
  llama3.3:latest: 26.8s mean — similar speed to gpt-oss:20b but lower quality
  qwen3.5:27b:    120.0s mean — ALL calls timed out at default 120s; unusable

Decision: use gpt-oss:20b for low-yield gap recovery passes (--model gpt-oss:20b). Keep qwen2.5:7b as the pipeline default for speed. llama3.1:8b is not recommended — it is slower than qwen2.5:7b and produces structurally broken output. qwen3.5:27b is blocked at default timeout and must not be used without raising ORCH_LLM_TIMEOUT.

Notes
The A/B fetch was a full re-fetch (not incremental): all pre-experiment PDFs remained; new PDFs were additive. AUTO-137-G1 (Arm A) yielded 0 new PDFs — the normalization produced valid queries but EBSCO had no matching articles for this gap regardless of query form. The benchmark report with per-record variant output lives at logs/model_bench_report.md. The experiment script is scripts/_ab_experiment.sh; the bench script is scripts/_bench_query_models.py. The live run (PID 76163 / run_27f86e44394442) was not interrupted during the experiment — the Arm gaps are a separate gap slice.

[2026-04-29] - Multi-variant seed query normalization for broader EBSCO retrieval coverage

Problem
A single Boolean query, even when well-formed, does not capture the full retrieval surface for a research gap. Different vocabulary angles — direct terms, adjacent-concept framing, historical/period-specific phrasing, proper-noun vs generic — surface different articles. The previous single-query normalization left significant recall on the table.

Root Cause
``normalize_seed_queries.py`` (commit 9859b68) was designed as a 1-in → 1-out transformer: one raw bquery in, one normalized Boolean query out. The ``bquery_normalized`` field was stored as a bare string, and ``_classify_record`` produced exactly one FetchItem per seed record. There was no mechanism to issue multiple searches for the same research gap.

Solution
1. Extended ``scripts/normalize_seed_queries.py``:
   - Added ``--variants N`` CLI flag (default 3, max 10). The LLM is prompted to generate N distinct search angles for the same gap, using a new system-prompt template with worked examples showing "direct", "adjacent", and "historical" vocabulary angles.
   - The LLM response is a numbered list (``1. query``, ``2. query``, …). ``_parse_numbered_list`` parses robustly — handles ``1.``, ``1)``, ``1:`` styles, blank lines, trailing whitespace, markdown fences — and deduplicates while preserving order.
   - ``bquery_normalized`` is now stored as ``List[str]`` instead of a bare string. ``_migrate_bquery_normalized`` auto-wraps old string values as ``[str]`` for backward compatibility.
   - Idempotency updated: skips records where ``bquery_normalized`` is already a non-empty list of length >= ``--variants`` (unless ``--force``). Old string-format records with length 1 trigger re-normalization when ``--variants > 1``.

2. Updated ``adapters/document_fetch.py``:
   - ``_classify_record`` now returns ``List[FetchItem]`` instead of ``Optional[FetchItem]``. When ``bquery_normalized`` is a list of N variants, it returns N FetchItems — same gap_id/source_id/out_dir, but each with a distinct spliced URL and a ``variant_index`` (1-based int). Un-normalized seeds and non-seed records return a single-element list (or empty list for no-match).
   - ``FetchItem`` gained a new ``variant_index: int = 0`` field (default 0 = un-normalized; ≥1 = variant number). This is backward compatible — no existing code sets or reads it, and the dataclass default means no callers need updating.
   - ``collect_fetch_items`` updated to iterate the returned list from ``_classify_record``.
   - All variant results land in the same ``<gap_id>/<source>/fetched/`` directory. Article slug collisions (same article found by two variants) are skipped by the existing file-exists check in ``_write_ebsco_records``.

3. Tests (202 → 207, net +5):
   - Updated 4 existing tests in ``test_document_fetch.py`` to match the new list-return shape.
   - Updated existing tests in ``test_normalize_seed_queries.py`` to expect ``bquery_normalized`` as a list.
   - Added: ``test_three_variants_produce_three_fetch_items``, ``test_old_string_bquery_normalized_produces_one_fetch_item``, ``test_dedup_identical_variants_produces_single_item``, ``test_migration_string_to_list_in_process_file``, ``test_variants_flag_sets_list_length``, ``test_idempotent_reruns_when_fewer_variants_than_requested``.

Notes
The ``FetchItem.variant_index`` field is a new contract field — added with a safe default (0) that is backward compatible. The change from ``Optional[FetchItem]`` to ``List[FetchItem]`` for ``_classify_record`` is an internal contract change; no external callers were found outside the test suite. Article deduplication at the filesystem level (slug collision) is intentional: if variant 2 finds the same article as variant 1 already wrote, it is silently skipped, keeping the fetched/ directory clean. Variant temperature raised to 0.3 (from 0.1) to encourage vocabulary variation across variants.

[2026-04-29] - LLM-driven EBSCO seed query normalization (bquery_normalized)

Problem
Raw ``bquery`` strings stored in seed JSON records were generated upstream with no regard for EBSCO's Boolean search syntax. Strings like ``"Amazon + e-commerce revolution archives"`` are interpreted literally — ``+`` is a literal character, multi-word concepts aren't phrase-quoted, synonyms are absent, and there's no Boolean structure. Result: queries are too narrow (implicitly AND-only), miss synonyms, and rich-content articles never match.

Root Cause
The upstream pipeline that generates ``bquery`` values treats the query as a free-text label, not a structured search expression. EBSCO expects Boolean syntax: ``OR``-grouped synonyms in parentheses, ``"quoted phrases"``, ``*`` truncation for stems, and ``AND`` between major concept groups.

Solution
1. Added ``scripts/normalize_seed_queries.py`` — a standalone CLI script that walks ``data/pull_outputs/<run_id>/<gap_id>/<source>/*.json``, reads each record's ``bquery`` field, sends it to an LLM for rewriting using a detailed system prompt with 5 worked examples, and writes the result back as ``bquery_normalized`` alongside the original ``bquery``. The original is also mirrored to ``bquery_original`` for explicit rollback.

2. Added ``_splice_normalized_bquery(url, normalized)`` in ``adapters/document_fetch.py`` — a pure URL transformer that replaces the ``bquery`` query parameter in a ``search.ebscohost.com/login.aspx`` URL with the normalized text, leaving all other parameters intact. No-op for non-login.aspx URLs or URLs with no bquery param.

3. Modified ``_classify_record`` in ``adapters/document_fetch.py`` to call ``_splice_normalized_bquery`` when a record has ``bquery_normalized`` set. This wires the normalization into the existing fetch pipeline: ``_classify_record`` → spliced URL in FetchItem → ``_rewrite_ebsco_url_if_configured`` builds the direct ``research.ebsco.com/c/<opid>/…`` URL using the normalized query.

LLM plumbing reused: ``layers/llm_client.py`` / ``make_llm_client()`` — the same provider-agnostic client (Ollama / Claude / OpenAI) used by the gap-analysis and reflection layers. ORCH_LLM_PROVIDER and ORCH_LLM_MODEL control provider selection; local Ollama is the default (no network cost, faster for batch).

Script CLI flags: ``--run-id`` (required), ``--gap-id``, ``--source`` (default: ebsco_api), ``--limit``, ``--force``, ``--dry-run``, ``--model``, ``--data-root``.

Safety invariants: original ``bquery`` is never overwritten; ``bquery_normalized`` field is additive; idempotent by default (skips records with existing ``bquery_normalized`` unless ``--force``); ``--dry-run`` makes zero writes; script explicitly documented as offline-only (the user runs it manually before fetch runs).

Tests added (183 → 201): 18 new tests in ``tests/test_normalize_seed_queries.py`` covering ``_process_file`` (write, idempotent, force, dry-run, limit, no-bquery, truncation), ``main()`` CLI (missing run dir, dry-run, limit, gap-id filter), and ``_splice_normalized_bquery`` / ``_classify_record`` wiring (replace bquery, no-op for non-EBSCO URLs, end-to-end pipeline through ``_rewrite_ebsco_url_if_configured``).

Notes
The system prompt includes 5 worked examples matching the actual JHU dataset query style. The LLM temperature is set to 0.1 (deterministic / low variance). Responses are truncated to 200 characters to stay within EBSCO's practical query length limit. The wiring change in _classify_record is a 4-line addition; no existing tests required modification. The live run in run_27f86e44394442/ was NOT touched during development — all testing used tmp_path fixtures.

[2026-04-30] - EBSCO multi-profile cookie auto-redirect — pin to named institutional profile via URL rewrite

Problem
After yesterday's successful full-corpus run from the JHU Libraries profile (op-id ``6hfcoc``, databases ``asn,bsu``), today's runs landed on the JHU School of Medicine profile (op-id ``mys74t``, medical databases like CINAHL / MEDLINE) — every legacy ``search.ebscohost.com/login.aspx?direct=true&bquery=...`` URL silently auto-redirected to SOM, and every search returned 0 results because the medical databases don't match e-commerce queries. Even after wiping the Chrome user-data-dir and signing in fresh via JHU Libraries (catalyst.library.jhu.edu), live probes initially returned Libraries (c/6hfcoc/) but degraded back to SOM within minutes — the cookie priority kept shifting.

Root Cause
EBSCO's ``login.aspx`` endpoint is an authentication-arbitration redirect: when multiple institutional sessions are present in the user's cookie jar (Libraries + SOM, both linked through JHU's Shibboleth IDP), EBSCO picks one based on priority signals that aren't stable from request to request. JHU users with access to both profiles see this as an apparently-random per-navigation choice. There's no reliable way to force a specific profile via the legacy URL pattern itself — adding ``&db=asn,bsu`` only triggers EBSCO's webauth callback (``PersistentLink.aspx?...&authtype=promptedcallback``) rather than redirecting to the named profile.

Solution
Added _rewrite_ebsco_url_if_configured(url) in adapters/document_fetch.py. When ``ORCH_EBSCO_OPID`` is set in the environment, legacy login.aspx URLs are rewritten to the modern direct profile URL: ``https://research.ebsco.com/c/<opid>/search/results?q=<urlencoded-bquery>&db=<urlencoded-csv>`` — bypassing the cookie-arbitration layer entirely. The bquery is parsed via urllib.parse.parse_qs (which decodes form-encoded ``+`` as space) and re-encoded via urllib.parse.quote — preserving literal ``+`` characters from the original ``%2B`` encoding (critical for boolean queries like "Amazon + e-commerce" used throughout the JHU dataset).

Configuration:
- ORCH_EBSCO_OPID — the institutional profile ID. Find by signing into EBSCO via the desired portal and inspecting the URL: research.ebsco.com/c/<opid>/.... For JHU Libraries it's ``6hfcoc``; SOM is ``mys74t``.
- ORCH_EBSCO_DB   — comma-separated database codes, default ``asn,bsu``.
- CLI flags --ebsco-opid and --ebsco-db (in scripts/fetch_documents.py) override the env vars per-invocation.

The rewrite is fully no-op when ORCH_EBSCO_OPID is unset (preserves legacy behaviour), when the URL is already a research.ebsco.com path, when bquery is missing, or for non-EBSCO sources entirely.

fetch_seed_page now applies the rewrite to ``item.url`` before passing to ``fetch_with_eval`` / ``fetch``. Stored URLs in the JSON record artifacts are unchanged — only the runtime navigation target gets adjusted.

Validated end-to-end: live probe of "Amazon e-commerce revolution archives" against the rewritten direct URL returned 20 articles from "Johns Hopkins Libraries" (c/6hfcoc/), where the same query through login.aspx returned 0 from "Johns Hopkins School of Medicine" (c/mys74t/). Tests: 177 → 182 (+5 regression tests covering: no-op when unset, URL pass-through for already-direct paths and non-EBSCO URLs, default DB asn,bsu, custom DB via env var, no-op when bquery missing, encoded-plus-survives roundtrip).

Notes
The opid value is institution-specific. JHU is hardcoded in test fixtures but the code is generic — any institution with multiple linked profiles can use this with their own opid. Future enhancement could auto-detect the opid by capturing it from a known-good probe URL once per run, but the env var approach is simpler and the user only sets it once. Not all EBSCO institutional profiles use the same operator-ID format; if a user encounters a longer or differently-shaped opid, the URL is opaque to our rewriter (we just substitute the path segment). The cookie-layer behaviour is also visible at the CLI: a user can detect the wrong profile by running a live probe that shows ``"institution": "..."``  — included in /docs/orchestrator_app.md as a debug procedure.

[2026-04-30] - Worker-level CAPTCHA detection + pool pause for human solve

Problem
The pool's worker threads silently skipped CAPTCHA-blocked articles. The main session pauses on CAPTCHA via on_blocked + input(), but the four worker tabs that drive PDF click-in have their own pages and weren't running iframe detection. Articles where EBSCO triggered a reCAPTCHA / hCaptcha just looked like "no_pdf_link" — the user couldn't intervene with a single click even though that's all it would take. For long unattended runs this was acceptable; for an attended run where the user is at the keyboard and willing to solve challenges as they appear, it was a clear lost-recovery path.

Root Cause
_PdfWorkerPool's worker loop called _download_pdf_with_page_detailed and treated the result as terminal — no second-look at the page DOM, no coordination across workers when one hit a challenge. There was also no way for the user to know which of N tabs needed attention.

Solution
1. Added pause_on_captcha (bool, default False) to _PdfWorkerPool.__init__. When enabled, after a "no_pdf_link" result the worker calls _handle_captcha_if_present(page, record), which probes via _detect_iframe_block (imported lazily from adapters.browser_client to avoid a circular). If a recaptcha / hcaptcha / turnstile iframe is found, the worker brings its tab to front via page.bring_to_front() (best-effort; surfaced tab gives the user a visual signal of which Chrome window to interact with), then acquires _captcha_prompt_lock and clears _free_event so other workers stop pulling tasks.

2. The pool then fires on_state_change("captcha_paused", meta_with_gap_id_title_url_action). This callback is synchronous from the worker thread; the CLI's _make_emit handler — when pause_on_captcha is True — prints a banner, sends a Telegram ping, and BLOCKS on input() until the user presses Enter. When the input() returns, control flows back through the callback, _free_event is set, "captcha_resumed" fires, and ALL workers resume. The original worker then re-attempts the article once on the same page (which is now post-solve), capturing the PDF if available.

3. If a second worker hits a CAPTCHA while the first one is in the prompt lock, _handle_captcha_if_present sees _free_event already cleared and just waits on it — no double-prompt.

4. CLI side (scripts/fetch_documents.py): added a mutually-exclusive --pause-on-captcha / --no-pause-on-captcha flag pair. Default behaviour: ON when interactive (no --no-prompt) and OFF when --no-prompt is set (input() would EOF). If the user explicitly passes --pause-on-captcha alongside --no-prompt, the script warns and disables it (otherwise the worker would block forever on EOF input). Plumbed through run_fetch's new pause_on_captcha kwarg → make_pdf_worker_pool.

5. New emit statuses captcha_paused / captcha_resumed; FetchDocumentsStats counters captcha_pauses / captcha_resumes; CLI summary prints a "CAPTCHA pauses: N (resumed: M)" line when any occurred.

6. EOFError / KeyboardInterrupt during the input() prompt are caught — pool resumes anyway and the article is reported as failed; the run does not get stuck.

Tests added (173 → 175): captcha-pause flow fires both state callbacks and unblocks workers; pool defaults to skip-quietly when pause_on_captcha is False.

Notes
The captcha pause is bidirectional with the throttle pause — both use _free_event but for different reasons. They don't double-pause since both check is_set before clearing. _detect_iframe_block is imported lazily inside _handle_captcha_if_present to avoid a circular when document_fetch is imported from browser_client (which it isn't today, but the lazy import keeps the option open). The pool's bring_to_front() call is best-effort because some Chrome / CDP versions silently ignore it; the banner + Telegram ping + URL-in-message ensure the user can find the right tab even if focus doesn't auto-shift. _handle_captcha_if_present is only invoked after a "no_pdf_link" result — successful PDF fetches and unrelated errors don't run the iframe probe (avoids per-task overhead). For non-CAPTCHA blocks (login wall, rate-limit) the worker's behavior is unchanged: timeouts go through the throttle path, login walls show up as no_pdf_link the same as missing PDFs would.

[2026-04-30] - Throttle detection + pool pause/cooldown + jitter + worker count flag

Problem
The full-corpus run on 2026-04-29 demonstrated a 60-gap "dead zone" (AUTO-040 through AUTO-099) where 0 PDFs were captured across 1714 articles. EBSCO had silently slow-walked detail-page responses past the 30 s page.goto timeout under sustained 4-worker concurrency. The pipeline reported every timed-out article as "no_pdf_link" / pdf_inline_unavailable because the post-timeout DOM lacked the /viewer/pdf/ anchor — indistinguishable from articles that genuinely have no PDF. ~200-250 retrievable PDFs were missed silently. Three needs surfaced:
1. Distinguish "EBSCO timed me out" (likely throttle) from "no PDF for this article" (legitimate).
2. When throttled, pause the pool and back off — instead of silently bleeding articles into the no-PDF bucket.
3. Make the worker count first-class on the CLI so the user can dial it up or down without exporting env vars.

Root Cause
fetch_documents.py / adapters/document_fetch.py treated every page.goto failure (including TimeoutError) as a return None → a one-shot "no_pdf_link" emission. There was no shared state across workers to count consecutive timeouts, no pause mechanism, no inter-request jitter, and no CLI knob for workers (only an env var).

Solution
1. Refactored _download_pdf_with_page into _download_pdf_with_page_detailed which returns (Optional[Path], Optional[str]) where the reason string is one of: None (success) | "no_pdf_link" | "navigation_timeout" | "navigation_error" | "viewer_capture_failed" | "write_failed". TimeoutError is detected via _is_playwright_timeout(exc) which does an isinstance check against playwright.sync_api.TimeoutError with a class-name fallback. Both the detail-page nav and viewer-page nav are tracked separately — viewer timeouts on empty capture are reported as navigation_timeout, viewer non-timeouts on empty capture as viewer_capture_failed. _download_pdf_with_page is preserved as a backward-compat wrapper that drops the reason.
2. _PdfWorkerPool now tracks consecutive_throttles + total_pauses under a lock. Each worker's result feeds _update_throttle_state(reason) — only navigation_timeout increments the counter; None and no_pdf_link reset it; ambiguous reasons (viewer_capture_failed, write_failed) leave it alone. When the counter hits throttle_threshold (default 3), _initiate_pause_locked clears the free_event and spawns a cooldown thread; workers block on the event before pulling each task. After cooldown_base_sec * total_pauses (linear backoff: 5min, 10min, 15min...) the cooldown thread sets the event and workers resume. After max_pauses (default 3) the pool sets _exhausted and drains remaining tasks with reason throttle_exhausted.
3. Per-task jitter: each worker sleeps random.uniform(0, jitter_ms) ms before processing each task (default 800 ms cap). Spreads same-instant request bursts across workers.
4. State callback: _PdfWorkerPool fires on_state_change("throttle_paused" | "throttle_resumed" | "throttle_exhausted", meta_dict) on each transition. run_fetch supplies _on_pool_state_change which routes through the same emit pipeline as article events; the CLI's _make_emit pings Telegram for these three statuses (per AGENTS.md §15) so the user knows about pauses even when running unattended.
5. _emit_pdf_outcome now uses the typed reason directly: None+path → pdf_inline_ok; no_pdf_link → pdf_inline_unavailable; navigation_timeout / throttle_exhausted → pdf_inline_throttled (new status); everything else → pdf_inline_failed.
6. FetchDocumentsStats added inline_pdfs_throttled, throttle_pauses, throttle_resumes, throttle_exhausted; the run_fetch emit wrapper tallies all five. CLI summary now prints "Article PDFs saved: N/M (X no PDF, Y throttled, Z failed)" and a "Throttle events:" line when any pause occurred.
7. CLI flags: --workers N (overrides ORCH_PDF_WORKERS), --throttle-cooldown SEC, --throttle-threshold N, --max-throttle-pauses N, --jitter-ms MS. Each flag overrides its env var so the user can tune per-invocation. make_pdf_worker_pool reads each as either explicit arg, env var, or class default in that order.
8. Sequential fallback (mock / non-CDP / workers=1) now also uses _download_pdf_with_page_detailed so emit reasons are consistent across all three dispatch paths (pool, per-task ThreadPoolExecutor, sequential).

Tests added (170 → 173): pool throttle threshold trigger fires on_state_change with correct meta; no_pdf_link resets the counter (legitimate stretches don't trigger spurious pauses); after max_pauses, next threshold-cross emits throttle_exhausted instead of throttle_paused.

Notes
The cooldown is fixed-linear (1×, 2×, 3×) rather than exponential because EBSCO's apparent throttle window is ~30 min — exponential would overshoot. The pool's drain timeout was bumped from 300 s to 1800 s so a paused pool isn't mistaken for a stuck pool by the worker_pool_timeout fallback. Workers in the pool DO honor the pause but still don't honor the CAPTCHA on_blocked path — a future enhancement would have workers run iframe detection and surface a pdf_inline_blocked event so the main thread can prompt the user to solve a challenge. Telegram pings on throttle events are best-effort (silent if creds missing) and route through _try_telegram which already had silent-fallback behavior. The `download_article_pdf` function is preserved unchanged for backward compatibility with API endpoint callers; internal pipeline paths now use _download_pdf_with_page_detailed directly.

[2026-04-29] - Persistent worker pool for PDF fetch (eliminates per-task setup overhead)

Problem
The first parallelism implementation used ThreadPoolExecutor where each task spawned its own sync_playwright session. Per-task setup (sync_playwright + connect_over_cdp + new_page + close + exit) cost ~1-2 s, comparable to the article work itself (~3-4 s). With 4 workers handling 8 articles per page sequentially in pairs, the measured speedup was only 2× over sequential — not the 4× the worker count suggested.

Root Cause
Playwright's sync_playwright is a per-thread context manager: each entry spawns a Node.js subprocess to host the browser-control bridge. With per-task sessions, every article paid that startup cost. The work-to-overhead ratio at 8 articles per worker over many search-results pages is dominated by setup if sessions don't persist.

Solution
Added _PdfWorkerPool: N persistent worker threads, each entering its own sync_playwright once at startup, opening one persistent page in the shared CDP context, then consuming (record, out_dir) tasks from a shared queue for the lifetime of the pool. Pages stay alive across many articles — the per-task overhead reduces to a queue.put + queue.get, single-digit microseconds. Workers push results to a result queue; the caller drains in arrival order. Errors during worker init (Playwright import, CDP connect failure) are propagated by draining outstanding tasks with a typed error string so the caller never hangs. A make_pdf_worker_pool(cdp_url, workers) context manager handles construction; returns None when CDP isn't available or workers <=1 so callers branch cleanly.

Plumbing: fetch_seed_page accepts an optional pdf_pool arg (default None for backward compat). _try_pdf_fetch_per_article checks for the pool first — if present, submits all article tasks and drains; else falls back to the per-task ThreadPoolExecutor (preserved for direct library callers, e.g. the FastAPI endpoint), or to sequential single-page (mocks, non-CDP sessions). run_fetch creates one pool at start when CDP is available, scoped to the seeds loop, exits the pool before the bulk-PDF loop (which doesn't need it).

Validated end-to-end
- AUTO-109-G1 (8 articles, 0 PDFs available, all just detail-page checks): 13 s. Down from 36 s sequential / 18 s per-task threading.
- AUTO-11-G1 (8 articles, 2 PDFs captured: 4.9 MB and 220 KB, real v1.6/v1.7 PDFs): 17 s. Down from 36 s sequential.

Full-corpus projection: ~2.7 hours (650 search-results pages × ~15 s avg) vs 6.5 hr sequential or 3.25 hr per-task. Tests: 168 → 170 (+2 covering pool helper return semantics and the pool-vs-fallback dispatch in _try_pdf_fetch_per_article).

Notes
The per-task ThreadPoolExecutor path is preserved as a fallback because the FastAPI endpoint and direct library users may not manage a pool. Workers still don't honor on_blocked — a CAPTCHA on a worker's detail page returns null from the viewer-link probe and the article gets reported as unavailable. A future enhancement: have workers detect iframe-CAPTCHAs and surface a pdf_inline_blocked event that the main thread can act on (e.g. drain remaining tasks, prompt user, retry queue). Pool teardown joins worker threads with a 15 s timeout — if a worker's sync_playwright fails to exit cleanly, the join is bounded so the run finishes.

[2026-04-29] - Parallel PDF fetch (4-way concurrency via thread workers)

Problem
The Phase-1 sequential PDF fetch took ~4-5 s per article. For the full corpus (650 search-results pages × ~8 articles ≈ 5200 articles), that projected to roughly 6-7 hours wall-clock — too slow for production runs.

Root Cause
fetch_seed_page invoked download_article_pdf in a serial Python for-loop, using the single ``_PersistentPageSession._page`` for all detail-page navigations. Even though network I/O was the dominant cost, only one in-flight Playwright operation was possible per run.

Solution
1. Refactored the page-level PDF logic into ``_download_pdf_with_page(page, record, out_dir)`` so it can be called by both the sequential session-based path AND by independent worker threads.
2. Added ``_fetch_pdf_in_worker(record, cdp_url, out_dir)``: each invocation creates its own ``sync_playwright`` instance (Playwright sync state is per-thread-not-shareable), connects to the same CDP browser, opens a transient page, runs the PDF capture, and tears down. Multiple workers run truly concurrently because each owns independent Playwright state but they share the browser's cookie jar (same authenticated session).
3. ``_try_pdf_fetch_per_article`` now reads ``ORCH_PDF_WORKERS`` (default 4); when >1 AND the session is a real CDP session (cdp_url is a string starting with http) AND has a ``_page``, it dispatches via ``ThreadPoolExecutor``. Otherwise (workers=1, non-CDP session, mocks), falls back to the original sequential single-page path.
4. Result emission moved to a shared helper ``_emit_pdf_outcome`` so both code paths produce identical events. ``as_completed`` is used to emit events as workers finish, not in submission order — output streams in real time.
5. ``isinstance(cdp_url, str) and cdp_url.startswith("http")`` guards against MagicMock attributes that auto-resolve in tests; without this, mock-based tests would hit the parallel path and report cdp_connect_failed.

Notes
The mock-detection guard is critical: with a plain MagicMock for cdp_url, ``can_parallelize`` would evaluate True and the worker would try to connect to a fake URL, polluting test results. Strict isinstance(str) + startswith("http") filters this cleanly. Workers do NOT currently honor on_blocked — if a CAPTCHA appears on a detail page in worker mode, the page evaluator returns null (no /viewer/pdf/ link visible), and the article gets reported as ``pdf_inline_unavailable``. Future enhancement: add iframe-block detection inside ``_download_pdf_with_page`` and emit a distinct ``pdf_inline_blocked`` event so blocked-but-recoverable articles can be retried in a follow-up pass. Tab pollution: each non-``--no-prompt`` CLI invocation opens an EBSCO login tab that persists across runs; over many runs these accumulate.

[2026-04-29] - Per-article click-in PDF fetch (EBSCO research.ebsco.com)

Problem
The post-run document fetch pipeline only extracted search-results metadata (title, authors, journal, abstract excerpt, EBSCO URL) into markdown files — it never downloaded the actual full-text PDFs that EBSCO offers behind a one-click "PDF Full Text" link on each article's detail page. Users running the pipeline got 300+ markdown files of metadata but zero PDFs, and had to manually click through each EBSCO URL to get the full text.

Root Cause
fetch_seed_page navigated only the search-results listing page, evaluated the EBSCO JS extractor on that page, and wrote one .md per article record. It never visited each article's detail page, where the PDF viewer link lives. The library had a download_pdf() function but it only fired for items with pdf_url already set in the original JSON — no upstream pipeline populated pdf_url, so download_pdf was effectively dead code for EBSCO.

Solution
1. Added download_article_pdf(record, session, out_dir) to adapters/document_fetch.py. Uses the persistent CDP session's _page to (a) navigate the article's detail URL, (b) page.evaluate a small JS expression for `a[href*="/viewer/pdf/"]` to find the viewer link, (c) if absent, return None (no PDF available — common for older trade publications); (d) if present, attach a `page.on("response", ...)` listener and navigate to the viewer URL — the viewer triggers a GET to content.ebscohost.com that returns Content-Type: application/pdf, captured by the listener and written as <slug>.pdf alongside the existing <slug>.md.
2. Added _try_pdf_fetch_per_article(records, session, out_dir) that loops over the extracted records and emits one structured event per article: pdf_inline_ok (saved), pdf_inline_unavailable (no viewer link), or pdf_inline_failed (link found but capture errored).
3. fetch_seed_page now calls _try_pdf_fetch_per_article after _write_ebsco_records for EBSCO sources only (JSTOR/MUSE selectors not yet identified).
4. Extended FetchDocumentsStats with inline_pdfs_attempted / inline_pdfs_ok / inline_pdfs_unavailable / inline_pdfs_failed. run_fetch wraps the user-supplied emit callable with a tally function so these events flow into stats automatically without changing fetch_seed_page's return contract.
5. CLI summary in scripts/fetch_documents.py now displays "Article PDFs saved: ok/attempted (unavailable, failed)" when any inline PDF activity occurred.

Validated end-to-end on AUTO-107-G1 (Palm computers gap, fresh data): 8/8 articles extracted as metadata, 2/8 PDFs captured (Epidemic_of_Palm_Syncing_Problems.pdf 195 KB / 2 pages, Handspring_and_Palm_HotSync_Sunk.pdf 244 KB / 1 page) — both real PDFs, version 1.2, openable. The other 6 articles had no PDF available (older trade publication shorts where EBSCO has metadata only). 36 s end-to-end (~4.5 s per article including the 6 without PDFs that only navigate the detail page once). Tests: 163 → 168 (+5 covering the no-CDP fallback, no-viewer-link path, idempotent re-fetch, multi-record event emission, and run_fetch stats tallying).

Notes
Sequential fetch is ~4-5 s per article (worst case 8 s when the viewer URL responds slowly). For the full corpus of 650 EBSCO seed URLs × 8 articles = ~5200 article click-ins, that's ~6-7 hours sequentially — too slow for production. A future enhancement is multi-tab parallelism via ThreadPoolExecutor where each worker maintains its own sync_playwright session against the same CDP browser; with 4 workers, runtime drops to ~1.5 hours. download_article_pdf currently treats null capture as "unavailable" rather than "failed" — both produce the same on-disk outcome (no PDF) so the distinction is mostly cosmetic, but future work could instrument the listener to distinguish a missing-link skip from a capture timeout.

[2026-04-29] - Browser thrash, iframe-CAPTCHA blind spot, undifferentiated retry, and fixed-wait races

Problem
After the initial pause-on-blocked feature shipped, real-world testing exposed four follow-on issues that, together, made the pipeline visibly broken: (1) every fetch_with_eval call entered its own sync_playwright block and created a new tab via ctx.new_page(), which on Chrome+CDP repeatedly stole OS focus from whatever the user was doing — a 20-page run produced 20 focus events; (2) reCAPTCHA / hCaptcha / Cloudflare Turnstile widgets live inside cross-origin iframes whose visible "I'm not a robot" text never appears in the parent page HTML, so the existing _BLOCK_SIGNALS text regex never fired and the pipeline skipped CAPTCHA pages without pausing; (3) on_blocked ran the same "human pause + retry" path for every blocked reason, including rate_limit (where humans cannot help) and access_denied (where retrying never works); (4) fetch_with_eval used a fixed page.wait_for_timeout(2500) which raced the EBSCO SPA — pages that rendered slowly returned 0 articles, contributing to a 15% miss rate in --limit 20 testing.

Root Cause
1. focus thrash: Playwright's sync API has no clean "create background tab" option; ctx.new_page() opens a focused tab. Architecturally, each fetch was creating + closing its own session/page so there was no opportunity to amortize the focus cost.
2. iframe blind spot: _detect_blocked() only inspected content[:8000] of the parent-page bytes. CAPTCHA widget DOM is cross-origin (Google/Cloudflare-hosted iframes); the parent page's bytes contain only a placeholder div.
3. retry mismatch: blocked_reason was already typed (captcha / login / rate_limit / access_denied) but the on_blocked handler dispatched all reasons through the same input() pause. Rate-limited backoff and skip-without-pause for access_denied were possible but unimplemented.
4. fixed wait: page.wait_for_timeout(2500) is uniform regardless of how fast or slow the page actually renders — wastes time on fast pages and times out on slow ones.

Solution
1. Single-tab session. Added BrowserClient.session() context manager that opens ONE persistent page and yields a _PersistentPageSession proxy whose fetch / fetch_with_eval reuse that page across the entire run. run_fetch wraps the seeds + pdfs loops in `with browser_client.session() as bc:`. Result: one focus event at session start (the user's previous tab is then immediately brought back to front via bring_to_front), zero thereafter. For non-CDP providers session() is a passthrough yielding self.
2. Iframe detection. Added _detect_iframe_block(page) helper that runs a small DOM probe checking iframe srcs (recaptcha / hcaptcha / challenges.cloudflare.com / turnstile / generic captcha) and JS challenge-API globals (window.grecaptcha / hcaptcha / turnstile). Called after parent-page text detection in all three call sites (_PersistentPageSession.fetch, .fetch_with_eval, and the legacy BrowserClient.fetch_with_eval). Best-effort: returns (False, "", "") on any error so it never breaks a successful fetch. Maps every detected family to reason="captcha" with a precise action_required string.
3. Differentiated retry policy. Rewrote _make_on_blocked() in scripts/fetch_documents.py to dispatch by reason: rate_limit → time.sleep(60) + return True (no human prompt; servers need time, not clicks); access_denied → print a one-line skip notice + return False (auth/subscription is not retry-able); captcha / login / unknown → existing banner + Telegram + input() + return True. Constants for the rate-limit backoff are top-level so they're easy to tune.
4. Anchor-based waits. Added wait_for parameter to BrowserClient.fetch_with_eval (and the session variant). When provided, page.wait_for_selector(wait_for, timeout=wait_ms) replaces the fixed sleep — typically returning in 500-1500 ms when content has rendered, with a 5000 ms hard cap before falling through. document_fetch.fetch_seed_page passes per-source result-list anchors from a new _WAIT_SELECTORS map (EBSCO: article[data-auto="search-result-item"] + legacy fallbacks; JSTOR: li.result; MUSE: .search-result, .result-item, article).

Validated end-to-end: --limit 5 against run_27f86e44394442 (post anchor-wait fix only): 40/40 articles in 26 s. --limit 20 (post anchor-wait fix): 160/160 articles (100%, was 136/160 = 85% before). Test suite: 162 → 163 (+1 iframe test), all passing.

Notes
The pause flow only retries ONCE per blocked URL — if the user's solve doesn't unblock the page (or they Ctrl-C out of input()), we save _blocked.html and move on. This avoids infinite loops at the cost of occasionally needing a second pass for stubborn pages. wait_for_selector falls through (does not raise) when the anchor never appears, so genuinely empty result pages and soft-blocked pages still get evaluated and the response is captured for inspection. The session() refactor is interface-compatible with the existing FastAPI run_fetch caller — _PersistentPageSession exposes the same fetch / fetch_with_eval / open_tabs / is_available methods as BrowserClient. A future enhancement would be page.wait_for_selector(captcha_iframe, state="detached") to wait IN-PLACE for the user to solve, avoiding the page.goto re-fetch on retry.

[2026-04-29] - Pipeline skipped CAPTCHA / login-wall pages instead of pausing for the user

Problem
During CLI fetch runs, when a provider page returned a CAPTCHA, "I'm not a robot" challenge, Cloudflare interstitial, login wall, or rate-limit notice, the pipeline emitted a "fetching/blocked" event but immediately moved on to the next URL. The user — sitting in front of the CDP Chrome window — had no chance to solve the challenge before the next navigation overwrote the page. Articles that could have been recovered with one human click were silently lost. This was observed in real runs: out of 160 expected articles at --limit 20, ~24 (15%) returned 0 extractions, some attributable to undetected CAPTCHA states.

Root Cause
Two issues:
1. Detection coverage. _BLOCK_SIGNALS in adapters/browser_client.py only matched a handful of phrases (access denied, captcha, please log in, authentication required, session expired, institutional access, not authorized). It missed the visible widget text "I'm not a robot" / "I am not a robot", Cloudflare's "Just a moment / Checking your browser" interstitial, rate-limit messages ("Too many requests", "Quota limit exceeded"), and explicit "you have been blocked" notices.
2. No retry path. fetch_seed_page detected blocked pages (when the regex matched) but only logged + saved _blocked.html and returned 0 — no callback, no pause, no retry. There was no way for the CLI to insert a human-in-the-loop step.

Solution
1. Expanded _BLOCK_SIGNALS in adapters/browser_client.py to include reCAPTCHA widget phrasing ("I'm not a robot", "I am not a robot", "verify you are human", "verify your humanity"), Cloudflare interstitials ("checking your browser", "just a moment"), rate-limit / quota wording ("too many requests", "rate limit", "quota limit exceeded", "quota violation"), and explicit-block notices ("you have been blocked", "your access has been blocked"). Each maps to a typed reason ("captcha" / "rate_limit" / "access_denied") with a useful action_required string.
2. Added an on_blocked: Optional[Callable] = None parameter to both fetch_seed_page() and run_fetch() in adapters/document_fetch.py. When a page is blocked, fetch_seed_page now calls on_blocked(item, page_result); if the handler returns True, the URL is re-fetched once. On retry success it emits a "fetching/unblocked" event. If still blocked (or no handler given), behavior is unchanged: save _blocked.html, return 0.
3. Wired up _make_on_blocked() in scripts/fetch_documents.py: when running with prompts enabled, it prints a clear "PAUSED — page blocked" banner with gap_id, source_id, URL, and action hint; sends a best-effort Telegram ping (per AGENTS.md §15, silent on failure if credentials absent); calls input("Press Enter once unblocked..."); then returns True so the library retries. With --no-prompt the on_blocked handler is None (preserves the existing skip-and-continue behavior for scripted use). Added 4 new tests in tests/test_document_fetch.py covering retry-on-True, skip-on-False, the new CAPTCHA phrases, and rate-limit detection. Total tests: 158 → 162, all passing.

Notes
The pause is per-blocked-page (one input() per blocked URL) — if many pages are blocked, the user sees one prompt per page rather than a global "everything stopped" prompt. The library still retries the URL only ONCE after unblock; if the page remains blocked after the user's intervention, the pipeline saves _blocked.html and moves on (avoids infinite loops). Telegram delivery is intentionally silent on failure so missing credentials never break the pause flow itself.

[2026-04-29] - EBSCO selectors stale after research.ebsco.com SPA migration

Problem
scripts/fetch_documents.py reported "Seed pages fetched: N / N (0 failed)" for every EBSCO query, yet "Articles extracted: 0" — silently producing no document records. Validation against run_27f86e44394442 (650 EBSCO seed URLs across 169 gaps) yielded zero markdown files even with a fully authenticated CDP browser session.

Root Cause
EBSCOhost migrated its post-login UI from search.ebscohost.com (legacy DOM with .result-list-item, [data-auto="record"], etc.) to a Next.js SPA at research.ebsco.com that uses CSS-module class names and a renamed data-auto-* attribute scheme (article[data-auto="search-result-item"], [data-auto="result-item-title__link"], [data-auto="result-item-metadata-content--contributors"], [data-auto="abstract-content"], [data-auto="result-item-metadata-content--published"], [data-auto="result-item-metadata-content--database"]). The _EBSCO_JS extractor in adapters/document_fetch.py still queried the legacy selectors only, so it walked an empty NodeList on every page. Page navigation and HTML save still succeeded, masking the failure.

Solution
Updated _EBSCO_JS in adapters/document_fetch.py: container query now matches the new article[data-auto="search-result-item"] first (legacy selectors retained as fallbacks for older skins). Per-field selectors prepend the new data-auto-* attributes ahead of legacy selectors. Added a new "database" field (Academic Search Ultimate, etc.) and an absolute "url" field built from the title link's href via new URL(href, location.origin) so downstream consumers get clickable links instead of relative SPA paths. Updated _write_ebsco_records to emit the new Database and URL lines in the saved markdown when present. Validated end-to-end: --limit 5 → 40 articles extracted (8/page); --limit 20 → 136 articles extracted (~85% rate). All 158 existing tests still pass.

Notes
About 15% of pages in the --limit 20 run returned 0 articles for back-to-back queries against the same source — appears to be a transient SPA render race, not a wait-time issue (a fresh manual probe of the same query returned 20 articles within 1500 ms). Re-running the script picks up the gaps because empty extractions don't write a slug.md, only search_results.html (which is skipped on subsequent runs by _save_html). A future enhancement could add a one-shot retry inside fetch_seed_page when eval_result is empty and not blocked. JSTOR and Project MUSE selectors in the same file have not been verified against their current live DOM and may need a similar pass.

[2026-04-29] - CLI auto-launches Chrome with CDP

Problem
scripts/fetch_documents.py printed a _CHROME_HELP block and blocked on input("Press Enter once Chrome is running...") when CDP was unreachable. Users had to manually copy the launch command into another terminal, which was error-prone and prevented scripted/non-interactive use even with --no-prompt.

Root Cause
The script had no mechanism to spawn Chrome itself. It could only detect whether Chrome was already running and print guidance if not.

Solution
Added _launch_chrome(port) — a new function in scripts/fetch_documents.py (~40 lines) that resolves the Chrome executable (macOS app bundle path first, then google-chrome/chromium on PATH via shutil.which), spawns Chrome with subprocess.Popen(start_new_session=True) pointing at a dedicated ~/.research_henchman_chrome user-data-dir, and returns the PID. Added _cdp_poll_until_ready(cdp_url) that polls /json/version every 0.5 s for up to 15 s using the same urllib + Host-header pattern as BrowserClient._playwright_cdp_ping(). The main() sign-in gate now auto-launches and polls by default; passing --no-launch restores the previous print-help-and-wait behavior. Added 2 new tests in tests/test_fetch_cli.py: one asserts Popen is never called with --no-launch; the other asserts Popen is called with --remote-debugging-port= and --user-data-dir= in default mode.

Notes
The ~/.research_henchman_chrome profile is dedicated and separate from the user's normal Chrome profile — no tab collisions. Library logins persist across CLI runs in that profile. If no Chrome executable is found, the script prints a clear error and exits non-zero. The --no-launch flag preserves full backwards compatibility for scripted or externally-managed environments.

[2026-04-29] - CLI Refactor: fetch_documents.py Rewritten as Thin Wrapper over document_fetch Library

Problem
scripts/fetch_documents.py duplicated virtually all logic already present in adapters/document_fetch.py: record classification, abstract saving, seed-page extraction (including the JS extractors for EBSCO/JSTOR/MUSE), PDF downloading, CDP ping, and tab-opening. This created two out-of-sync implementations where a bug fix or provider-DOM change would need to be applied in two places.

Root Cause
The CLI script was written before adapters/document_fetch.py existed as a standalone library. When the library was extracted for API use, the script was left intact rather than refactored to delegate, creating the duplication.

Solution
Rewrote scripts/fetch_documents.py as a thin CLI wrapper (~200 lines vs ~610 before). All fetch logic is now delegated to library functions: collect_fetch_items() for item collection, run_fetch() for the full fetch orchestration (seed extraction, PDF download, abstract saving), and make_browser_client(settings) for browser construction — exactly mirroring how main.py uses these functions. The script retains: run resolution (--run-id flag, API fallback, disk fallback), Chrome launch guidance, the interactive login-prompt gate (with new --no-prompt flag to skip all input() calls for scripted/non-interactive use), and a structured emit() callback that prints [stage/status] message lines. All existing flags (--run-id, --gap-id, --limit, --dry-run, --cdp-url) are preserved with identical semantics. Added 6 new tests in tests/test_fetch_cli.py covering --dry-run, --no-prompt, emit formatting, and port parsing.

Notes
The old script used plain dicts for fetch items; the library uses FetchItem dataclasses. The CLI now accesses fields as attributes (item.fetch_type, item.gap_id) rather than dict keys. No library files were changed.

[2026-04-29] - Post-Run Document Fetch: Full Article Retrieval via API and CDP Browser

Problem
Pipeline runs produced seed-only results for browser-backed sources (JSTOR, EBSCO, ProQuest, Gale). Users could see search-URL placeholders but had no in-app way to fetch the actual article content. The standalone CLI script (fetch_documents.py) used input() prompts which required a real terminal and could not be triggered from the web UI, creating a permissions/access blockage.

Root Cause
Document fetching required interactive terminal prompts (input() calls) for the Chrome CDP sign-in gate and the "press enter when done" confirmation. BrowserClient had no method to run JS expressions on a navigated page (needed for EBSCO/JSTOR DOM extraction). The sign-in infrastructure already existed in the web UI but was not wired to a post-run fetch action.

Solution
1. Added fetch_with_eval(url, js_expr, wait_ms) to BrowserClient — connects via CDP, navigates, waits for JS rendering, runs page.evaluate(js_expr), returns (PageResult, eval_result). Enables EBSCO, JSTOR, and Project MUSE DOM extraction through the existing authenticated browser session.
2. New adapters/document_fetch.py library — no input() calls, uses BrowserClient. Provides: collect_fetch_items(), preview_counts(), save_abstract(), fetch_seed_page() with per-provider JS extractors (EBSCO, JSTOR, Muse, generic HTML fallback), download_pdf() with direct HTTP then CDP fallback, run_fetch() orchestrator with structured emit events.
3. FetchDocumentsResult dataclass added to contracts.py; fetch_status and fetch_result fields added to RunRecord (backward-compatible defaults).
4. GET /api/orchestrator/runs/{run_id}/fetch_items — returns seed/pdf/abstract counts and CDP availability for UI preview.
5. POST /api/orchestrator/runs/{run_id}/fetch_documents — triggers background fetch task, emits progress events to the run's existing event stream, persists fetch_status/fetch_result on run record.
6. "Fetch Documents" panel added to static/index.html — appears after run completion (complete/partial), with Preview Items, Sign In to Databases (reuses existing /signin/open endpoint), and Fetch Documents buttons. Progress shown via existing live log. Polls fetch_status for completion summary.
7. 23 new regression tests in tests/test_document_fetch.py; full suite: 150 passed.

Notes
Blocked pages (CAPTCHA/login walls) are detected, emitted as fetching/blocked events with action hints, and saved as _blocked.html for manual inspection. Existing fetch_documents.py CLI script preserved for headless/scripted use. fetch_with_eval() falls back gracefully on HTTP provider (returns None eval result).

[2026-04-25] - Layer 6: Chart Generation from Data Pull Artifacts

Problem
Raw pull outputs were JSON files only. No visual representation of data; user had no way to quickly understand what BLS, FRED, World Bank, or EBSCO pulls actually returned.

Root Cause
Pipeline had no rendering stage. All data artifacts stayed as machine-readable JSON in pull_outputs/.

Solution
Added Layer 6 render stage: `layers/render.py`, `RenderResult` contract, `RunStatus.RENDERING`, `ORCH_AUTO_RENDER_CHARTS` setting. Pipeline runs render after fit, before export. Charts are written as PNG into the same source directory as JSON artifacts so artifact_export.py picks them up automatically.

Chart types per source:
- bls: line chart of time-series CPI/economic data points (year+period → value)
- fred: horizontal timeline bar chart showing observation span per series, colored by popularity
- ebsco_api/ebscohost: stacked bar chart of publication year distribution by quality label (high/medium/seed)
- world_bank: horizontal bar chart of indicator count by topic
- bea/census/ilostat/oecd: skipped (return metadata only, no plottable values)

Gap _README.md files now embed chart images via markdown `![caption](documents/<source>/chart_*.png)` via new `_chart_section()` in artifact_export.py.

Notes
12 new tests in tests/test_render.py cover all source types, empty data, unresolvable gaps, and skipped sources. matplotlib Agg backend used for headless rendering (no display required).

[2026-04-25] - Gap Analysis: Batched Paragraph-Level LLM Analysis Replaces Single Manuscript Call

Problem
Gap analysis found only 10 gaps max on a 15-chapter manuscript. The per-section LLM call hit context limits and returned a thin sample.

Root Cause
Analysis made one LLM call per manuscript section, each containing many paragraphs. Context limits (~8K tokens) caused the model to truncate or skip most content. The model also received vague "Consider its position in the argument" framing that allowed rationalizing away gaps.

Solution (per Opus architecture review)
1. `_annotate_paragraphs()`: Splits full manuscript on blank lines, annotates each block with its current chapter heading. Preserves paragraph boundaries and chapter attribution.
2. `_score_paragraph()`: Heuristic 0-100 suspicion score using regex signals — causal language, statistics, superlatives, hedges, and explicit markers. Citations reduce but don't eliminate score.
3. `_MAX_LLM_PARAGRAPHS = 40`: Heuristic pre-filter selects top 40 most suspicious paragraphs, reducing LLM call count.
4. `_build_batch_prompt()`: Batches 4 paragraphs per LLM call. Context = thesis sentence + chapter title + preceding paragraph only. Adversarial fact-checker framing: enumerate ALL assertions, mechanical citation test, anti-rationalization default. Requires `paragraph_index` field (1-4) in each output row.
5. `_analyze_with_ollama()` Stage 3 rewrite: Groups top_sorted paragraphs by chapter → chunks into batches of 4 → prev_para lookup uses `{id(p): i}` position map (not `text.index()` which breaks on duplicate text) → routes response rows back to source paragraph via `paragraph_index`.
6. Dropped per-chapter role summarization (15 extra LLM calls that hurt recall by providing narrative framing excuses).
7. Expected: ~11 total LLM calls × 30s = 330s for a 15-chapter manuscript.

Notes
`original_indices = {id(p): i for i, p in enumerate(annotated)}` built in Stage 2 and reused in Stage 3 for prev_para and top_sorted document-order restoration. Text-match lookup via `annotated_texts.index()` was replaced because duplicate paragraph text would return the wrong position.

Opus architecture review recommendations (documented):
- Keep qwen2.5:7b — gap detection is lexical/structural, not reasoning-heavy; 32B too slow for pipeline budget
- Drop chapter role summaries — small models use chapter framing as excuse to skip claims
- Batch 4 paragraphs per call — reduces 80 calls to 10, fits 40 paragraphs in one context window pass
- Adversarial fact-checker prompt — forces enumeration, mechanical citation test, anti-rationalization

[2026-04-24] - Test False Positive: Empty Per-Source Pull Directory Treated as Failure
Problem
`scripts/test_run.py` reported FAIL with "NO FILES in .../oecd" and "NO FILES in .../ilostat" even though the pipeline ran correctly. OECD and ILOSTAT legitimately return no results for e-commerce history queries — they are the wrong domain for that manuscript.
Root Cause
`check_pull_artifacts` added a warning for every source run-directory that contained zero files, and the caller added all warnings to the failures list. A per-source empty result is not a system failure — it means the source had no relevant content.
Solution
Separated the return value of `check_pull_artifacts` into `hard_failures` (total zero artifacts across all sources — a real system failure) and `soft_notes` (per-source empty directories — informational only). Main function now only adds hard failures to the test failures list; soft notes are printed with "(note)" prefix.
Notes
A full-zero artifact run is still a hard failure. Per-source empty is expected and fine.

[2026-04-24] - EBSCO EIT REST API and Playwright CDP Adapter Implemented
Problem
EBSCO pulls only returned seed click-through links. Two new retrieval paths (EIT REST API and Playwright CDP) were needed to fetch real article records.
Root Cause
EIT API requires a 3-part profile string `<account_id>.<group>.<profile_id>` — the library only provided `eitws2` (the profile ID). EDS API credentials are web-UI logins, not provisioned EDS API profiles. Both require separate provisioning from JHU library IT.
Solution
1. Added `_eit_search()` / `_parse_eit_xml()` to `EbscoApiAdapter` in `adapters/keyed_apis.py`. EIT REST endpoint: `http://eit.ebscohost.com/Services/SearchService.asmx/Search`. Profile built from `EBSCO_ACCOUNT_ID.EBSCO_GROUP_ID.EBSCO_PROFILE_ID`. `pull()` now tries EIT → EDS → seed fallback.
2. Implemented `EbscohostPlaywrightAdapter` in `adapters/playwright_adapters.py` with CDP connect (`http://localhost:9222`), JS DOM extraction via `page.evaluate()`, multi-selector fallbacks for EBSCOhost HTML, detail-page abstract fetching, and era-date URL params (`DT1`/`DT2`).
3. Saved EIT WSDL to `docs/ebsco_eit_wsdl.xml` and credentials/endpoint documentation to `docs/ebsco_eit_api.md`.
Notes
EIT is blocked until JHU library IT provides the account ID prefix (e.g. `s8875689`) so full profile `s8875689.main.eitws2` resolves. Playwright CDP activates once Chrome is launched with `--remote-debugging-port=9222` and user is logged into EBSCOhost.

[2026-04-24] - EbscoApiAdapter Was Not Calling the EDS API
Problem
Every EBSCO pull produced only seed links (search.ebscohost.com click-through URLs). No article titles, abstracts, or full text were ever retrieved.
Root Cause
The adapter never called the EBSCO Discovery Service (EDS) API at all. It only called build_link_rows() which constructs provider search URLs. The credentials in .env (EBSCO_PROF, EBSCO_PWD, EBSCO_PROFILE_ID) were completely unused.
Solution
Replaced the adapter with a full EDS API implementation:
  1. POST /authservice/rest/uidauth → AuthToken
  2. GET  /edsapi/rest/createsession → SessionToken
  3. GET  /edsapi/rest/search (with query + DT1/DT2 era limiters) → records
  4. GET  /edsapi/rest/retrieve (DbId + AN) → full-text HTML or PDF links per record
  5. GET  /edsapi/rest/endsession
Records are parsed for title, authors, journal, abstract, DOI, PDF URL, and full-text HTML. Quality label is "high" (full text), "medium" (abstract), or "seed" (link only). When EDS auth fails (invalid/missing credentials) the adapter falls back to seed click-through URLs with a clear api_error field in stats.
Notes
The credentials in .env return EDS auth error 1102 "Invalid Credentials" — the web UI login (lhyman6@jh.edu) is not an EDS API profile. JHU library IT needs to provision an EDS API profile in EBSCOadmin, or IP-based authentication can be configured. The code is fully ready for valid credentials.

[2026-04-24] - Export Bundle Race Condition: Status Set Before Files Written
Problem
End-to-end test reported _INDEX.md, _BIBLIOGRAPHY.md, and _README.md missing from the export bundle even though the files existed on disk at the time of inspection.
Root Cause
pipeline.py called `save(final_status, "Run complete")` to set status to "complete" BEFORE calling `export_run_bundle()`. The test (and any external caller) polls for "complete", then immediately checks the bundle — but the export hadn't started yet, so the files appeared missing.
Solution
Moved `export_run_bundle(rec, settings)` to run before `save(final_status, ...)` in pipeline.py so the bundle is fully written to disk before the status transitions to "complete".
Notes
The race only affected callers that check bundle contents right after polling for terminal status.

[2026-04-24] - Reset Wrote Wrong Empty Value to events.json
Problem
After clicking Reset in the sidebar, all new runs would get stuck in "queued" state forever with no events appearing.
Root Cause
The reset endpoint wrote `{}` (empty dict) to both `runs.json` and `events.json`. But `runs.json` is a dict keyed by run_id while `events.json` is a flat list. `store.append_event()` calls `events.append(event)` — which fails with `AttributeError: 'dict' object has no attribute 'append'` on a dict. Since this happens inside the background thread before any status update, the run stays in "queued" silently.
Solution
Reset endpoint now writes `{}` for `runs.json` and `[]` for `events.json`, matching the shape each file expects.
Notes
Runs created before the fix can be unblocked by hitting the Retry button.

[2026-04-22] - Fix Settings API Shape Mismatch and Add Provider Dropdowns
Problem
React settings modal showed empty LLM Provider and Browser Provider fields. Selecting a provider value had no effect.
Root Cause
`fetchSettings()` in `api.ts` was returning the raw `/connections/values` response (`{env_path, values[]}`) unmodified. The modal was reading `settings.llm_provider` which is never a key in that shape. Also used free-text inputs for enum-valued fields (ollama/claude/openai, playwright_cdp/http/claude_cu).
Solution
Updated `fetchSettings()` to transform the values array into a flat `key→value` dict and add `llm_provider`/`browser_provider`/`library_system` aliases. Changed Settings modal LLM/browser provider inputs to `<select>` dropdowns with the valid enum options.
Notes
All credential fields still work since they read `settings['ORCH_*']` which is now populated from the flat dict.

[2026-04-22] - Historian Overhaul: LLM + Browser Abstractions, Export Redesign, React Frontend
Problem
App had LLM calls scattered across 4 layer files (each with its own _call_ollama function), browser/Playwright calls hardcoded in adapters with no provider abstraction, a flat artifact export structure using opaque gap_id folder names, and a 2,000-line monolithic vanilla JS frontend.
Root Cause
Original design grew organically without provider abstraction layers. Each layer had its own HTTP call to Ollama. Browser automation was tightly coupled to Playwright/CDP. Export structure used internal IDs instead of human-readable names. Frontend had no component system.
Solution
1. Created `layers/llm_client.py`: LLMClient abstraction with Ollama (default), Claude, and OpenAI backends. `complete()` → str, `complete_json()` → dict with retry. Config selects provider via ORCH_LLM_PROVIDER. All 4 call sites (analysis, reflection, search_policy, fit) now use make_llm_client(settings).
2. Created `adapters/browser_client.py`: BrowserClient abstraction with PlaywrightCDP (default), HTTP, and Claude Computer Use (stub) backends. PageResult envelope with blocked-page detection. `fetch()`, `probe_login()`, `open_tabs()`. Config selects via ORCH_BROWSER_PROVIDER. Updated seed_url_fetch.py and main.py sign-in open to delegate to BrowserClient.
3. Added `llm_provider` and `browser_provider` fields to OrchestratorSettings.
4. Rewrote `artifact_export.py` for historian-friendly output: human-readable gap folder slugs from chapter+claim text, `_README.md` per gap (claim, context, sources table, Ollama synthesis), `_INDEX.md` master cross-reference table, `_BIBLIOGRAPHY.md` deduplicated URLs, `by_chapter/` mirror, `synthesis/` Ollama-generated "what was found / what's missing" summaries. Documents moved to `gaps/<slug>/documents/<source>/` instead of `gaps/<id>/related_documents/<source>/`.
5. Added SSE streaming endpoint `/api/orchestrator/runs/{id}/stream` via sse-starlette (keeps polling endpoint for backwards compat).
6. Built complete React frontend at `frontend/` (Vite + React 18 + TypeScript + Tailwind). FastAPI serves `frontend/dist/` with SPA fallback route. Components: PipelineRail, GapCard with ConfidenceBar + AccordionLadder, EvidencePanel (framer-motion slide-in), SignInSplash, SettingsModal.
7. Created 3 historian test manuscripts in `Manuscript/`: labor_history_new_deal.md, civil_rights_voting_rights.md, federal_reserve_early_history.md.
Notes
Backwards compat: `_call_ollama` kept as shim in analysis.py (reflection.py imports it). Polling events endpoint kept alongside SSE. Legacy static/index.html preserved alongside React build. All 114 tests pass after updating monkeypatching targets to new function names.

[2026-04-05] - Open Sign-In Splash Tabs in Active Playwright CDP Session
Problem
Clicking `Open Sign-In Pages` from the sign-in splash could open tabs only in the current UI browser window, which did not always match the browser session used for Playwright login tests/pulls. Users then had to sign in again.
Root Cause
Frontend splash action used `window.open(...)` only, so sign-in links were not opened through the attached CDP browser context that powers `Test Login` and seed URL pulls.
Solution
Added `POST /api/orchestrator/signin/open` (`SignInOpenInput`) in `main.py` to open selected sign-in URLs in the active CDP browser session. Updated sign-in splash UI (`static/index.html`) so `Open Sign-In Pages` calls this endpoint first and logs open counts; if CDP opening fails, it falls back to local `window.open(...)` tabs.
Notes
This preserves existing workflows while aligning sign-in actions with the authenticated Playwright session whenever CDP is available.

[2026-04-05] - Add Mandatory Sign-In Splash Before Login Tests
Problem
Users could click `Test Login` without a clear, explicit pre-check instruction to sign into university/provider systems first, causing confusing blocked results.
Root Cause
Login tests launched immediately from run/settings UI actions without an interstitial prompt that emphasized required sign-in behavior.
Solution
Updated `static/index.html` to add a blocking sign-in splash modal shown before both run-level and per-database login tests. The splash lists target providers, includes `Open Sign-In Pages`, and requires explicit continue/cancel before test execution.
Notes
This is a UI workflow clarity change only; sign-in test API contracts are unchanged.

[2026-04-05] - Reduce Playwright Focus Stealing with Background-First CDP Fetch
Problem
Automated Playwright checks could pull browser focus by opening tabs while testing/pulling provider URLs.
Root Cause
CDP retrieval used direct page navigation as the primary path, which may create/focus transient tabs in attached browser sessions.
Solution
Updated `adapters/seed_url_fetch.py` CDP flow to try a storage-state request-context fetch first (authenticated background request) and only fall back to opening a transient page when needed.
Notes
This is a best-effort focus reduction; some provider flows may still require page fallback depending on site behavior.

[2026-04-05] - Prevent CDP Login Tests From Closing User Browser Session
Problem
Clicking login/test actions could make the Chrome debug window disappear, interrupting sign-in and causing follow-up CDP connection failures.
Root Cause
CDP fetch helper (`_fetch_via_cdp`) called `browser.close()` after `connect_over_cdp(...)`, which can close the attached user browser session instead of only cleaning up transient test artifacts.
Solution
Updated `adapters/seed_url_fetch.py` CDP fetch flow to avoid closing the connected browser. The helper now closes only a temporary context when one is explicitly created, and leaves the user’s existing browser session intact. Added regression coverage in `tests/test_seed_url_fetch.py` to ensure browser close is not invoked for attached CDP sessions.
Notes
This is a runtime behavior fix only; API contracts and pull outputs are unchanged.

[2026-04-05] - Add Per-Database Login Test Controls in Settings
Problem
Users could test login readiness only from the run preflight flow, but there was no quick way in Settings to verify individual library databases and see pass/fail state per provider.
Root Cause
Settings database rows were informational only; they did not include source-specific login probe actions or persistent row-level status indicators.
Solution
Updated `static/index.html` Settings database rendering to include a `Test Login` button on each row and row-level status badges. Wired these actions to `POST /api/orchestrator/signin/test` with `source_ids=[source_id]`, and display green for pass (`ok`) and red for blocked/unreachable outcomes, including diagnostic messages.
Notes
This is an additive UI enhancement. It reuses existing sign-in test backend contracts and does not change pipeline execution behavior.

[2026-04-05] - Add Semantic Workflow Colors for Ready/Blocked/Completed States
Problem
Workflow status cues were visually inconsistent, making it harder to quickly tell whether a stage was ready to proceed, blocked, or fully completed.
Root Cause
Status text and stage cards used mixed styles without a strict semantic mapping, and sign-in/launch state transitions did not consistently apply explicit state classes.
Solution
Updated `static/index.html` workflow styling and state transitions so semantic colors are enforced across launch/sign-in/stage surfaces: green for `ready`, red for `blocked`, and black for `completed`. Added explicit status helpers in UI logic to apply consistent state classes and synced sign-in box border styling with the same state model.
Notes
This is a frontend UX clarity improvement only; orchestration contracts and backend pipeline behavior remain unchanged.

[2026-04-05] - Make Sign-In Checklist Manuscript-Aware via Analysis Preflight
Problem
Pre-run login checklist was generated from profile-level availability only, so users could be asked to sign into providers not actually needed for the selected manuscript run.
Root Cause
Sign-in target generation happened before manuscript-specific analysis/reflection planning, so it lacked knowledge of planned provider routes.
Solution
Added `POST /api/orchestrator/signin/preflight` to run analysis+reflection preflight for the selected manuscript and derive sign-in targets from planned source IDs. Updated frontend sign-in stage to require `Analyze Sources` before login confirmation, and wired `Test Login` to probe those manuscript-derived targets.
Notes
This is additive and contract-safe. Full run pipeline stages are unchanged; this only improves pre-run targeting precision for login checks.

[2026-04-05] - Add Pre-Run "Test Login" Provider Access Probe
Problem
Users could mark pre-run sign-in complete without any direct verification that their active browser/library session could access required provider platforms.
Root Cause
The sign-in stage only rendered checklist links and manual confirmation; there was no automated provider-access probe tied to active library profile and source availability.
Solution
Added `POST /api/orchestrator/signin/test` in `main.py` to probe active provider sign-in URLs and return per-source status (`ok`, `blocked`, `unreachable`) with fetch mode, blocked reason, and action hints. Added `probe_sign_in_access(...)` in `adapters/seed_url_fetch.py` (CDP-first with direct-HTTP fallback) and wired a new `Test Login` button in `static/index.html` to run this probe and render status per platform before launch.
Notes
This is additive and contract-safe. Run launch gating remains user-confirmed (`Mark Sign-In Complete`), while `Test Login` provides explicit readiness diagnostics.

[2026-04-05] - Include Playwright Python Client in Docker Runtime
Problem
Dockerized runs could report Playwright source availability but still never perform browser-backed seed URL fetches, leaving pull output at seed links only.
Root Cause
`adapters/seed_url_fetch.py` uses `playwright.sync_api` for CDP-backed fetch fallback, but the Docker image dependencies did not include the Playwright Python package. Import failed and fetch silently returned empty.
Solution
Added `playwright==1.54.0` to `requirements.txt` so container runtime includes the Playwright client needed for `connect_over_cdp(...)` calls during seed URL resolution.
Notes
This does not require bundled browser binaries for current usage because runtime attaches to an external Chrome CDP session.

[2026-04-05] - Normalize Docker CDP Hostname for Playwright Browser Attach
Problem
Docker runs reported Playwright/CDP as unavailable even when Chrome remote debugging was active on the host, so browser-backed source pulls could not execute.
Root Cause
When `ORCH_PLAYWRIGHT_CDP_URL` used `host.docker.internal`, Chrome DevTools returned HTTP 500 because the request Host header was a hostname rather than `localhost`/IP. Availability probe and CDP fetch code used the hostname directly.
Solution
Added `adapters/cdp_utils.py` with `effective_cdp_url(...)` to normalize `host.docker.internal` to its resolved IP before probing/connecting. Wired this into both `check_cdp_endpoint(...)` and seed URL CDP fetch (`_fetch_via_cdp(...)`) so health checks and real browser pulls share the same fix path. Added regression tests in `tests/test_cdp_utils.py`.
Notes
This is contract-safe and runtime-focused. Existing `.env` values remain valid; Docker Playwright attach is now resilient to Chrome host-header constraints.

[2026-04-05] - Add Pre-Run Sign-In Stage and Launch Gate in Run Workflow
Problem
Users could start runs immediately without an explicit login step, which made authentication-dependent pulls fail later (or produce blocked pages) without a clear pre-run operator action point.
Root Cause
The Run UI had no dedicated preflight stage for platform authentication and no launch gate requiring users to confirm they had signed into required library/provider systems.
Solution
Updated `static/index.html` to add a `Pre-Run Sign-In Stage` in the Launch panel. The stage loads sign-in checklist entries from active source catalog + health availability, renders open-platform links, and requires explicit `Mark Sign-In Complete` confirmation before `Run Research` can start. Also added a visible `signin` stage in the stage rail and reset sign-in readiness on manuscript changes/uploads.
Notes
This is a frontend workflow/control-plane change only; backend run contracts are unchanged.

[2026-04-05] - Surface CAPTCHA/Login Blockers and Prefer API-Family Sources
Problem
Runs could report successful pulls while actually storing blocked login/challenge HTML snapshots, and users were not explicitly told when manual CAPTCHA/login bypass was required. Source selection could also spend effort on same-family Playwright routes (for example `ebscohost`) even when keyed API routes (`ebsco_api`) were available.
Root Cause
Resolved snapshot artifacts were treated as medium/high pull evidence by default, with no blocked-page classification or run-event warnings. Pull source ordering had no family-level API preference pass, so browser fallback could remain in the candidate list despite an available API source.
Solution
Added blocked-page detection in `adapters/seed_url_fetch.py` for common CAPTCHA/challenge/login/access-denied signals, tagged blocked rows with `blocked_reason`/`action_required`, and demoted those rows to seed quality so they do not count as real pulled evidence. Wired blocked stats (`blocked_files`, `captcha_blocks`, `challenge_blocks`, `login_blocks`) into Playwright and keyed adapters, and emitted explicit `pulling/warn` events in accordion execution instructing users to complete provider verification/login in-browser then retry. Added source-family API preference in `layers/pull.py` so keyed sources are preferred over same-family Playwright fallbacks (for example keep `ebsco_api`, skip `ebscohost` when both are present). Exposed blocked metadata in document API rows and UI rendering/log styling.
Notes
This improves transparency and pull quality accounting without changing API contracts. Source-specific full-text extraction workflows are still needed for deeper retrieval beyond search/login pages.

[2026-04-03] - Prefer JSON Packet Links Over Raw Resolved-File Packets in Results API
Problem
Run document views could become noisy and misleading because `/runs/{run_id}/documents` treated nested `_resolved_urls`/`_fetched_urls` files as top-level packets, creating duplicate rows and inflated quality counts.
Root Cause
Packet indexing walked all files under each adapter run directory and built packets for both JSON packet files and nested resolved artifacts. Flattened rows then repeated the same source artifact through multiple packet paths.
Solution
Updated `main.py` document indexing to make JSON packet files the source of truth, skip nested resolved/fetched URL files as standalone packets, and dedupe flattened rows by stable evidence/locator keys. Added direct-file quality calibration helper and preserved link metadata (`title`, `link_type`, `source_key`) in flattened API rows. Added regression coverage in `tests/test_main_api.py` to ensure resolved artifacts are surfaced as linked docs, not duplicate packets.
Notes
This is contract-safe: API shapes remain unchanged, but packet quality/readability now better reflects actual pulled evidence.

[2026-04-03] - Resolve Seed Search URLs Into Pulled Local Artifacts During Adapter Pulls
Problem
Runs could complete with seed-only provider-search rows (for example Project MUSE/JSTOR placeholder links), so click-through often landed on broad search pages and still required manual source hunting.
Root Cause
Playwright/keyed seed adapters emitted provider/local link rows but did not execute a follow-on retrieval pass to pull concrete page/document artifacts from those seed URLs.
Solution
Added `adapters/seed_url_fetch.py` and wired it into `PlaywrightAdapter._link_seed_result(...)` and `EbscoApiAdapter.pull(...)`. Seed/provider URLs are now fetched into per-query `_resolved_urls/<query>/` folders, child links are selectively followed, and pulled artifacts are appended as `resolved_snapshot` rows with medium/high quality labels. Added tests (`tests/test_seed_url_fetch.py`, expanded `tests/test_adapter_links.py`) and verified end-to-end runs now produce non-seed pulled artifacts for previously seed-only sources.
Notes
Source-specific extraction remains improvable, but this closes the seed-only gap by ensuring adapters attempt concrete pull artifacts as part of normal run execution.

[2026-04-02] - Add Stable Evidence IDs and Snippet-Linked Document References
Problem
Users could open pulled sources, but links back to the exact support point were fragile and required re-reading full source materials to relocate relevant passages.
Root Cause
Document packet rows lacked deterministic evidence references and snippet-level metadata. Results UI links pointed to broad source URLs/files without stable quote hashes or reusable evidence lookup paths.
Solution
Added deterministic `evidence_id` generation in document indexing based on normalized locator + quote hash, attached snippet metadata (`excerpt`, `quote_hash`, `source_locator`) to linked document rows, and generated best-effort text-fragment jump links (`anchor_url`) for URL sources with excerpt text. Added evidence lookup APIs (`/api/orchestrator/runs/{run_id}/evidence/{evidence_id}` and `/api/orchestrator/evidence/{evidence_id}`) and updated UI rendering to expose stable evidence references and snippet-open actions.
Notes
This is the basic stable-linking layer only. Multi-LLM evidence arbitration remains intentionally separate as an advanced feature.

[2026-04-02] - Add Switchable Frontend Interface Variants for Run + Settings
Problem
Operators needed to compare multiple frontend interface styles quickly without branching code or losing runtime functionality while evaluating UX direction.
Root Cause
The app shipped with a single visual system in `static/index.html`, so style experiments required manual code edits and page reloads with no persistent style preference.
Solution
Added a top-level `Interface Style` selector with three variants (`editorial`, `operations`, `atlas`) and local-storage persistence (`orchestrator_v2_ui_variant`). Implemented variant-specific typography/color/spacing/layout tokens while keeping all API/run behavior unchanged. Added responsive override guards so variant desktop grids reset correctly on mobile. Documented variant theses and usage in `docs/frontend_interface_variants.md`, and updated app/docs references.
Notes
This is presentation-only and contract-safe: backend APIs, run orchestration, and settings persistence semantics are unchanged.

[2026-04-02] - Refresh Gap Export Folders Per Run and Follow Seed URLs Into Gap Artifacts
Problem
Manuscript gap folders could contain stale files from previous runs of the same manuscript, and seed-link exports often stayed at provider-search URL placeholders instead of fetching linked page/document artifacts into gap folders.
Root Cause
Bundle export reused the same manuscript-title directory across runs without clearing prior `gaps/` content, so old artifacts persisted. URL follow-up was limited to raw href traversal and could waste child fetch attempts on static assets.
Solution
Updated `artifact_export.py` to clear `manuscript_exports/<title>/gaps` at the start of each export, ensuring each run produces a fresh per-gap artifact snapshot. Added best-effort URL follow fetch from copied source JSON URLs into `_fetched_urls` and filtered child-link traversal to skip obvious static asset extensions. Added regression tests for URL-follow fetch behavior and stale-gap cleanup in `tests/test_artifact_export.py`, and documented refreshed gap export semantics in `docs/orchestrator_app.md`.
Notes
This is additive and contract-safe: report/manifest filenames remain per-run, while gap artifact folders now reflect the latest run only for that manuscript title.

[2026-04-02] - Make Repo-Root Runs Work and Refresh Saved .env Values Immediately
Problem
Fresh GitHub clones did not run locally with documented commands because source/tests expected an `app.*` package path that was not present in this checkout, and Settings saves could appear stale because process env values continued to shadow newly saved `.env` values.
Root Cause
Imports and run/test commands were still aligned to an older package layout. In addition, `.env` save flow updated the file but did not evict edited keys from `os.environ`, so subsequent reads favored stale in-process values.
Solution
Updated imports and run/test entrypoints to repo-root module paths (`main:app`, `tests/`), aligned Docker/compose path assumptions to the current checkout, and restored a first-class Settings page for library profile + credential management. Added `/api/orchestrator/library/profiles` for selectable base library systems. Updated `.env` persistence helpers to allow blank updates, and updated connection-save flow to clear edited keys from process env so refreshed values reflect saved `.env` content immediately.
Notes
Contracts remained additive. Regression coverage was added for library-profile endpoint behavior and blank-value `.env` saves. Full suite passes via `python3 -m pytest tests -q`.

[2026-04-14] - Accordion Search Model with Era Vocabulary
Problem
Historical manuscript claims were routed to wrong source families (e-commerce claims hitting macro-stat APIs). Queries used only modern vocabulary and missed the historical record that used period terminology. Zero-result queries were logged and abandoned with no systematic broadening.
Root Cause
`_claim_routing_profile` used keyword regex that missed commerce/platform/retail vocabulary, routing claims to `OTHER/MIXED` at 0.46 confidence. `_clean_queries` filtered existing queries but generated no era-equivalent vocabulary. No backoff existed to recover from zero results by trying related period terms.
Solution
New module `layers/search_policy.py` implements the accordion model:
1. One LLM call per gap (temperature=0, ~25s timeout) generates a `SynonymRing` with three vocabulary drift types (terminology_shifts, institutional_names, era_modifiers) plus a four-rung `AccordionLadder` with {PRIMARY} templates.
2. `get_accordion_move` drives execution: lateral through synonyms at current scope before widening to the next rung. Five actions: accept, lateral, widen, tighten, exhausted.
3. Synonym ring and ladder stored on `gap.query_ladder` for auditability and retry without re-calling the LLM.
4. Heuristic fallback (empty synonym ring, regex classifier) on Ollama failure.
5. All accordion state emitted as structured log events, visible in UI run log.
6. Plan cards in frontend show synonym ring categories, rung templates, and era range.
Notes
`era_start`/`era_end` extracted by LLM and stored on `SynonymRing`; see [2026-04-14] date-range faceting entry below for adapter wiring.
Subject heading pivot and archival finding-aid sources bracketed for future sprint.

[2026-04-14] - Era Date Range Faceting in Provider Search URLs and BLS
Problem
Provider click-through search URLs were era-blind: JSTOR, EBSCO, ProQuest, and other database URLs generated by adapters contained no date facets, so users clicking through landed on unfiltered results even when the LLM had already identified the claim's historical era. BLS time-series calls used a hardcoded 2019–2024 window regardless of the manuscript's period.
Root Cause
`era_start`/`era_end` were extracted by the accordion model LLM call and stored on `SynonymRing` in `gap.query_ladder`, but `provider_search_url` and `build_link_rows` in `adapters/document_links.py` had no parameter for era bounds, so adapter `pull()` calls could not forward them. BLS `BlsAdapter.pull()` had literal string values `"2019"/"2024"` that were never connected to the claim's era.
Solution
Added `era_start`/`era_end` optional params to `provider_search_url` and `build_link_rows` in `adapters/document_links.py`. Date-range URL parameters are now appended for sources that support faceting: JSTOR (`sd`/`ed`), ProQuest (`daterange=custom`, `startdate`/`enddate`), EBSCOhost (`DT1`/`DT2` in YYYYMMDD format), Gale (`startDate`/`endDate`), and Americas Historical Newspapers (`date_low`/`date_high`). Added `era_years_from_gap()` helper in `adapters/io_utils.py` to extract era bounds from `gap.query_ladder` safely. Updated `BlsAdapter.pull()` to call this helper and use `era_start`/`era_end` as `startyear`/`endyear`, falling back to `"2019"`/`"2024"` when no era is available. Updated `EbscoApiAdapter.pull()` and `PlaywrightAdapter._link_seed_result()` to extract era bounds from the gap and pass them to `build_link_rows`. Added 12 new regression tests covering URL parameter injection, `era_years_from_gap` edge cases, and adapter propagation.
Notes
No API contract changes. `provider_search_url` remains backward-compatible (era params default to None, producing identical output when omitted). Per-source noise thresholds remain a follow-up item.

[2026-04-02] - Load Local .env From Repository Root in API Runtime
Problem
API health and runs showed keyed APIs as unavailable (`missing_keys`) even when valid credentials existed in the repository `.env`, leading to avoidable zero-result routing quality in local runs.
Root Cause
Runtime settings in `main.py` defaulted `ORCH_WORKSPACE` to the parent of the repository root, so `load_runtime_env(...)` read the wrong path and skipped the project `.env`.
Solution
Updated `_settings()` to default workspace to `BASE_DIR` (repository root), ensuring local `.env` is loaded consistently when `ORCH_WORKSPACE` is unset. Added regression test asserting default workspace equals repo root.
Notes
This is contract-safe and local-runtime focused. Explicit `ORCH_WORKSPACE` still overrides default behavior.
