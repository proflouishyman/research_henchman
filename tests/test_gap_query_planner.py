"""Tests for layers/gap_query_planner.py."""

from __future__ import annotations

from typing import List

import pytest

from layers.gap_query_planner import (
    SRC_EBSCO,
    SRC_HATHI,
    SRC_PQ_INTL,
    SRC_PQ_US,
    SRC_SEC_10K,
    _clean_query,
    plan_queries,
)


class FakeLLM:
    """Records system/prompt and returns the next preset response."""

    def __init__(self, responses: List[str]):
        self._iter = iter(responses)
        self.calls: List[dict] = []

    def complete(self, *, system: str, prompt: str, temperature: float = 0.2) -> str:
        self.calls.append({"system": system, "prompt": prompt})
        try:
            return next(self._iter)
        except StopIteration:
            return ""


# ---------------------------------------------------------------------------
# _clean_query
# ---------------------------------------------------------------------------


class TestCleanQuery:
    def test_strips_query_prefix(self):
        assert _clean_query("Query: foo AND bar") == "foo AND bar"

    def test_strips_numbering(self):
        assert _clean_query("1. foo AND bar") == "foo AND bar"

    def test_drops_markdown_fence(self):
        assert _clean_query("```\nfoo AND bar\n```") == "foo AND bar"

    def test_takes_first_line(self):
        assert _clean_query("foo AND bar\nignored") == "foo AND bar"

    def test_strips_outer_quotes(self):
        assert _clean_query('"foo AND bar"') == "foo AND bar"

    def test_blank_returns_empty(self):
        assert _clean_query("") == ""
        assert _clean_query("   \n  \n") == ""

    def test_truncates_at_max(self):
        out = _clean_query("a" * 500, max_len=100)
        assert len(out) == 100


# ---------------------------------------------------------------------------
# Routing: intro_promise
# ---------------------------------------------------------------------------


class TestPlanIntroPromise:
    def test_tier_1_includes_intl(self):
        llm = FakeLLM(["broad query", "narrow query"])
        node = {
            "gap_type": "intro_promise",
            "tier": 1,
            "claim_text": "China became the world's largest retail market.",
        }
        plans = plan_queries(node, llm)
        sources = [s for _, s in plans]
        assert SRC_PQ_INTL in sources
        # Hath + EBSCO + PQ-US each get broad + narrow = 6, plus Intl broad = 7
        assert len(plans) == 7

    def test_tier_2_no_intl(self):
        llm = FakeLLM(["broad q", "narrow q"])
        node = {
            "gap_type": "intro_promise",
            "tier": 2,
            "claim_text": "Smartphone adoption changed online shopping.",
        }
        plans = plan_queries(node, llm)
        sources = [s for _, s in plans]
        assert SRC_PQ_INTL not in sources
        # 3 sources × 2 queries = 6
        assert len(plans) == 6

    def test_falls_back_to_claim_when_llm_blank(self):
        llm = FakeLLM(["", ""])
        node = {
            "gap_type": "intro_promise",
            "tier": 2,
            "claim_text": "C" * 60,
        }
        plans = plan_queries(node, llm)
        # No crash; some queries should be the claim text (truncated).
        assert plans
        for q, _ in plans:
            assert q  # non-empty


# ---------------------------------------------------------------------------
# Routing: research_gap
# ---------------------------------------------------------------------------


class TestPlanResearchGap:
    def test_two_sources_one_query(self):
        llm = FakeLLM(["alipay AND PBOC"])
        node = {
            "gap_type": "research_gap",
            "tier": 1,
            "claim_text": "Alipay regulatory history",
        }
        plans = plan_queries(node, llm)
        assert len(plans) == 2
        assert {s for _, s in plans} == {SRC_EBSCO, SRC_HATHI}
        assert plans[0][0] == "alipay AND PBOC"


# ---------------------------------------------------------------------------
# Routing: company_profile
# ---------------------------------------------------------------------------


class TestPlanCompanyProfile:
    def test_first_query_is_entity_for_edgar(self):
        llm = FakeLLM(['("Wal-Mart" OR "Walmart") AND history'])
        node = {
            "gap_type": "company_profile",
            "tier": 1,
            "claim_text": "Wal-Mart",
        }
        plans = plan_queries(node, llm)
        # 1 EDGAR + 3 press sources
        assert len(plans) == 4
        # EDGAR gets the raw entity name (the puller does the CIK lookup itself)
        edgar = [(q, s) for q, s in plans if s == SRC_SEC_10K]
        assert edgar == [("Wal-Mart", SRC_SEC_10K)]
        # Other three sources share the LLM-generated press query
        press = [(q, s) for q, s in plans if s != SRC_SEC_10K]
        assert {s for _, s in press} == {SRC_EBSCO, SRC_HATHI, SRC_PQ_US}
        assert all(q.startswith('("Wal-Mart"') for q, _ in press)


# ---------------------------------------------------------------------------
# Routing: editorial_todo / unknown
# ---------------------------------------------------------------------------


class TestPlanSkippedTypes:
    def test_editorial_todo_returns_empty(self):
        node = {"gap_type": "editorial_todo", "tier": 3, "claim_text": "fix prose"}
        assert plan_queries(node, FakeLLM([])) == []

    def test_unknown_type_returns_empty(self):
        node = {"gap_type": "totally_new_kind", "tier": 1, "claim_text": "x"}
        assert plan_queries(node, FakeLLM([])) == []

    def test_empty_claim_returns_empty(self):
        node = {"gap_type": "intro_promise", "tier": 1, "claim_text": ""}
        assert plan_queries(node, FakeLLM([])) == []
