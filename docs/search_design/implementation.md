# Historian Search Policy: Implementation Guide

**Status:** Code complete, 49/49 tests passing.
**Deliverables:** `app/layers/search_policy.py`, `app/tests/test_search_policy.py`

---

## 1. Why This Exists: What the Librarian Guides Actually Teach

The problem this code solves is not primarily a software problem. It is a
research methodology problem that the software was ignoring. The librarian
guides provide the methodology.

### Harvard "Exploring Your Topic"
*Library Research Guide for History* (guides.library.harvard.edu/history/exploring)

> "The best way of finding primary sources is often to get an overview of your
> topic. Collect names of persons and organizations involved, and **words and
> phrases used in your era** (public health was once called hygiene). These names
> and terms can be searched in the primary source databases."

The core instruction is **vocabulary-first, before searching**. Before any query
is formed, a researcher gathers proper nouns (people, organizations, institutions)
and era-specific terminology. The guide is explicit that the same concept carries
different names across time — a search using modern vocabulary will systematically
miss the historical record that used period vocabulary.

The guide also instructs researchers to reason about what *kinds of documents*
the people and organizations involved would have generated — correspondence,
annual reports, testimony, newspapers — before choosing a source. The document
type is not incidental; it determines which database to use.

### Harvard "Finding Online Sources: Detailed Instructions"
*Library Research Guide for History* (guides.library.harvard.edu/history/detailed)

The structured query pattern taught here:

```
Subject terms: [entity or topic]
AND
Subject terms: sources OR archives OR correspondence OR diaries
AND
Material type: not ebook
Limit: Internet Resources
```

And for open-web search:

```
all these words:  [topic entity]
any of these words:  archives manuscripts correspondence diaries scrapbooks sources letters
```

The key technique is **always appending archival document-type vocabulary** to
entity keywords. The guide gives a concrete example: `women slavery` alone returns
general texts; `women slavery AND (archives OR correspondence)` surfaces primary
source collections. Entity keywords alone are insufficient.

The guide also specifies **WorldCat subject-heading pivoting**: find one good
catalog record, harvest its subject subdivisions (the terms after hyphens in the
catalog record), and rerun searches using those subdivisions against other subject
terms. This is a self-correcting loop that the current system does not yet implement
(bracketed as future work below).

### Stanford "Search and Find"
*Stanford University Libraries* (library.stanford.edu/services/search-and-find)

Stanford's guide teaches **tool routing by material type**. The catalog
(SearchWorks) is for physical and digital books, journals, archives, and databases.
Articles+ is for journal articles and e-resources. The Online Archive of California
is for archival finding aids. Databases are for topic-specific deep search. The
actionable rule: choose the tool based on what *kind of thing* you are looking
for, not by default habit. A researcher looking for a statistical series should
not start with an article database. A researcher looking for a business event in
1994 should not start with a macro-statistics API.

---

## 2. The Root Cause This Addresses

The prior `_claim_routing_profile` function in `reflection.py` used a small set
of keyword regex patterns. A claim like "NetMarket enabled the first secure online
transaction" matched none of the patterns (no words like "history," "merchant," or
"century") and fell through to `OTHER/MIXED` at 0.46 confidence. It was then
routed to macro-statistics APIs (BLS, World Bank) — the exact wrong source family
for a company operations claim that belongs in newspaper archives and JSTOR.

The `_clean_queries` function that followed filtered and scored whatever queries
the upstream LLM reflection had already produced, but it generated no new
vocabulary. It had no awareness that "e-commerce" in 1994 was called "electronic
commerce" or "online retailing" in the sources of the period. Every query rung
used the author's modern phrasing. Nothing would find the historical record.

The accordion model fixes both problems in a single LLM call per gap.

---

## 3. The Accordion Model

The accordion model is the organizing principle of `search_policy.py`. The name
describes how it works: it contracts toward precision first, then expands laterally
through era synonyms before widening scope, and expands further only when all
synonyms at the current scope are exhausted.

### 3a. The Synonym Ring

Before any query is executed, the LLM generates a **SynonymRing** for the claim.
The ring has three named fields, each corresponding to a distinct type of
vocabulary drift identified in the Harvard guide:

**`terminology_shifts`** — What the concept was actually called in the era.
"E-commerce" in a 1994 newspaper would appear as "electronic commerce," "online
retailing," "internet shopping," or "direct marketing." These are not synonyms in
the modern sense; they are the period vocabulary that the historical record
actually uses.

**`institutional_names`** — How the organization appeared in period sources.
"NetMarket" might appear as "Net Market Inc," "Netmarket.com," or "the Internet
Shopping Network." Catalog records and newspaper indexes often use the formal
registered name, not the brand shorthand.

**`era_modifiers`** — Adjectives and qualifiers used in period headlines, article
titles, and catalog subject headings. "Interactive commerce," "computer-mediated
trade," "cyberspace retail." These are the words a journalist or cataloger in
1994 would attach to the concept that would not appear in a modern framing of the
same claim.

The three types are kept separate because they require different LLM reasoning.
Asking for "synonyms" produces near-duplicates. Asking explicitly for terminology
shifts, institutional names, and era modifiers forces the model to think about
each drift type independently.

### 3b. The Accordion Ladder

The **AccordionLadder** has four named rungs and a synonym ring. Each rung is a
query template using `{PRIMARY}` as a placeholder for the primary entity term.
At execution time, `{PRIMARY}` is substituted with each variant in the synonym
ring in turn, up to `synonym_cap` (default 3).

```
constrained:  {PRIMARY} 1994 online commerce newspaper periodical historical press
contextual:   {PRIMARY} 1994 online commerce
broad:        {PRIMARY}
fallback:     commerce retail history   (chapter keywords only — no {PRIMARY})
```

With a synonym ring containing `["electronic commerce", "online retailing"]`, the
constrained rung generates:

```
NetMarket 1994 online commerce newspaper periodical historical press
electronic commerce 1994 online commerce newspaper periodical historical press
online retailing 1994 online commerce newspaper periodical historical press
```

### 3c. Execution: Lateral Before Vertical

The accordion contracts before it expands. Execution order:

```
constrained + primary term     → if 0 results: lateral →
constrained + synonym_1        → if 0 results: lateral →
constrained + synonym_2        → if synonyms exhausted: widen →
contextual  + primary term     → if 0 results: lateral →
contextual  + synonym_1        → ...
broad       + primary term     → ...
fallback    (no substitution)  → if 0 results: exhausted → needs_review
```

The five possible moves from `get_accordion_move`:

| Action | Trigger | Next state |
|---|---|---|
| `accept` | 0 < results ≤ noise_threshold | Stop, merge results |
| `lateral` | 0 results, synonyms remain at current rung | Next synonym, same rung |
| `widen` | 0 results, synonyms exhausted at current rung | Next rung, primary term |
| `tighten` | results > noise_threshold | Jump to constrained rung |
| `exhausted` | All rungs and synonyms tried | Mark gap `needs_review` |

A gap with three synonym variants and four rungs has up to 12 query attempts
before being marked exhausted — all within a single source adapter call, emitting
structured log events at each step.

### 3d. Archival Suffix Table

The constrained rung always appends archival document-type vocabulary, directly
encoding the Harvard "Detailed Instructions" pattern of
`[entity] AND (archives OR correspondence OR diaries)`:

| Evidence need | Appended vocabulary |
|---|---|
| `scholarly_secondary` | `scholarly article journal history` |
| `primary_source` | `archives correspondence diaries manuscripts sources` |
| `news_archive` | `newspaper periodical historical press` |
| `legal_text` | `statute act regulation law court ruling` |
| `official_statistics` | `statistics data time series official` |
| `mixed` | `historical evidence primary source archive` |

### 3e. Heuristic Fallback

When Ollama is unavailable, `classify_and_build_ladder` falls back to a
regex-based classifier and a heuristic ladder with an empty `SynonymRing`. The
pipeline stays alive; the UI shows `generation_method: heuristic` in the plan
card so the degradation is visible and auditable. No era vocabulary is generated
in fallback mode — the gap will still be searched, but only with modern terminology.

### 3f. Storage

The `AccordionLadder.to_dict()` result is stored on `gap.query_ladder` (a
`Dict[str, object]` field added to `PlannedGap` in `contracts.py`). This means:

- The synonym ring is visible in the UI plan card
- The pull layer reconstructs the ladder on retry without re-calling the LLM
- The full search strategy is auditable in the run record

---

## 4. What This Fixes and What Remains Bracketed

### Fixed

**Claim-to-source routing** — The LLM reads the claim in context rather than
matching keywords. Commerce, retail, platform, and e-commerce claims are correctly
classified as `company_operations → news_archive` rather than falling through to
`OTHER/MIXED` and hitting macro-statistics APIs.

**Era vocabulary** — The synonym ring generates period-equivalent terminology
before the first query fires. Queries no longer depend solely on the author's
modern framing.

**Archival suffix pattern** — The constrained rung always appends document-type
vocabulary per the Harvard guide, surfacing primary source collections rather
than general texts.

**Systematic backoff** — Every zero-result query triggers a structured decision
(lateral or widen) rather than silent continuation. Every move emits a log event
with rung and synonym index, making the search behavior fully auditable.

**Pipeline liveness** — Ollama failure at any point falls back gracefully. No
blocking, no crashes, no silent skips.

### Bracketed (explicitly out of scope for this sprint)

**Archival finding-aid sources** — JSTOR and EBSCO are full-text article
databases, not finding-aid databases. Claims requiring primary source *collections*
(ArchiveGrid, OAISTER, repository EAD catalogs) are not yet routed to the right
tool family. Bracketed pending adapter development.

**Subject heading pivot** — The Harvard guide teaches harvesting Library of
Congress subject subdivisions from returned records to refine subsequent queries.
This requires a feedback channel from ingest back to planning that does not exist
in the current architecture. Bracketed as a future sprint.

### Remaining gap (not bracketed, follow-up tickets)

**Date range faceting** — The LLM extracts `era_start` and `era_end` from the
claim and stores them on the `SynonymRing`. These values are not yet passed to
adapters as date-range filter parameters. EBSCO, ProQuest, and JSTOR all support
date faceting. Passing `era_start`/`era_end` to adapter `pull()` calls requires
no changes to `search_policy.py`.

**Per-source noise thresholds** — `ORCH_PULL_NOISE_THRESHOLD` is currently a
single global value. Stat APIs should never tighten on count alone; news archives
should tighten at a lower threshold than article databases. Config-only change,
no code required.

---

## 5. Agent Handoff

### Files the agent must read first (do not skip)

```
AGENTS.md
SOLUTIONS.md
docs/orchestrator_app.md
README.md
```

### New files to place in the repo

```
search_policy.py       →  app/layers/search_policy.py
test_search_policy.py  →  app/tests/test_search_policy.py
```

### Files the agent must modify

**`app/contracts.py`**

Add one field to the `PlannedGap` dataclass, after `review_notes`:

```python
query_ladder: Dict[str, object] = field(default_factory=dict)
```

`query_ladder` stores the full `AccordionLadder.to_dict()` output including the
synonym ring. Existing serialized records deserialize safely with an empty-dict
default because `from_primitive` uses field defaults for missing keys. No
migration required.

Note: the old `BackoffResult` import (if any exists at the contracts layer) can
be removed. It is replaced by `AccordionMove` in `search_policy.py` and is not
used at the contracts layer.

---

**`app/layers/reflection.py`**

Add import at the top (after existing pull imports):

```python
from .search_policy import classify_and_build_ladder, AccordionLadder, query_quality_score
```

Replace `_claim_routing_profile` entirely. The new version calls
`classify_and_build_ladder`, stores the ladder on `gap.query_ladder`, and
converts the returned string enums to `ClaimKind`/`EvidenceNeed`. It must
receive `settings` as a parameter:

```python
def _claim_routing_profile(
    gap: PlannedGap, settings: OrchestratorSettings
) -> Tuple[ClaimKind, EvidenceNeed, float]:
    ck_str, en_str, conf, ladder = classify_and_build_ladder(
        chapter=gap.chapter,
        claim_text=gap.claim_text,
        use_llm=settings.reflection_use_ollama,
        model=settings.reflection_model,
        base_url=settings.ollama_base_url,
        timeout_seconds=min(settings.reflection_timeout_seconds, 25),
        existing_queries=gap.search_queries[:] if gap.search_queries else None,
    )
    gap.query_ladder = ladder.to_dict()
    try:
        claim_kind = ClaimKind(ck_str)
    except ValueError:
        claim_kind = ClaimKind.OTHER
    try:
        evidence_need = EvidenceNeed(en_str)
    except ValueError:
        evidence_need = EvidenceNeed.MIXED
    return (claim_kind, evidence_need, conf)
```

