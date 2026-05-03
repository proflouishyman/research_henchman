"""Tests for layers/gap_detector.py — Pass A, Pass B, Pass F.

All passes are exercised with stubbed LLMs; the live model is too slow for
unit tests. Pass B's classifier is exercised via a programmable JSON stub.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pytest

from adapters.gap_tree import (
    ensure_gap_tree_schema,
    fetch_research_question,
    insert_node,
    update_research_question,
)
from layers.gap_detector import (
    detect_pass_a,
    detect_pass_b,
    detect_pass_f,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class StubLLM:
    """Programmable LLM stub for unit tests.

    `json_response` may be:
      - a list/dict — returned for every complete_json call;
      - a callable taking the user prompt and returning a list/dict — lets
        a single test return different JSON shapes depending on which call
        site invoked the stub (e.g. Pass A's promise extraction vs Pass B's
        classifier).
    """

    def __init__(
        self,
        json_response: Union[List[Any], Dict[str, Any], Any, None] = None,
        text_response: str = "What is the answer?",
    ) -> None:
        self.json_response = json_response if json_response is not None else []
        self.text_response = text_response
        self.calls: List = []

    def complete(self, *, system: str = "", prompt: str = "", temperature: float = 0.0) -> str:
        self.calls.append(("complete", system, prompt))
        return self.text_response

    def complete_json(self, *, system: str = "", prompt: str = "", temperature: float = 0.0):
        self.calls.append(("complete_json", system, prompt))
        if callable(self.json_response):
            return self.json_response(prompt)
        return self.json_response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "gt.sqlite"))
    conn.row_factory = sqlite3.Row
    ensure_gap_tree_schema(conn)
    return conn


def _write_minimal_intro_docx(write_docx, target: Path) -> None:
    """Build a docx with an Introduction promising one topic + a thin section.

    Section structure:
      Introduction        — promises Alibaba coverage
      Chapter 1: Alibaba  — only ~5 words of body (thin → tier 1)
      Chapter 2: Other    — ~40 words of body (no promise points here)
    """
    paragraphs = [
        "Introduction",
        "This book covers many topics in e-commerce history.",
        "We will examine Alibaba's rise in China during the 2000s.",
        "Chapter 1: Alibaba in China",
        "A short body about Alibaba.",
        "Chapter 2: Other Topic",
        " ".join(["unrelated"] * 40),
    ]
    write_docx(target, paragraphs)


def _write_todo_docx(write_docx, target: Path) -> None:
    """Build a docx body containing TODO-shaped brackets and one citation ref."""
    paragraphs = [
        "Manuscript Body",
        "First paragraph contains a placeholder [TODO 1] inline.",
        "Second paragraph: [need section on Alibaba's IPO regulation].",
        "Third paragraph cites [Smith 2003] as evidence.",
        "Fourth paragraph references footnote [1] in passing.",
        "Fifth paragraph notes [add the part about JD.com platform launch] needs work.",
    ]
    write_docx(target, paragraphs)


def _classifier_stub(prompt: str) -> Dict[str, Any]:
    """A simple classifier-stub used by Pass B tests.

    Returns ``editorial_note`` for prose-shaped notes ("sharper", "build on"),
    otherwise ``research_gap``. Pass A's complete_json calls (which use a
    different system prompt and return arrays of promises) won't reach this
    stub because TestPassA fixtures don't supply a callable json_response.
    """
    p = prompt.lower()
    editorial_signals = (
        "sharper", "build on chapter", "describe further", "more on this please",
        "stunningly brilliant", "cut it up", "linking comparisons", "intuition vs reason",
        "be technical",
    )
    cls = "editorial_note" if any(sig in p for sig in editorial_signals) else "research_gap"
    return {"classification": cls, "confidence": 0.9, "reason": "stub"}


def _passa_or_classifier(promises: List[Dict[str, Any]]):
    """Build a json_response callable that routes Pass A vs Pass B classifier.

    Pass A's prompt mentions "Manuscript Introduction"; the classifier's
    prompt mentions "Bracketed annotation". Use that to dispatch.
    """
    def _impl(prompt: str):
        if "manuscript introduction" in prompt.lower() or "promises" in prompt.lower():
            return promises
        return _classifier_stub(prompt)
    return _impl


# ---------------------------------------------------------------------------
# Pass A
# ---------------------------------------------------------------------------

class TestPassA:
    def test_extracts_promises_from_minimal_intro(self, tmp_path, write_docx):
        docx = tmp_path / "manuscript.docx"
        _write_minimal_intro_docx(write_docx, docx)

        llm = StubLLM(
            json_response=[{
                "promise_text": "We will examine Alibaba's rise in China during the 2000s.",
                "key_entity": "Alibaba",
                "region": "China",
                "expected_chapter_hint": "Alibaba China rise",
                "importance": 5,
            }],
            text_response="How did Alibaba rise to dominance in China during the 2000s?",
        )

        nodes = detect_pass_a(docx, llm)
        assert len(nodes) == 1
        n = nodes[0]
        assert n["gap_id"] == "IP1"
        assert n["gap_type"] == "intro_promise"
        assert n["detector_pass"] == "A"
        assert "Alibaba" in (n["chapter"] or "")
        assert n["tier"] == 1
        assert n["evidence_target"] == 120
        assert n["research_question"]
        assert n["source_locator"] == "introduction"

    def test_no_intro_returns_empty(self, tmp_path, write_docx):
        docx = tmp_path / "no_intro.docx"
        write_docx(docx, ["Chapter 1: Body", "Just body content here."])
        llm = StubLLM(json_response=[{"promise_text": "x", "key_entity": "x", "region": "US"}])
        nodes = detect_pass_a(docx, llm)
        assert nodes == []

    def test_promise_to_well_developed_section_is_skipped(self, tmp_path, write_docx):
        big_body = " ".join(["alibaba"] * 400)
        docx = tmp_path / "developed.docx"
        write_docx(docx, [
            "Introduction",
            "We will examine Alibaba's rise in China.",
            "Chapter 1: Alibaba",
            big_body,
        ])
        llm = StubLLM(
            json_response=[{
                "promise_text": "We will examine Alibaba's rise in China.",
                "key_entity": "Alibaba",
                "region": "China",
            }],
            text_response="How did Alibaba rise?",
        )
        nodes = detect_pass_a(docx, llm)
        assert nodes == []

    def test_empty_llm_response_returns_empty(self, tmp_path, write_docx):
        docx = tmp_path / "manuscript.docx"
        _write_minimal_intro_docx(write_docx, docx)
        llm = StubLLM(json_response=[])
        nodes = detect_pass_a(docx, llm)
        assert nodes == []

    def test_pass_a_finds_named_entities(self, tmp_path, write_docx):
        """Both Mercado Libre and Shein, mentioned in the intro, become
        separate IP gaps. Each pairs to its own dedicated (thin) section.
        """
        docx = tmp_path / "named.docx"
        write_docx(docx, [
            "Introduction",
            ("This book covers global e-commerce. Mercado Libre rose to "
             "dominate Latin America. Shein and Temu reshaped fast fashion."),
            "Chapter 4: Mercado Libre and Latin America",
            "A brief mention.",
            "Chapter 5: Shein and Fast Fashion",
            "A brief mention.",
        ])

        llm = StubLLM(
            json_response=[
                {
                    "promise_text": "Mercado Libre rose to dominate Latin America.",
                    "key_entity": "Mercado Libre",
                    "region": "Latin America",
                    "importance": 4,
                },
                {
                    "promise_text": "Shein and Temu reshaped fast fashion.",
                    "key_entity": "Shein",
                    "region": "Global",
                    "importance": 4,
                },
            ],
            text_response="What is the research question?",
        )
        nodes = detect_pass_a(docx, llm)
        entities_seen = {(n["claim_text"]).lower() for n in nodes}
        assert any("mercado libre" in t for t in entities_seen)
        assert any("shein" in t for t in entities_seen)
        # Each promise should pair to its own dedicated heading.
        chapters = [n["chapter"] for n in nodes if n["chapter"]]
        assert any("mercado libre" in (c or "").lower() for c in chapters)
        assert any("shein" in (c or "").lower() for c in chapters)


# ---------------------------------------------------------------------------
# Pass B
# ---------------------------------------------------------------------------

class TestPassB:
    def test_finds_bracketed_todos(self, tmp_path, write_docx):
        docx = tmp_path / "todos.docx"
        _write_todo_docx(write_docx, docx)

        llm = StubLLM(
            json_response=_classifier_stub,  # callable per-prompt
            text_response="What is the answer to this TODO?",
        )
        nodes = detect_pass_b(docx, llm=llm)

        contents = [n["claim_text"] for n in nodes]
        joined = " || ".join(contents)
        assert "Alibaba's IPO regulation" in joined
        assert "JD.com platform launch" in joined

        assert "Smith 2003" not in joined
        for c in contents:
            assert "TODO 1" not in c

        for n in nodes:
            # All test fixtures are research-shaped; classifier returns
            # research_gap.
            assert n["gap_type"] == "research_gap"
            assert n["tier"] == 1
            assert n["detector_pass"] == "B"
            assert n["research_question"]

    def test_evidence_target_short_vs_long(self, tmp_path, write_docx):
        docx = tmp_path / "todos2.docx"
        write_docx(docx, [
            "Body",
            "Para A [add Alibaba revenue data].",
            "Para B [add the part about JD.com platform launch and IPO documents needed].",
        ])
        llm = StubLLM(
            json_response=_classifier_stub,
            text_response="What is the answer?",
        )
        nodes = detect_pass_b(docx, llm=llm)
        assert len(nodes) == 2
        short = [n for n in nodes if "Alibaba revenue data" in n["claim_text"]][0]
        long_ = [n for n in nodes if "JD.com" in n["claim_text"]][0]
        assert short["evidence_target"] == 40
        assert long_["evidence_target"] == 80

    def test_research_question_resumes(self, tmp_path, write_docx, db):
        """Re-running Pass B with the same DB doesn't re-call the LLM if rq is set."""
        docx = tmp_path / "todos3.docx"
        write_docx(docx, [
            "Body",
            "Para A [need section on Alibaba's IPO regulation].",
        ])

        llm1 = StubLLM(
            json_response=_classifier_stub,
            text_response="First-run RQ",
        )
        nodes1 = detect_pass_b(docx, llm=llm1, conn=db)
        assert len(nodes1) == 1
        assert nodes1[0]["research_question"] == "First-run RQ"
        n = nodes1[0]
        insert_node(db, **n)
        update_research_question(db, n["gap_id"], "Curated RQ")

        # Second run — same docx, same DB. complete() should not be called
        # for the research-question path (rq already on disk).
        llm2 = StubLLM(
            json_response=_classifier_stub,
            text_response="WRONG: should not be called",
        )
        nodes2 = detect_pass_b(docx, llm=llm2, conn=db)
        assert len(nodes2) == 1
        assert nodes2[0]["research_question"] == "Curated RQ"
        complete_calls = [c for c in llm2.calls if c[0] == "complete"]
        assert complete_calls == []

    def test_no_llm_falls_back_to_verbatim_text(self, tmp_path, write_docx):
        """When llm is None, research_question equals the bracketed content."""
        docx = tmp_path / "todos4.docx"
        write_docx(docx, [
            "Body",
            "Para A [need section on Alibaba's IPO regulation].",
        ])
        nodes = detect_pass_b(docx, llm=None)
        assert len(nodes) == 1
        assert nodes[0]["research_question"] == "need section on Alibaba's IPO regulation"
        # No classifier llm → default safe lane is research_gap.
        assert nodes[0]["gap_type"] == "research_gap"

    def test_pass_b_classifies_editorial_note(self, tmp_path, write_docx):
        """A 'this can be sharper' note is editorial, not a research gap."""
        docx = tmp_path / "editorial.docx"
        write_docx(docx, [
            "Body",
            "Para A [this can be sharper] inline note.",
            "Para B [need section on Alibaba's IPO regulation].",
        ])
        llm = StubLLM(
            json_response=_classifier_stub,
            text_response="generic rq",
        )
        nodes = detect_pass_b(docx, llm=llm)
        editorial = [n for n in nodes if "sharper" in n["claim_text"]][0]
        research = [n for n in nodes if "Alibaba" in n["claim_text"]][0]
        assert editorial["gap_type"] == "editorial_todo"
        assert editorial["tier"] == 3
        assert editorial["status"] == "rejected"
        assert research["gap_type"] == "research_gap"
        assert research["tier"] == 1


