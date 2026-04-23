"""Historian Search Policy Layer — Accordion Model.

Core idea: historical records use the vocabulary of their era, not the author's.
A search on modern terms alone will systematically miss primary material.

The accordion model:
  1. LLM generates a SYNONYM RING per claim — three distinct vocabulary types:
       - terminology_shifts:  what the concept was called in the era
       - institutional_names: how organizations named themselves or were named
       - era_modifiers:       adjectives/qualifiers used in period sources
  2. Each RUNG of the ladder (constrained → contextual → broad → fallback)
     is executed across up to SYNONYM_CAP variants before moving to a wider rung.
  3. On zero results:  move laterally through synonyms first, then widen rung
  4. On noisy results: tighten by moving to constrained rung
  5. Results from synonym variants at the same rung are MERGED up to a cap

The synonym ring is stored on the gap (auditable, reusable on retry).
The LLM decides how many variants to generate per type; we cap at execution time.

LLM call: one prompt per gap, temperature=0, ~300 tokens out.
Heuristic fallback: no synonym ring, plain ladder only (liveness guarantee).

Public API:
    classify_and_build_ladder(chapter, claim_text, *, use_llm, model, ...) ->
        (claim_kind, evidence_need, confidence, AccordionLadder)

    get_accordion_move(ladder, current_rung, current_synonym_idx, result_count) ->
        AccordionMove

    query_quality_score(query) -> float
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from layers.llm_client import LLMClient, LLMProvider


# ---------------------------------------------------------------------------
# Constants (mirror contracts.py enum values — no circular import)
# ---------------------------------------------------------------------------

CLAIM_HISTORICAL_NARRATIVE  = "historical_narrative"
CLAIM_QUANTITATIVE_MACRO    = "quantitative_macro"
CLAIM_QUANTITATIVE_LABOR    = "quantitative_labor"
CLAIM_LEGAL_REGULATORY      = "legal_regulatory"
CLAIM_COMPANY_OPERATIONS    = "company_operations"
CLAIM_BIOGRAPHICAL          = "biographical"
CLAIM_OTHER                 = "other"

EVIDENCE_SCHOLARLY_SECONDARY = "scholarly_secondary"
EVIDENCE_PRIMARY_SOURCE      = "primary_source"
EVIDENCE_OFFICIAL_STATISTICS = "official_statistics"
EVIDENCE_LEGAL_TEXT          = "legal_text"
EVIDENCE_NEWS_ARCHIVE        = "news_archive"
EVIDENCE_MIXED               = "mixed"

VALID_CLAIM_KINDS = {
    CLAIM_HISTORICAL_NARRATIVE, CLAIM_QUANTITATIVE_MACRO, CLAIM_QUANTITATIVE_LABOR,
    CLAIM_LEGAL_REGULATORY, CLAIM_COMPANY_OPERATIONS, CLAIM_BIOGRAPHICAL, CLAIM_OTHER,
}
VALID_EVIDENCE_NEEDS = {
    EVIDENCE_SCHOLARLY_SECONDARY, EVIDENCE_PRIMARY_SOURCE, EVIDENCE_OFFICIAL_STATISTICS,
    EVIDENCE_LEGAL_TEXT, EVIDENCE_NEWS_ARCHIVE, EVIDENCE_MIXED,
}

# Archival vocabulary appended to constrained queries (Harvard primary-source strategy)
_ARCHIVAL_SUFFIX: Dict[str, str] = {
    EVIDENCE_SCHOLARLY_SECONDARY: "scholarly article journal history",
    EVIDENCE_PRIMARY_SOURCE:      "archives correspondence diaries manuscripts sources",
    EVIDENCE_LEGAL_TEXT:          "statute act regulation law court ruling",
    EVIDENCE_NEWS_ARCHIVE:        "newspaper periodical historical press",
    EVIDENCE_OFFICIAL_STATISTICS: "statistics data time series official",
    EVIDENCE_MIXED:               "historical evidence primary source archive",
}

DEFAULT_SYNONYM_CAP = 3
_CACHE_SCHEMA_VERSION = "v1"


# ---------------------------------------------------------------------------
# SynonymRing
# ---------------------------------------------------------------------------

@dataclass
class SynonymRing:
    """Era-equivalent vocabulary for one claim, organized by drift type.

    terminology_shifts:  What the concept was actually called in the era.
        "e-commerce" in a 1990s source might appear as "electronic commerce",
        "online retailing", "internet shopping", or "direct marketing".

    institutional_names: How the organization appeared in period sources —
        former names, abbreviations, trade names, or common shorthand.
        "NetMarket" might appear as "Net Market Inc", "Netmarket.com",
        or "the Internet Shopping Network".

    era_modifiers:       Adjectives and qualifiers that period journalists,
        catalogers, and academics attached to the concept. These appear in
        article titles, newspaper headlines, and subject headings.
        "interactive commerce", "computer-mediated trade", "cyberspace retail".

    era_start / era_end: Year bounds of the claim's historical period.
        Used to add date-range hints to constrained queries.
    """
    terminology_shifts:  List[str] = field(default_factory=list)
    institutional_names: List[str] = field(default_factory=list)
    era_modifiers:       List[str] = field(default_factory=list)
    era_start:           Optional[int] = None
    era_end:             Optional[int] = None

    def all_variants(self) -> List[str]:
        """Flat deduplicated list across all three variant types."""
        seen: set[str] = set()
        out: List[str] = []
        for term in (
            self.terminology_shifts
            + self.institutional_names
            + self.era_modifiers
        ):
            key = term.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(term.strip())
        return out

    def is_empty(self) -> bool:
        return not (self.terminology_shifts or self.institutional_names or self.era_modifiers)

    def to_dict(self) -> Dict[str, object]:
        return {
            "terminology_shifts":  self.terminology_shifts,
            "institutional_names": self.institutional_names,
            "era_modifiers":       self.era_modifiers,
            "era_start":           self.era_start,
            "era_end":             self.era_end,
        }

    @staticmethod
    def from_dict(d: Dict[str, object]) -> "SynonymRing":
        def _strs(key: str) -> List[str]:
            raw = d.get(key, [])
            return [str(s).strip() for s in raw if str(s).strip()] if isinstance(raw, list) else []
        def _int_or_none(key: str) -> Optional[int]:
            v = d.get(key)
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        return SynonymRing(
            terminology_shifts=_strs("terminology_shifts"),
            institutional_names=_strs("institutional_names"),
            era_modifiers=_strs("era_modifiers"),
            era_start=_int_or_none("era_start"),
            era_end=_int_or_none("era_end"),
        )


# ---------------------------------------------------------------------------
# AccordionLadder
# ---------------------------------------------------------------------------

@dataclass
class AccordionLadder:
    """Four-rung ladder where each rung is executed across a synonym ring.

    Rung templates use {PRIMARY} as a substitution placeholder for the
    primary entity term. At execution time, each synonym variant is
    substituted in turn (up to synonym_cap) before moving to the next rung.

    Execution model (accordion):
        constrained rung:
            try primary term → try synonym_1 → try synonym_2 → (up to cap)
            if any yield results: merge and stop widening
        contextual rung:
            same lateral sweep if constrained exhausted
        broad rung:
            same
        fallback rung:
            last resort; no synonym substitution (chapter keywords only)

    Tightening: if results > noise_threshold, jump directly to constrained rung.
    Exhausted: all rungs and synonyms tried → mark gap needs_review.
    """
    constrained:  str = ""
    contextual:   str = ""
    broad:        str = ""
    fallback:     str = ""

    primary_term:  str = ""
    synonym_ring:  SynonymRing = field(default_factory=SynonymRing)

    claim_kind:        str = CLAIM_OTHER
    evidence_need:     str = EVIDENCE_MIXED
    archival_suffix:   str = ""
    generation_method: str = "llm"

    def queries_for_rung(self, rung: str, synonym_cap: int = DEFAULT_SYNONYM_CAP) -> List[str]:
        """Return up to synonym_cap query strings for a rung, with synonyms substituted.

        The primary term is always index 0. Synonym variants follow in the order
        returned by SynonymRing.all_variants().
        """
        template = getattr(self, rung, "") or ""
        if not template:
            return []

        # Build variant list: primary term first, then era synonyms
        all_variants = [self.primary_term] + self.synonym_ring.all_variants()
        seen_v: set[str] = set()
        variants: List[str] = []
        for v in all_variants:
            key = v.strip().lower()
            if key and key not in seen_v:
                seen_v.add(key)
                variants.append(v.strip())

        queries: List[str] = []
        seen_q: set[str] = set()
        for variant in variants[:synonym_cap]:
            if "{PRIMARY}" in template:
                q = template.replace("{PRIMARY}", variant).strip()
            else:
                # Template has no placeholder — primary term query as-is,
                # synonym queries append the variant as an additional term
                q = template.strip() if variant == self.primary_term else f"{template} {variant}".strip()
            key = q.lower()
            if key and key not in seen_q:
                seen_q.add(key)
                queries.append(q)
        return queries

    def rung_order(self) -> List[str]:
        return ["constrained", "contextual", "broad", "fallback"]

    def to_dict(self) -> Dict[str, object]:
        """Serialize for storage on PlannedGap.query_ladder."""
        return {
            "constrained":       self.constrained,
            "contextual":        self.contextual,
            "broad":             self.broad,
            "fallback":          self.fallback,
            "primary_term":      self.primary_term,
            "synonym_ring":      self.synonym_ring.to_dict(),
            "claim_kind":        self.claim_kind,
            "evidence_need":     self.evidence_need,
            "archival_suffix":   self.archival_suffix,
            "generation_method": self.generation_method,
        }

    @staticmethod
    def from_dict(d: Dict[str, object]) -> "AccordionLadder":
        ring_raw = d.get("synonym_ring", {})
        ring = SynonymRing.from_dict(ring_raw) if isinstance(ring_raw, dict) else SynonymRing()
        return AccordionLadder(
            constrained=str(d.get("constrained", "")),
            contextual=str(d.get("contextual", "")),
            broad=str(d.get("broad", "")),
            fallback=str(d.get("fallback", "")),
            primary_term=str(d.get("primary_term", "")),
            synonym_ring=ring,
            claim_kind=str(d.get("claim_kind", CLAIM_OTHER)),
            evidence_need=str(d.get("evidence_need", EVIDENCE_MIXED)),
            archival_suffix=str(d.get("archival_suffix", "")),
            generation_method=str(d.get("generation_method", "llm")),
        )


# ---------------------------------------------------------------------------
# AccordionMove
# ---------------------------------------------------------------------------

@dataclass
class AccordionMove:
    """Decision returned by get_accordion_move after one query attempt.

    action:
        "lateral"   — try next synonym variant at the same rung
        "widen"     — move to next (broader) rung, reset to primary term
        "tighten"   — jump to constrained rung (noisy results)
        "accept"    — result count is good, stop
        "exhausted" — all rungs and synonyms tried, mark needs_review

    next_queries:   queries to execute (empty if accept/exhausted)
    rung:           rung name for next_queries
    synonym_idx:    synonym index to resume from (0 = primary term)
    """
    action:       str
    next_queries: List[str]
    rung:         str
    synonym_idx:  int
    reason:       str


def get_accordion_move(
    ladder: AccordionLadder,
    current_rung: str,
    current_synonym_idx: int,
    result_count: int,
    noise_threshold: int = 50,
    min_accept_results: int = 1,
    synonym_cap: int = DEFAULT_SYNONYM_CAP,
) -> AccordionMove:
    """Decide the next accordion move based on result count at current position.

    Zero results + more synonyms remain → lateral (try next synonym, same rung)
    Zero results + synonyms exhausted   → widen (next rung, primary term)
    Too many results                    → tighten (jump to constrained rung)
    Acceptable count                    → accept
    All rungs exhausted                 → exhausted (needs_review)
    """
    rung_order = ladder.rung_order()

    # Acceptable. Allow callers to require a minimum evidence floor
    # before accepting and stopping ladder expansion.
    if min_accept_results <= result_count <= noise_threshold:
        return AccordionMove(
            action="accept", next_queries=[], rung=current_rung,
            synonym_idx=current_synonym_idx,
            reason=f"{result_count} results accepted at rung={current_rung} synonym={current_synonym_idx}",
        )

    # Noisy — tighten
    if result_count > noise_threshold:
        if current_rung == "constrained":
            return AccordionMove(
                action="accept", next_queries=[], rung="constrained",
                synonym_idx=current_synonym_idx,
                reason=f"{result_count} results; already at constrained rung, accepting",
            )
        constrained_queries = ladder.queries_for_rung("constrained", synonym_cap)
        return AccordionMove(
            action="tighten",
            next_queries=constrained_queries[:1],
            rung="constrained", synonym_idx=0,
            reason=f"{result_count} results exceeds noise threshold; tightening to constrained rung",
        )

    # Insufficient (non-zero but below min_accept_results) is treated as
    # "continue searching" rather than immediate accept.
    insufficient = 0 < result_count < max(1, min_accept_results)

    # Zero/insufficient results — try lateral synonym first
    next_synonym_idx = current_synonym_idx + 1
    rung_queries = ladder.queries_for_rung(current_rung, synonym_cap)
    if next_synonym_idx < len(rung_queries):
        return AccordionMove(
            action="lateral",
            next_queries=[rung_queries[next_synonym_idx]],
            rung=current_rung,
            synonym_idx=next_synonym_idx,
            reason=(
                f"{result_count} results at rung={current_rung} synonym={current_synonym_idx}; "
                f"trying synonym variant {next_synonym_idx}"
            ),
        )

    # Synonyms exhausted at this rung — widen
    try:
        current_rung_idx = rung_order.index(current_rung)
    except ValueError:
        current_rung_idx = -1
    next_rung_idx = current_rung_idx + 1

    if next_rung_idx >= len(rung_order):
        return AccordionMove(
            action="exhausted", next_queries=[], rung="exhausted", synonym_idx=0,
            reason="all rungs and synonyms exhausted; marking needs_review",
        )

    next_rung = rung_order[next_rung_idx]
    next_queries = ladder.queries_for_rung(next_rung, synonym_cap)
    if not next_queries:
        return AccordionMove(
            action="exhausted", next_queries=[], rung="exhausted", synonym_idx=0,
            reason=f"next rung '{next_rung}' has no queries; exhausted",
        )

    return AccordionMove(
        action="widen",
        next_queries=next_queries[:1],
        rung=next_rung, synonym_idx=0,
        reason=(
            f"{'insufficient results' if insufficient else 'synonyms exhausted'} "
            f"at rung={current_rung}; widening to rung={next_rung}"
        ),
    )


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
You are a research librarian. Given a manuscript gap claim, do three things:
1. Classify the claim type and evidence needed.
2. Generate era-equivalent vocabulary — what would sources from the period
   actually say, not what the author says today.
3. Build a four-rung query ladder using {{PRIMARY}} as a placeholder for the
   primary entity/term so synonym variants can be substituted at runtime.

Return ONLY a JSON object. No preamble, no explanation, no markdown fences.

{{
  "claim_kind": "<historical_narrative|quantitative_macro|quantitative_labor|legal_regulatory|company_operations|biographical|other>",
  "evidence_need": "<scholarly_secondary|primary_source|official_statistics|legal_text|news_archive|mixed>",
  "confidence": <0.0-1.0>,
  "primary_term": "<the single most searchable entity or concept in the claim>",
  "era_start": <year integer or null>,
  "era_end": <year integer or null>,
  "synonym_ring": {{
    "terminology_shifts": [
      "<what the concept was actually called in the era>",
      "<another period term if genuinely different>"
    ],
    "institutional_names": [
      "<how the organization appeared in period sources — former names, abbreviations, trade names>"
    ],
    "era_modifiers": [
      "<adjective or qualifier used in period headlines, article titles, or catalog subject headings>"
    ]
  }},
  "query_constrained": "<{{PRIMARY}} + era context + archival vocabulary — most precise>",
  "query_contextual":  "<{{PRIMARY}} + era context — primary working query>",
  "query_broad":       "<{{PRIMARY}} alone — maximum recall anchor>",
  "query_fallback":    "<chapter heading keywords only — last resort, no {{PRIMARY}}>"
}}

Classification rules:
- company_operations: business, retail, e-commerce, platform, marketplace, startup.
- historical_narrative: cultural/social/political history without explicit numbers.
- quantitative_labor: ONLY when claim has explicit labor/wage/employment statistics.
- quantitative_macro: ONLY when claim has explicit GDP/CPI/inflation numbers.
- legal_regulatory: law, statute, regulation, court ruling, policy.
- biographical: a specific person's life, career, relationships.
- evidence_need=news_archive for company_operations (business events in newspapers).
- evidence_need=scholarly_secondary for historical_narrative.
- evidence_need=official_statistics ONLY for quantitative claims.

Archival vocabulary for query_constrained:
  scholarly_secondary → "scholarly article journal history"
  primary_source      → "archives correspondence diaries manuscripts"
  news_archive        → "newspaper periodical historical press"
  legal_text          → "statute act regulation law court"
  official_statistics → "statistics data time series official"

Synonym ring rules:
- Generate as many terminology_shifts as genuinely apply — do not pad with near-duplicates.
- Only include institutional_names if the claim mentions a specific organization.
- era_modifiers: think about words a journalist or cataloger in the era would use.
- All variants must work as standalone search terms, not as explanations.
- If institutional_names or era_modifiers do not apply, use empty lists.

CHAPTER: {chapter}
CLAIM: {claim_text}
"""


