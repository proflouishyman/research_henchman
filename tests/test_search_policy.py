"""Tests for accordion search policy.

Covers:
- SynonymRing: construction, all_variants(), serialization round-trip
- AccordionLadder: queries_for_rung() with/without {PRIMARY}, to/from_dict
- get_accordion_move(): all five actions (lateral, widen, tighten, accept, exhausted)
- classify_and_build_ladder(): LLM path (mocked), heuristic fallback, existing queries
- Heuristic classifier: claim routing correctness
- query_quality_score()
"""

from __future__ import annotations

import pytest

from app.layers import search_policy
from app.layers.search_policy import (
    CLAIM_BIOGRAPHICAL,
    CLAIM_COMPANY_OPERATIONS,
    CLAIM_HISTORICAL_NARRATIVE,
    CLAIM_LEGAL_REGULATORY,
    CLAIM_OTHER,
    CLAIM_QUANTITATIVE_LABOR,
    CLAIM_QUANTITATIVE_MACRO,
    EVIDENCE_LEGAL_TEXT,
    EVIDENCE_MIXED,
    EVIDENCE_NEWS_ARCHIVE,
    EVIDENCE_OFFICIAL_STATISTICS,
    EVIDENCE_PRIMARY_SOURCE,
    EVIDENCE_SCHOLARLY_SECONDARY,
    AccordionLadder,
    AccordionMove,
    SynonymRing,
    _heuristic_accordion,
    _heuristic_classify,
    classify_and_build_ladder,
    get_accordion_move,
    query_quality_score,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ring(
    terms: list = None,
    institutions: list = None,
    modifiers: list = None,
    era_start: int = None,
    era_end: int = None,
) -> SynonymRing:
    return SynonymRing(
        terminology_shifts=terms or [],
        institutional_names=institutions or [],
        era_modifiers=modifiers or [],
        era_start=era_start,
        era_end=era_end,
    )


def _ladder_with_ring(ring: SynonymRing = None, synonym_cap: int = 3) -> AccordionLadder:
    """Standard test ladder with {PRIMARY} templates."""
    return AccordionLadder(
        constrained="{PRIMARY} 1994 online commerce newspaper periodical historical press",
        contextual="{PRIMARY} 1994 online commerce",
        broad="{PRIMARY}",
        fallback="commerce retail history",
        primary_term="NetMarket",
        synonym_ring=ring or _ring(),
        claim_kind=CLAIM_COMPANY_OPERATIONS,
        evidence_need=EVIDENCE_NEWS_ARCHIVE,
        archival_suffix="newspaper periodical historical press",
        generation_method="llm",
    )


# ---------------------------------------------------------------------------
# SynonymRing
# ---------------------------------------------------------------------------

class TestSynonymRing:
    def test_all_variants_deduplicates(self):
        ring = _ring(
            terms=["electronic commerce", "online retailing"],
            institutions=["electronic commerce"],  # duplicate
            modifiers=["interactive"],
        )
        variants = ring.all_variants()
        assert variants.count("electronic commerce") == 1
        assert "online retailing" in variants
        assert "interactive" in variants

    def test_all_variants_order(self):
        """terminology_shifts come before institutional_names before era_modifiers."""
        ring = _ring(
            terms=["electronic commerce"],
            institutions=["Net Market Inc"],
            modifiers=["interactive commerce"],
        )
        v = ring.all_variants()
        assert v.index("electronic commerce") < v.index("Net Market Inc")
        assert v.index("Net Market Inc") < v.index("interactive commerce")

    def test_is_empty_true(self):
        assert _ring().is_empty()

    def test_is_empty_false(self):
        assert not _ring(terms=["electronic commerce"]).is_empty()

    def test_serialization_round_trip(self):
        ring = _ring(
            terms=["electronic commerce", "online retailing"],
            institutions=["Net Market Inc"],
            modifiers=["interactive"],
            era_start=1993,
            era_end=1999,
        )
        restored = SynonymRing.from_dict(ring.to_dict())
        assert restored.terminology_shifts == ring.terminology_shifts
        assert restored.institutional_names == ring.institutional_names
        assert restored.era_modifiers == ring.era_modifiers
        assert restored.era_start == 1993
        assert restored.era_end == 1999

    def test_from_dict_handles_missing_keys(self):
        ring = SynonymRing.from_dict({})
        assert ring.terminology_shifts == []
        assert ring.era_start is None


# ---------------------------------------------------------------------------
# AccordionLadder.queries_for_rung
# ---------------------------------------------------------------------------

class TestAccordionLadderQueriesForRung:
    def test_primary_term_is_first(self):
        ladder = _ladder_with_ring(_ring(terms=["electronic commerce"]))
        queries = ladder.queries_for_rung("contextual", synonym_cap=3)
        assert queries[0] == "NetMarket 1994 online commerce"

    def test_synonym_substituted_in_template(self):
        ladder = _ladder_with_ring(_ring(terms=["electronic commerce"]))
        queries = ladder.queries_for_rung("contextual", synonym_cap=3)
        assert any("electronic commerce" in q for q in queries)

    def test_synonym_cap_respected(self):
        ring = _ring(terms=["a", "b", "c", "d", "e"])
        ladder = _ladder_with_ring(ring)
        queries = ladder.queries_for_rung("contextual", synonym_cap=3)
        assert len(queries) <= 3

    def test_no_duplicates_in_queries(self):
        ring = _ring(terms=["NetMarket"])  # same as primary_term
        ladder = _ladder_with_ring(ring)
        queries = ladder.queries_for_rung("contextual", synonym_cap=3)
        lower = [q.lower() for q in queries]
        assert len(lower) == len(set(lower))

    def test_template_without_placeholder(self):
        """Template without {PRIMARY} — primary term query is unchanged,
        synonym queries append the variant."""
        ladder = AccordionLadder(
            contextual="commerce 1994 online",
            primary_term="NetMarket",
            synonym_ring=_ring(terms=["electronic commerce"]),
        )
        queries = ladder.queries_for_rung("contextual", synonym_cap=3)
        assert "commerce 1994 online" in queries
        assert any("electronic commerce" in q for q in queries)

    def test_empty_rung_returns_empty_list(self):
        ladder = AccordionLadder(contextual="", primary_term="x")
        assert ladder.queries_for_rung("contextual") == []

    def test_fallback_rung_no_synonym_substitution_needed(self):
        """Fallback should be chapter keywords — synonym substitution still safe."""
        ladder = _ladder_with_ring(_ring(terms=["electronic commerce"]))
        queries = ladder.queries_for_rung("fallback", synonym_cap=3)
        assert queries  # not empty

    def test_serialization_round_trip(self):
        ring = _ring(terms=["electronic commerce"], era_start=1993, era_end=1999)
        ladder = _ladder_with_ring(ring)
        restored = AccordionLadder.from_dict(ladder.to_dict())
        assert restored.constrained == ladder.constrained
        assert restored.primary_term == ladder.primary_term
        assert restored.synonym_ring.terminology_shifts == ["electronic commerce"]
        assert restored.synonym_ring.era_start == 1993


# ---------------------------------------------------------------------------
# get_accordion_move
# ---------------------------------------------------------------------------

class TestGetAccordionMove:
    def _ladder(self, has_synonyms: bool = True) -> AccordionLadder:
        ring = _ring(
            terms=["electronic commerce", "online retailing"],
        ) if has_synonyms else _ring()
        return _ladder_with_ring(ring)

    # --- Accept ---

    def test_accept_on_good_count(self):
        ladder = self._ladder()
        move = get_accordion_move(ladder, "constrained", 0, result_count=15)
        assert move.action == "accept"

    def test_low_nonzero_count_widens_when_below_min_accept(self):
        ladder = self._ladder(has_synonyms=False)
        move = get_accordion_move(
            ladder,
            "constrained",
            0,
            result_count=1,
            min_accept_results=2,
            synonym_cap=2,
        )
        assert move.action == "widen"
        assert move.rung == "contextual"

    def test_accept_exactly_at_noise_threshold(self):
        ladder = self._ladder()
        move = get_accordion_move(ladder, "contextual", 0, result_count=50, noise_threshold=50)
        assert move.action == "accept"

    # --- Lateral (synonym) ---

    def test_lateral_on_zero_results_with_synonyms(self):
        ladder = self._ladder(has_synonyms=True)
        move = get_accordion_move(ladder, "constrained", 0, result_count=0)
        assert move.action == "lateral"
        assert move.rung == "constrained"
        assert move.synonym_idx == 1

    def test_lateral_advances_synonym_index(self):
        ladder = self._ladder(has_synonyms=True)
        move = get_accordion_move(ladder, "contextual", 1, result_count=0)
        assert move.action == "lateral"
        assert move.synonym_idx == 2

    def test_lateral_has_one_query(self):
        ladder = self._ladder(has_synonyms=True)
        move = get_accordion_move(ladder, "constrained", 0, result_count=0)
        assert len(move.next_queries) == 1

    # --- Widen ---

    def test_widen_when_synonyms_exhausted(self):
        """With synonym_cap=3 and 2 synonyms, index 0 (primary) + 1 + 2 = 3 total.
        At synonym_idx=2 (last), next move should widen."""
        ring = _ring(terms=["electronic commerce", "online retailing"])
        ladder = _ladder_with_ring(ring)
        # synonym_cap=3 means queries_for_rung returns 3 items (indices 0,1,2)
        move = get_accordion_move(ladder, "constrained", 2, result_count=0, synonym_cap=3)
        assert move.action == "widen"
        assert move.rung == "contextual"
        assert move.synonym_idx == 0

    def test_widen_from_contextual_goes_to_broad(self):
        ring = _ring(terms=["electronic commerce"])
        ladder = _ladder_with_ring(ring)
        move = get_accordion_move(ladder, "contextual", 1, result_count=0, synonym_cap=2)
        assert move.action == "widen"
        assert move.rung == "broad"

    def test_widen_from_broad_goes_to_fallback(self):
        ring = _ring(terms=["electronic commerce"])
        ladder = _ladder_with_ring(ring)
        move = get_accordion_move(ladder, "broad", 1, result_count=0, synonym_cap=2)
        assert move.action == "widen"
        assert move.rung == "fallback"

    # --- Exhausted ---

    def test_exhausted_when_fallback_synonyms_done(self):
        ring = _ring(terms=["electronic commerce"])
        ladder = _ladder_with_ring(ring)
        move = get_accordion_move(ladder, "fallback", 1, result_count=0, synonym_cap=2)
        assert move.action == "exhausted"
        assert move.next_queries == []

    def test_exhausted_empty_ladder(self):
        ladder = AccordionLadder()
        move = get_accordion_move(ladder, "constrained", 0, result_count=0)
        assert move.action in {"widen", "exhausted"}

    # --- Tighten ---

    def test_tighten_when_noisy(self):
        ladder = self._ladder()
        move = get_accordion_move(ladder, "contextual", 0, result_count=200, noise_threshold=50)
        assert move.action == "tighten"
        assert move.rung == "constrained"
        assert move.synonym_idx == 0

    def test_tighten_at_constrained_accepts_instead(self):
        ladder = self._ladder()
        move = get_accordion_move(ladder, "constrained", 0, result_count=200, noise_threshold=50)
        assert move.action == "accept"

    def test_zero_results_no_synonyms_widens(self):
        """Without synonyms, zero results at any rung should immediately widen."""
        ladder = self._ladder(has_synonyms=False)
        move = get_accordion_move(ladder, "constrained", 0, result_count=0, synonym_cap=3)
        # Only primary term (1 query), index 0 already tried → widen
        assert move.action == "widen"
        assert move.rung == "contextual"


# ---------------------------------------------------------------------------
# classify_and_build_ladder — LLM path (mocked)
# ---------------------------------------------------------------------------

class TestClassifyAndBuildLadderLLM:
    def _llm_data(self) -> dict:
        return {
            "claim_kind": CLAIM_COMPANY_OPERATIONS,
            "evidence_need": EVIDENCE_NEWS_ARCHIVE,
            "confidence": 0.91,
            "primary_term": "NetMarket",
            "era_start": 1993,
            "era_end": 1999,
            "synonym_ring": {
                "terminology_shifts": ["electronic commerce", "online retailing"],
                "institutional_names": ["Net Market Inc"],
                "era_modifiers": ["interactive commerce"],
            },
            "query_constrained": "{PRIMARY} 1994 online commerce newspaper periodical",
            "query_contextual":  "{PRIMARY} 1994 online commerce",
            "query_broad":       "{PRIMARY}",
            "query_fallback":    "commerce retail history",
        }

    def test_llm_result_used(self, monkeypatch):
        monkeypatch.setattr(search_policy, "_call_ollama", lambda *a, **kw: self._llm_data())
        ck, en, conf, ladder = classify_and_build_ladder(
            "NetMarket", "NetMarket enabled secure online purchases.", use_llm=True
        )
        assert ck == CLAIM_COMPANY_OPERATIONS
        assert en == EVIDENCE_NEWS_ARCHIVE
        assert abs(conf - 0.91) < 0.01
        assert ladder.generation_method == "llm"
        assert ladder.primary_term == "NetMarket"
        assert "electronic commerce" in ladder.synonym_ring.terminology_shifts
        assert "Net Market Inc" in ladder.synonym_ring.institutional_names
        assert ladder.synonym_ring.era_start == 1993

    def test_synonym_ring_stored_on_ladder(self, monkeypatch):
        monkeypatch.setattr(search_policy, "_call_ollama", lambda *a, **kw: self._llm_data())
        _, _, _, ladder = classify_and_build_ladder(
            "Commerce", "NetMarket transformed commerce.", use_llm=True
        )
        assert not ladder.synonym_ring.is_empty()
        d = ladder.to_dict()
        assert d["synonym_ring"]["terminology_shifts"] == ["electronic commerce", "online retailing"]

    def test_llm_generates_era_vocabulary(self, monkeypatch):
        monkeypatch.setattr(search_policy, "_call_ollama", lambda *a, **kw: self._llm_data())
        _, _, _, ladder = classify_and_build_ladder(
            "Commerce", "NetMarket transformed commerce.", use_llm=True
        )
        queries = ladder.queries_for_rung("contextual", synonym_cap=5)
        # Should have query with primary term AND queries with era synonyms
        assert any("NetMarket" in q for q in queries)
        assert any("electronic commerce" in q for q in queries)

    def test_falls_back_to_heuristic_on_ollama_error(self, monkeypatch):
        def _boom(*a, **kw): raise RuntimeError("ollama unavailable")
        monkeypatch.setattr(search_policy, "_call_ollama", _boom)
        ck, en, conf, ladder = classify_and_build_ladder(
            "NetMarket", "NetMarket enabled secure commerce.", use_llm=True
        )
        assert ladder.generation_method == "heuristic"
        assert ladder.synonym_ring.is_empty()  # no era vocab without LLM
        assert ladder.constrained   # but ladder still populated

    def test_use_llm_false_skips_llm(self, monkeypatch):
        called = []
        monkeypatch.setattr(search_policy, "_call_ollama", lambda *a, **kw: called.append(1) or {})
        classify_and_build_ladder("Ch", "Claim.", use_llm=False)
        assert not called

    def test_invalid_enum_values_fall_to_defaults(self, monkeypatch):
        bad = {**self._llm_data(), "claim_kind": "nonsense", "evidence_need": "nonsense"}
        monkeypatch.setattr(search_policy, "_call_ollama", lambda *a, **kw: bad)
        ck, en, _, ladder = classify_and_build_ladder("Ch", "Claim.", use_llm=True)
        assert ck == CLAIM_OTHER
        assert en == EVIDENCE_MIXED

    def test_existing_good_query_promoted_to_constrained(self, monkeypatch):
        monkeypatch.setattr(search_policy, "_call_ollama", lambda *a, **kw: self._llm_data())
        existing = ["NetMarket 1994 secure online purchase newspaper historical press"]
        _, _, _, ladder = classify_and_build_ladder(
            "Commerce", "NetMarket transformed commerce.",
            use_llm=True, existing_queries=existing
        )
        assert ladder.constrained.startswith(existing[0])

    def test_low_quality_existing_queries_not_promoted(self, monkeypatch):
        monkeypatch.setattr(search_policy, "_call_ollama", lambda *a, **kw: self._llm_data())
        bad = ["split claims and tie each to evidence"]
        _, _, _, ladder = classify_and_build_ladder(
            "Commerce", "NetMarket transformed commerce.",
            use_llm=True, existing_queries=bad
        )
        assert ladder.constrained != bad[0]


# ---------------------------------------------------------------------------
# Heuristic classifier
# ---------------------------------------------------------------------------

class TestHeuristicClassify:
    def test_legal(self):
        ck, en, conf = _heuristic_classify("Regulation", "Sherman Antitrust Act limited monopolies.")
        assert ck == CLAIM_LEGAL_REGULATORY and en == EVIDENCE_LEGAL_TEXT

    def test_labor(self):
        ck, en, _ = _heuristic_classify("Labor", "US unemployment rate reached 6% in 1991.")
        assert ck == CLAIM_QUANTITATIVE_LABOR and en == EVIDENCE_OFFICIAL_STATISTICS

    def test_macro(self):
        ck, en, _ = _heuristic_classify("GDP", "GDP grew at 4.2% annually.")
        assert ck == CLAIM_QUANTITATIVE_MACRO

    def test_biographical(self):
        ck, _, _ = _heuristic_classify("Founders", "Bezos was born in Albuquerque.")
        assert ck == CLAIM_BIOGRAPHICAL

    def test_company_ecommerce_not_other(self):
        ck, en, _ = _heuristic_classify(
            "Chapter Two: NetMarket",
            "NetMarket transformed online commerce allowing customers to buy products securely."
        )
        assert ck == CLAIM_COMPANY_OPERATIONS
        assert ck != CLAIM_OTHER

    def test_historical_narrative(self):
        ck, _, _ = _heuristic_classify(
            "Rise of Commerce", "The emergence of online retail in the 1990s transformed consumer culture."
        )
        assert ck in {CLAIM_HISTORICAL_NARRATIVE, CLAIM_COMPANY_OPERATIONS}

    def test_unknown_other_low_confidence(self):
        ck, en, conf = _heuristic_classify("Notes", "Something vague.")
        assert ck == CLAIM_OTHER and conf < 0.55


# ---------------------------------------------------------------------------
# Heuristic accordion (no LLM)
# ---------------------------------------------------------------------------

class TestHeuristicAccordion:
    def test_all_rungs_populated(self):
        ladder = _heuristic_accordion(
            "Commerce", "NetMarket launched secure purchase in 1994.",
            CLAIM_COMPANY_OPERATIONS, EVIDENCE_NEWS_ARCHIVE
        )
        assert all([ladder.constrained, ladder.contextual, ladder.broad, ladder.fallback])

    def test_synonym_ring_empty(self):
        ladder = _heuristic_accordion(
            "Commerce", "NetMarket launched secure purchase.",
            CLAIM_COMPANY_OPERATIONS, EVIDENCE_NEWS_ARCHIVE
        )
        assert ladder.synonym_ring.is_empty()
        assert ladder.generation_method == "heuristic"

    def test_constrained_contains_archival_vocab(self):
        ladder = _heuristic_accordion(
            "Archives", "Merchant guilds kept correspondence.",
            CLAIM_HISTORICAL_NARRATIVE, EVIDENCE_PRIMARY_SOURCE
        )
        assert any(kw in ladder.constrained.lower()
                   for kw in ["archives", "correspondence", "manuscripts", "diaries"])


# ---------------------------------------------------------------------------
# query_quality_score
# ---------------------------------------------------------------------------

class TestQueryQualityScore:
    def test_good_scores_high(self):
        assert query_quality_score("NetMarket 1994 online commerce newspaper") > 0.7

    def test_empty_zero(self):
        assert query_quality_score("") == 0.0

    def test_too_short_low(self):
        assert query_quality_score("x") < 0.4

    def test_meta_instruction_low(self):
        assert query_quality_score("split claims and tie each to evidence") < 0.4


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