# ---------------------------------------------------------------------------
# Pass F — company / character profiles
# ---------------------------------------------------------------------------

class TestPassF:
    def test_pass_f_detects_company_with_empty_heading(self, tmp_path, write_docx):
        """A dedicated heading with empty/thin body → CP gap with evidence_target=200."""
        docx = tmp_path / "cp_empty.docx"
        # Mercado Libre heading present but body is empty (no body line).
        write_docx(docx, [
            "Introduction",
            "Mercado Libre rose to dominate Latin American e-commerce.",
            "Chapter 4: Mercado Libre",
            "x",  # 1-word body — under the 200-word floor
        ])
        nodes = detect_pass_f(
            docx,
            llm=None,
            entity_seeds=["Mercado Libre"],
        )
        assert len(nodes) == 1
        n = nodes[0]
        assert n["gap_id"] == "CP1"
        assert n["gap_type"] == "company_profile"
        assert n["tier"] == 1
        assert n["evidence_target"] == 200
        assert "Mercado Libre" in (n["chapter"] or "")
        assert n["claim_text"] == "Mercado Libre"
        assert n["detector_pass"] == "F"
        assert "empty section" in (n["rationale"] or "")

    def test_pass_f_skips_well_covered_company(self, tmp_path, write_docx):
        """A heading + 1000-word body → no CP gap (company is covered)."""
        big_body = " ".join(["amazon"] * 1000)
        docx = tmp_path / "cp_full.docx"
        write_docx(docx, [
            "Introduction",
            "Amazon is everywhere.",
            "Chapter 1: Amazon",
            big_body,
        ])
        nodes = detect_pass_f(
            docx,
            llm=None,
            entity_seeds=["Amazon"],
        )
        assert nodes == []

    def test_pass_f_no_heading_with_high_mention_intro_link(self, tmp_path, write_docx):
        """No dedicated heading, ≥5 body mentions, intro mention → tier-1 CP gap."""
        body_with_alibaba = (
            "Alibaba did one thing. Alibaba did another. Alibaba grew. "
            "Alibaba dominated. Alibaba expanded. Alibaba diversified."
        )
        docx = tmp_path / "cp_no_heading.docx"
        write_docx(docx, [
            "Introduction",
            "Alibaba is a major Chinese platform.",
            "Chapter 1: Other Topic",
            body_with_alibaba,
        ])
        nodes = detect_pass_f(
            docx,
            llm=None,
            entity_seeds=["Alibaba"],
        )
        assert len(nodes) == 1
        n = nodes[0]
        assert n["gap_id"] == "CP1"
        assert n["evidence_target"] == 150
        assert n["chapter"] == "(no section yet)"
        assert "no dedicated section" in (n["rationale"] or "")

    def test_pass_f_skips_entity_not_in_text(self, tmp_path, write_docx):
        """An entity that doesn't appear in the manuscript at all is ignored."""
        docx = tmp_path / "cp_absent.docx"
        write_docx(docx, [
            "Introduction",
            "Some text without our target entity.",
        ])
        nodes = detect_pass_f(
            docx,
            llm=None,
            entity_seeds=["NonexistentCorp"],
        )
        assert nodes == []