def _call_llm_for_classification(prompt: str, model: str, base_url: str, timeout_seconds: int) -> Dict[str, object]:
    """Call the configured LLM for gap classification and return parsed JSON."""
    client = LLMClient(
        provider=LLMProvider.OLLAMA,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        temperature=0.0,  # deterministic classification
    )
    return client.complete_json(prompt=prompt)


def _parse_llm_response(data: Dict[str, object]) -> Tuple[str, str, float, str, SynonymRing, Dict[str, str]]:
    claim_kind    = str(data.get("claim_kind",    CLAIM_OTHER)).strip().lower()
    evidence_need = str(data.get("evidence_need", EVIDENCE_MIXED)).strip().lower()
    if claim_kind    not in VALID_CLAIM_KINDS:    claim_kind    = CLAIM_OTHER
    if evidence_need not in VALID_EVIDENCE_NEEDS: evidence_need = EVIDENCE_MIXED

    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.7))))
    except (TypeError, ValueError):
        conf = 0.7

    primary_term = str(data.get("primary_term", "")).strip()

    ring_raw = data.get("synonym_ring", {})
    ring_raw = ring_raw if isinstance(ring_raw, dict) else {}

    def _strs(key: str) -> List[str]:
        raw = ring_raw.get(key, [])
        return [str(s).strip() for s in raw if str(s).strip()] if isinstance(raw, list) else []

    def _int_or_none(key: str) -> Optional[int]:
        v = data.get(key)
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    ring = SynonymRing(
        terminology_shifts=_strs("terminology_shifts"),
        institutional_names=_strs("institutional_names"),
        era_modifiers=_strs("era_modifiers"),
        era_start=_int_or_none("era_start"),
        era_end=_int_or_none("era_end"),
    )

    queries = {
        "constrained": str(data.get("query_constrained", "")).strip(),
        "contextual":  str(data.get("query_contextual",  "")).strip(),
        "broad":       str(data.get("query_broad",       "")).strip(),
        "fallback":    str(data.get("query_fallback",    "")).strip(),
    }

    return claim_kind, evidence_need, conf, primary_term, ring, queries