Update the one call site in `_apply_routing_policy` to pass `settings`:

```python
claim_kind, evidence_need, claim_conf = _claim_routing_profile(gap, settings)
```

Replace `_clean_queries` to read from the ladder already stored on the gap:

```python
def _clean_queries(gap: PlannedGap, evidence_need: EvidenceNeed) -> Tuple[List[str], float]:
    ladder_dict = getattr(gap, "query_ladder", {}) or {}
    if ladder_dict:
        ladder = AccordionLadder.from_dict(ladder_dict)
        # Return constrained rung queries as the planning-time query list.
        # The pull layer handles full accordion traversal at execution time.
        ordered = ladder.queries_for_rung("constrained") or ladder.queries_for_rung("contextual")
    else:
        # Fallback: existing keyword extraction behavior
        kws = _extract_keywords(f"{gap.chapter} {gap.claim_text}")
        core = " ".join(kws[:4]) or gap.chapter or "manuscript claim"
        archival = _ARCHIVAL_SUFFIX.get(evidence_need.value, "historical evidence archive")
        ordered = [f"{core} {archival}", core]
    scores = [query_quality_score(q) for q in ordered]
    avg = sum(scores) / len(scores) if scores else 0.0
    return (ordered[:4], avg)
```

Remove the old `_ARCHIVAL_SUFFIX` dict and `_query_quality_score` function from
`reflection.py` — both are now defined in `search_policy.py`. Replace any
remaining usages of `_query_quality_score` with `query_quality_score` from the
import above.

---

**`app/layers/pull.py`**

Add imports:

```python
from .search_policy import get_accordion_move, AccordionLadder
```

Replace the inner query-execution loop with an accordion-aware loop:

```python
def _execute_with_accordion(
    adapter: PullAdapter,
    gap: PlannedGap,
    run_dir: str,
    timeout_seconds: int,
    emit_fn,
    noise_threshold: int = 50,
    synonym_cap: int = 3,
) -> List[SourceResult]:
    ladder_dict = getattr(gap, "query_ladder", {}) or {}
    ladder = AccordionLadder.from_dict(ladder_dict) if ladder_dict else AccordionLadder()

    results: List[SourceResult] = []
    attempted: set[str] = set()

    current_rung = "constrained"
    current_synonym_idx = 0
    queries = ladder.queries_for_rung(current_rung, synonym_cap)
    if not queries:
        current_rung = "contextual"
        queries = ladder.queries_for_rung(current_rung, synonym_cap)
    if not queries:
        return results

    current_query = queries[0]

    while True:
        key = current_query.strip().lower()
        if key in attempted:
            break
        attempted.add(key)

        try:
            result = adapter.pull(gap, current_query, run_dir, timeout_seconds)
            doc_count = result.document_count or 0
        except Exception as exc:  # noqa: BLE001
            emit_fn("pulling", "warning",
                    f"[{gap.gap_id}] {adapter.source_id}: {exc}",
                    {"gap_id": gap.gap_id, "source_id": adapter.source_id,
                     "query": current_query[:80]})
            doc_count = 0

        move = get_accordion_move(
            ladder, current_rung, current_synonym_idx,
            doc_count, noise_threshold=noise_threshold, synonym_cap=synonym_cap,
        )

        emit_fn("pulling", move.action,
                f"[{gap.gap_id}] {adapter.source_id}: {move.reason}",
                {"gap_id": gap.gap_id, "source_id": adapter.source_id,
                 "query": current_query[:80], "rung": current_rung,
                 "synonym_idx": current_synonym_idx, "doc_count": doc_count,
                 "action": move.action})

        if move.action == "accept":
            if doc_count > 0:
                results.append(result)
            break

        if move.action == "exhausted":
            gap.needs_review = True
            gap.review_notes = (gap.review_notes or "") + " Accordion exhausted all rungs."
            break

        if move.action in {"lateral", "widen", "tighten"}:
            if doc_count > 0:
                results.append(result)
            current_rung = move.rung
            current_synonym_idx = move.synonym_idx
            current_query = move.next_queries[0] if move.next_queries else ""
            if not current_query:
                break
            continue

    return results
```