def _cache_key(chapter: str, claim_text: str, model: str) -> str:
    raw = f"{_CACHE_SCHEMA_VERSION}|{model.strip().lower()}|{chapter.strip().lower()}|{claim_text.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _cache_read(cache_dir: Optional[Path], key: str) -> Optional[Dict[str, object]]:
    if cache_dir is None:
        return None
    path = cache_dir / f"{key}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def _cache_write(cache_dir: Optional[Path], key: str, payload: Dict[str, object]) -> None:
    if cache_dir is None:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{key}.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        return


def _apply_optional_date_hints(ladder: AccordionLadder) -> AccordionLadder:
    """Append year hints only when available and not already present.

    Terms still drive retrieval quality; year hints are additive and optional.
    """
    start = ladder.synonym_ring.era_start
    end = ladder.synonym_ring.era_end
    if start is None and end is None:
        return ladder

    hints = []
    if start is not None:
        hints.append(str(start))
    if end is not None and end != start:
        hints.append(str(end))
    if not hints:
        return ladder
    hint_block = " ".join(hints)

    for field in ("constrained", "contextual"):
        value = getattr(ladder, field, "")
        if not value:
            continue
        if any(str(year) in value for year in hints):
            continue
        setattr(ladder, field, f"{value} {hint_block}".strip())
    return ladder


# ---------------------------------------------------------------------------
# Heuristic fallback (no synonym ring — liveness when LLM unavailable)
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "there",
    "about", "without", "where", "which", "what", "been", "were", "have",
    "has", "had", "your", "their", "them", "they", "could", "would", "should",
    "argument", "evidence", "claim", "claims", "section", "chapter",
    "manuscript", "source", "sources", "citation", "unsupported",
}
_TOKEN_RE  = re.compile(r"[A-Za-z][A-Za-z\-']{2,}")
_YEAR_RE   = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")
_PROPER_RE = re.compile(r"(?<!\. )(?<!\n)[A-Z][a-z]{2,}")

_HEURISTIC_RULES: List[Tuple[str, str, str, float]] = [
    (r"\b(law|regulation|statute|act|policy|court|ruling|legal|compliance)\b",
     CLAIM_LEGAL_REGULATORY, EVIDENCE_LEGAL_TEXT, 0.80),
    (r"\b(worker|labor|labour|employment|unemployment|injury|wage|payroll|strike|union)\b",
     CLAIM_QUANTITATIVE_LABOR, EVIDENCE_OFFICIAL_STATISTICS, 0.82),
    (r"\b(percent|rate|gdp|inflation|cpi|index|\d+%|\$\s?\d+|billion|trillion)\b",
     CLAIM_QUANTITATIVE_MACRO, EVIDENCE_OFFICIAL_STATISTICS, 0.80),
    (r"\b(born|father|mother|childhood|biograph|memoir|founder|life of)\b",
     CLAIM_BIOGRAPHICAL, EVIDENCE_PRIMARY_SOURCE, 0.72),
    (r"\b(company|firm|corporation|startup|platform|retail|e-commerce|ecommerce|"
     r"marketplace|warehouse|logistics|store|brand|merchant|commerce|consumer|"
     r"netmarket|amazon|ebay|product|price|revenue|sales|market)\b",
     CLAIM_COMPANY_OPERATIONS, EVIDENCE_NEWS_ARCHIVE, 0.68),
    (r"\b(history|historical|century|era|period|decade|archive|transformation|"
     r"revolution|emergence|rise of|decline|origins|movement|society|culture|"
     r"community|industry|trade|migration)\b",
     CLAIM_HISTORICAL_NARRATIVE, EVIDENCE_SCHOLARLY_SECONDARY, 0.66),
]