Replace the existing per-query adapter call loop with a call to
`_execute_with_accordion`. The existing backoff helper (if any) can be removed.
Add `noise_threshold=settings.pull_noise_threshold` to the call.

---

**`app/config.py`**

Add to `OrchestratorSettings` dataclass:

```python
pull_noise_threshold: int
```

Add to `OrchestratorSettings.from_env()`:

```python
pull_noise_threshold=int(os.getenv("ORCH_PULL_NOISE_THRESHOLD", "50")),
```

---

**`app/static/index.html`**

In the plan card `innerHTML` template, replace the static queries line with
accordion-aware display:

```javascript
const ladder = gap.query_ladder || {};
const ring = ladder.synonym_ring || {};
const termShifts   = (ring.terminology_shifts  || []).join(", ");
const instNames    = (ring.institutional_names  || []).join(", ");
const eraModifiers = (ring.era_modifiers        || []).join(", ");
const genMethod    = ladder.generation_method || "unknown";
const eraRange     = (ring.era_start && ring.era_end)
    ? ` · ${ring.era_start}–${ring.era_end}` : "";

const ladderHtml = ladder.constrained
    ? `<div class="muted ladder">
         <span class="pill">${genMethod}</span>${eraRange}<br>
         🔬 <strong>constrained:</strong> ${ladder.constrained}<br>
         🔭 <strong>contextual:</strong>  ${ladder.contextual}<br>
         📖 <strong>broad:</strong>       ${ladder.broad}<br>
         ${termShifts   ? `era terms: ${termShifts}<br>`     : ""}
         ${instNames    ? `inst. names: ${instNames}<br>`    : ""}
         ${eraModifiers ? `era modifiers: ${eraModifiers}`   : ""}
       </div>`
    : `<div class="muted">queries: ${
         (gap.search_queries || []).slice(0, 3).join(" | ") || "none"
       }</div>`;
```

In `summarizeEvent`, extend the detail line to show accordion state:

```javascript
function summarizeEvent(evt) {
    const ts     = evt.ts_utc  || "";
    const stage  = evt.stage   || "stage";
    const status = evt.status  || "status";
    const msg    = evt.message || "";
    const meta   = evt.meta    || {};
    let detail   = "";
    if (["lateral", "widen", "tighten", "exhausted"].includes(status) && meta.rung) {
        detail = ` [${meta.action} rung=${meta.rung} syn=${meta.synonym_idx ?? "?"} docs=${meta.doc_count ?? "?"}]`;
    } else if (status === "accept" && meta.rung) {
        detail = ` [accepted rung=${meta.rung} docs=${meta.doc_count ?? "?"}]`;
    }
    return `[${ts}] ${stage}/${status}${detail}: ${msg}`;
}
```

---

**`SOLUTIONS.md`**

Append the following entry:

```markdown
## Search Quality: Accordion Model with Era Vocabulary

**Problem**
Historical manuscript claims were routed to wrong source families (e-commerce
claims hitting macro-stat APIs). Queries used only modern vocabulary and missed
the historical record that used period terminology. Zero-result queries were
logged and abandoned with no systematic broadening.

**Root Cause**
`_claim_routing_profile` used keyword regex that missed commerce/platform/retail
vocabulary, routing claims to `OTHER/MIXED` at 0.46 confidence. `_clean_queries`
filtered existing queries but generated no era-equivalent vocabulary. No backoff
existed that could recover from zero results by trying related period terms.

**Solution**
New module `app/layers/search_policy.py` implements the accordion model:
1. One LLM call per gap (temperature=0, ~25s timeout) generates a `SynonymRing`
   with three vocabulary drift types (terminology_shifts, institutional_names,
   era_modifiers) plus a four-rung `AccordionLadder` with {PRIMARY} templates.
2. `get_accordion_move` drives execution: lateral through synonyms at current
   scope before widening to the next rung. Five actions: accept, lateral, widen,
   tighten, exhausted.
3. Synonym ring and ladder stored on `gap.query_ladder` for auditability and
   retry without re-calling the LLM.
4. Heuristic fallback (empty synonym ring, regex classifier) on Ollama failure.
5. All accordion state emitted as structured log events, visible in UI run log.

**Notes / Follow-up**
- `era_start`/`era_end` extracted by LLM and stored on `SynonymRing` but not
  yet passed to adapters as date-range filters. Follow-up ticket.
- `ORCH_PULL_NOISE_THRESHOLD` currently global; per-source thresholds follow-up.
- Subject heading pivot and archival finding-aid sources bracketed for future sprint.
```