def _heuristic_classify(chapter: str, claim_text: str) -> Tuple[str, str, float]:
    text = f"{chapter} {claim_text}".lower()
    for pattern, ck, en, conf in _HEURISTIC_RULES:
        if re.search(pattern, text):
            return (ck, en, conf)
    return (CLAIM_OTHER, EVIDENCE_MIXED, 0.40)


def _extract_tokens(text: str, limit: int = 6) -> List[str]:
    tokens = _TOKEN_RE.findall(text)
    proper, common, seen = [], [], set()
    for tok in tokens:
        low = tok.lower()
        if low in _STOPWORDS or low in seen or len(low) < 3:
            continue
        seen.add(low)
        (proper if tok[0].isupper() else common).append(tok)
    return (proper + common)[:limit]


def _heuristic_accordion(
    chapter: str, claim_text: str, claim_kind: str, evidence_need: str
) -> AccordionLadder:
    full     = f"{chapter} {claim_text}"
    tokens   = _extract_tokens(full)
    proper   = [t for t in _PROPER_RE.findall(full) if t.lower() not in _STOPWORDS][:3]
    years    = _YEAR_RE.findall(full)[:1]
    archival = _ARCHIVAL_SUFFIX.get(evidence_need, _ARCHIVAL_SUFFIX[EVIDENCE_MIXED])

    primary_term = proper[0] if proper else (tokens[0] if tokens else chapter.split()[0] if chapter else "claim")
    broad_toks   = proper[:2] or tokens[:2]
    broad        = " ".join(broad_toks) or primary_term

    ctx_toks   = (proper[:2] + years + [v for v in tokens if v.lower() not in {t.lower() for t in proper}])[:4]
    contextual = " ".join(ctx_toks) or broad
    constrained = f"{contextual} {archival}".strip()

    fallback_toks = _extract_tokens(chapter, limit=3)
    fallback = " ".join(fallback_toks) or broad

    return AccordionLadder(
        constrained=constrained,
        contextual=contextual,
        broad=broad,
        fallback=fallback,
        primary_term=primary_term,
        synonym_ring=SynonymRing(),  # empty — no era vocabulary without LLM
        claim_kind=claim_kind,
        evidence_need=evidence_need,
        archival_suffix=archival,
        generation_method="heuristic",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_LOW_QUALITY_RE = re.compile(
    r"\b(split claims|tie .{0,20} evidence|needs evidence|"
    r"improve manuscript|refine .{0,20} claim)\b"
)


def classify_and_build_ladder(
    chapter: str,
    claim_text: str,
    *,
    use_llm: bool = True,
    model: str = "qwen2.5:7b",
    base_url: str = "http://127.0.0.1:11434",
    timeout_seconds: int = 25,
    existing_queries: Optional[List[str]] = None,
    cache_dir: Optional[Path] = None,
) -> Tuple[str, str, float, AccordionLadder]:
    """Classify claim and build accordion ladder. LLM-first, heuristic on failure.

    Returns:
        (claim_kind, evidence_need, confidence, AccordionLadder)

    The LLM generates the synonym ring and rung templates in a single call.
    The AccordionLadder.to_dict() result should be stored on gap.query_ladder
    for auditability, UI display, and retry without re-calling the LLM.

    Falls back to a plain heuristic ladder (no synonym ring) on any Ollama
    failure, ensuring the pipeline never blocks.
    """
    claim_kind    = CLAIM_OTHER
    evidence_need = EVIDENCE_MIXED
    confidence    = 0.40
    ladder: Optional[AccordionLadder] = None

    if use_llm:
        cache_key = _cache_key(chapter, claim_text, model)
        cached = _cache_read(cache_dir, cache_key)
        if cached:
            try:
                claim_kind = str(cached.get("claim_kind", CLAIM_OTHER))
                evidence_need = str(cached.get("evidence_need", EVIDENCE_MIXED))
                confidence = float(cached.get("confidence", 0.7))
                ladder_data = cached.get("ladder", {})
                if isinstance(ladder_data, dict):
                    ladder = AccordionLadder.from_dict(ladder_data)
            except Exception:  # noqa: BLE001
                ladder = None

        try:
            if ladder is None:
                prompt = _CLASSIFY_PROMPT.format(
                    chapter=chapter.strip()[:300],
                    claim_text=claim_text.strip()[:400],
                )
                data = _call_llm_for_classification(prompt, model, base_url, timeout_seconds)
                claim_kind, evidence_need, confidence, primary_term, ring, queries = _parse_llm_response(data)
                archival = _ARCHIVAL_SUFFIX.get(evidence_need, _ARCHIVAL_SUFFIX[EVIDENCE_MIXED])
                ladder = AccordionLadder(
                    constrained=queries.get("constrained", ""),
                    contextual=queries.get("contextual", ""),
                    broad=queries.get("broad", ""),
                    fallback=queries.get("fallback", ""),
                    primary_term=primary_term,
                    synonym_ring=ring,
                    claim_kind=claim_kind,
                    evidence_need=evidence_need,
                    archival_suffix=archival,
                    generation_method="llm",
                )
                _cache_write(
                    cache_dir,
                    cache_key,
                    {
                        "claim_kind": claim_kind,
                        "evidence_need": evidence_need,
                        "confidence": confidence,
                        "ladder": ladder.to_dict(),
                    },
                )
        except Exception:  # noqa: BLE001
            pass

    if ladder is None:
        claim_kind, evidence_need, confidence = _heuristic_classify(chapter, claim_text)
        ladder = _heuristic_accordion(chapter, claim_text, claim_kind, evidence_need)

    if existing_queries:
        usable = [q.strip() for q in existing_queries if _is_usable_query(q)]
        if usable:
            ladder.constrained = sorted(usable, key=len, reverse=True)[0]

    ladder = _apply_optional_date_hints(ladder)
    return claim_kind, evidence_need, confidence, ladder


def _is_usable_query(query: str) -> bool:
    q = (query or "").strip().lower()
    return len(q.split()) >= 3 and not _LOW_QUALITY_RE.search(q)


def query_quality_score(query: str) -> float:
    raw = (query or "").strip()
    if not raw:
        return 0.0
    score = 1.0
    if len(raw) < 12:        score -= 0.35
    if len(raw.split()) < 3: score -= 0.30
    if _LOW_QUALITY_RE.search(raw.lower()): score -= 0.70
    return max(0.0, min(1.0, score))