### Files the agent must NOT touch

```
app/layers/analysis.py
app/layers/ingest.py
app/layers/fit.py
app/pipeline.py
app/store.py
app/main.py
app/library_profiles.default.json
```

### Critical implementation note for the agent

`get_accordion_move` is a **pure stateless function**. It does not track which
rung or synonym was last tried. The pull layer must maintain `current_rung` and
`current_synonym_idx` as explicit mutable loop variables per gap×source execution.
Do not attempt to infer rung or synonym index from the query string itself —
query strings from different rungs can overlap when synonyms are short. Initialize
the loop state to `("constrained", 0)` and update it from `move.rung` and
`move.synonym_idx` on every iteration.

---

## 6. Implementation Steps

Apply in this order. Each step is independently deployable and verifiable.

**Step 1 — Place new files**

```
app/layers/search_policy.py      (from search_policy.py)
app/tests/test_search_policy.py  (from test_search_policy.py)
```

Run the existing test suite immediately:

```bash
python -m pytest app/tests -q
```

The new module has no app imports and cannot break anything at this stage.
The 49 new tests should all pass.

**Step 2 — Patch `contracts.py`**

Add `query_ladder` field to `PlannedGap`. Run tests again. No behavioral change
at this step — the field is empty on all existing gaps.

**Step 3 — Patch `reflection.py`**

Replace `_claim_routing_profile` and `_clean_queries`. Update the call site in
`_apply_routing_policy` to pass `settings`. Remove the old `_ARCHIVAL_SUFFIX`
dict and `_query_quality_score` function. Run tests.

Key behavioral change: planning now makes one Ollama call per gap. Setting
`ORCH_REFLECTION_USE_OLLAMA=false` skips this and runs the heuristic instead.

**Step 4 — Patch `pull.py` and `config.py`**

Add `pull_noise_threshold` to settings. Replace the inner query loop with
`_execute_with_accordion`. Run tests. Verify in UI that plan cards show the
ladder and that run logs show accordion move events.

**Step 5 — Patch `static/index.html`**

Apply plan card and `summarizeEvent` patches. Verify in browser that plan cards
show the synonym ring categories, rung templates, and era range; and that log
rows show `[lateral rung=constrained syn=1 docs=0]` style annotations.

**Step 6 — Append SOLUTIONS.md entry, rebuild Docker**

```bash
python -m pytest app/tests -q
docker compose build && docker compose up -d
```

---

## 7. Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `ORCH_PULL_NOISE_THRESHOLD` | `50` | Results above this count trigger tighten move |
| `ORCH_REFLECTION_USE_OLLAMA` | `true` | Controls both reflection LLM and classify LLM |
| `ORCH_REFLECTION_MODEL` | `qwen2.5:7b` | Model for both reflection and classification |
| `ORCH_REFLECTION_TIMEOUT_SECONDS` | `120` | Classify call uses `min(this, 25)` per gap |

No new credentials or API keys are required. All existing env vars are preserved.

---

## 8. Sources

Harvard Library. "Exploring Your Topic." *Library Research Guide for History.*
Faculty of Arts & Sciences Libraries, Harvard University.
https://guides.library.harvard.edu/history/exploring

Harvard Library. "Finding Online Sources: Detailed Instructions." *Library
Research Guide for History.* Faculty of Arts & Sciences Libraries, Harvard
University. https://guides.library.harvard.edu/history/detailed

Stanford University Libraries. "Search and Find."
https://library.stanford.edu/services/search-and-find

Stanford University Libraries. "Search Tools." *General Library Search Tools
Guide.* https://guides.library.stanford.edu/search-services