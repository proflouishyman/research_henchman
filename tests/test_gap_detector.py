"""Tests for layers/gap_detector.py — Pass A & Pass B detector functions.

Pass A (intro promises) is exercised with a stubbed LLM that returns a
fixed JSON list, since the live model is too slow for unit tests.

Pass B (bracketed TODOs) is exercised with a stubbed LLM that just echoes
the bracketed text — we're testing the regex + filter logic, not the
research-question phrasing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from adapters.gap_tree import (
    ensure_gap_tree_schema,
    fetch_research_question,
    insert_node,
    update_research_question,
)
from layers.gap_detector import detect_pass_a, detect_pass_b


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class StubLLM:
    """Programmable LLM stub for unit tests.

    Set `.json_response` for `complete_json` calls (Pass A's primary call).
    Set `.text_response` for `complete` calls (Pass A's RQ rewrite, Pass B's
    RQ generation, and the JSON-repair fallback). `.calls` records
    (method, system, prompt) tuples.
    """

    def __init__(
        self,
        json_response: Optional[List[Dict[str, Any]]] = None,
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
      Chapter 1: Alibaba  — only ~10 words of body (thin → tier 1)
      Chapter 2: Other    — ~40 words of body (thin → tier 1, but no promise points here)
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
    """Build a docx body containing TODO-shaped brackets and one citation ref.

    Cases (all wrapped in body paragraphs, not headings):
      [TODO 1]                — too short / single word post-strip-> still ≥10? "TODO 1" is 6 chars. Filtered by length floor.
      [need section on Alibaba's IPO regulation] — should match
      [Smith 2003]            — citation pattern, should be filtered
      [1]                     — bare numeric ref, regex requires letter start; filtered
      [add the part about JD.com platform launch] — should match
    """
    paragraphs = [
        "Manuscript Body",
        "First paragraph contains a placeholder [TODO 1] inline.",
        "Second paragraph: [need section on Alibaba's IPO regulation].",
        "Third paragraph cites [Smith 2003] as evidence.",
        "Fourth paragraph references footnote [1] in passing.",
        "Fifth paragraph notes [add the part about JD.com platform launch] needs work.",
    ]
    write_docx(target, paragraphs)


# ---------------------------------------------------------------------------
# Pass A
# ---------------------------------------------------------------------------

class TestPassA:
    def test_extracts_promises_from_minimal_intro(self, tmp_path, write_docx):
        docx = tmp_path / "manuscript.docx"
        _write_minimal_intro_docx(write_docx, docx)

        # Stubbed LLM returns one promise that should pair with Chapter 1.
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
        assert n["chapter"].startswith("CHAPTER 1: ALIBABA") or "Alibaba" in n["chapter"]
        # Section body had ~5 words (after the heading was stripped) → tier 1.
        assert n["tier"] == 1
        assert n["evidence_target"] == 120
        assert n["research_question"]  # non-empty
        assert n["source_locator"] == "introduction"

    def test_no_intro_returns_empty(self, tmp_path, write_docx):
        docx = tmp_path / "no_intro.docx"
        write_docx(docx, ["Chapter 1: Body", "Just body content here."])
        llm = StubLLM(json_response=[{"promise_text": "x", "key_entity": "x", "region": "US"}])
        nodes = detect_pass_a(docx, llm)
        assert nodes == []

    def test_promise_to_well_developed_section_is_skipped(self, tmp_path, write_docx):
        # Chapter 1 has >300 words → promise should be dropped.
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


# ---------------------------------------------------------------------------
# Pass B
# ---------------------------------------------------------------------------

class TestPassB:
    def test_finds_bracketed_todos(self, tmp_path, write_docx):
        docx = tmp_path / "todos.docx"
        _write_todo_docx(write_docx, docx)

        llm = StubLLM(text_response="What is the answer to this TODO?")
        nodes = detect_pass_b(docx, llm=llm)

        # Expect 2 surviving TODOs:
        #   "need section on Alibaba's IPO regulation"
        #   "add the part about JD.com platform launch"
        # Filtered out:
        #   "TODO 1" — content "TODO 1" is < 10 chars after content extraction
        #   "Smith 2003" — citation heuristic
        #   "1" — fails the [A-Za-z]-starts-with regex
        contents = [n["claim_text"] for n in nodes]
        joined = " || ".join(contents)
        assert "Alibaba's IPO regulation" in joined
        assert "JD.com platform launch" in joined

        # Definitely-not-matched:
        assert "Smith 2003" not in joined
        for c in contents:
            assert "TODO 1" not in c  # the short one shouldn't appear

        # All TODOs are tier 1
        for n in nodes:
            assert n["tier"] == 1
            assert n["gap_type"] == "explicit_todo"
            assert n["detector_pass"] == "B"
            assert n["research_question"]  # populated by stubbed LLM

    def test_evidence_target_short_vs_long(self, tmp_path, write_docx):
        # 2 TODOs: one is ≤6 words, the other is >6 words.
        # Avoid 4-digit years inside short brackets — those trip the
        # citation-style filter on purpose.
        docx = tmp_path / "todos2.docx"
        write_docx(docx, [
            "Body",
            "Para A [add Alibaba revenue data].",
            "Para B [add the part about JD.com platform launch and IPO documents needed].",
        ])
        llm = StubLLM(text_response="What is the answer?")
        nodes = detect_pass_b(docx, llm=llm)
        assert len(nodes) == 2
        short = [n for n in nodes if "Alibaba revenue data" in n["claim_text"]][0]
        long_ = [n for n in nodes if "JD.com" in n["claim_text"]][0]
        assert short["evidence_target"] == 40    # ≤6 words
        assert long_["evidence_target"] == 80    # >6 words

    def test_research_question_resumes(self, tmp_path, write_docx, db):
        """Re-running Pass B with the same DB doesn't re-call the LLM if rq is set."""
        docx = tmp_path / "todos3.docx"
        write_docx(docx, [
            "Body",
            "Para A [need section on Alibaba's IPO regulation].",
        ])

        # First run — fresh DB, no existing rows. LLM is called once.
        llm1 = StubLLM(text_response="First-run RQ")
        nodes1 = detect_pass_b(docx, llm=llm1, conn=db)
        assert len(nodes1) == 1
        assert nodes1[0]["research_question"] == "First-run RQ"
        # Insert the row and override the rq with a curated value.
        n = nodes1[0]
        insert_node(db, **n)
        update_research_question(db, n["gap_id"], "Curated RQ")

        # Second run — same docx, same DB. Should resume (no LLM call) and
        # surface the curated RQ instead of asking the model again.
        llm2 = StubLLM(text_response="WRONG: should not be called")
        nodes2 = detect_pass_b(docx, llm=llm2, conn=db)
        assert len(nodes2) == 1
        assert nodes2[0]["research_question"] == "Curated RQ"
        # The stubbed LLM's complete() should NOT have been called for the
        # research-question path.
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
        # No LLM → research_question is the verbatim content.
        assert nodes[0]["research_question"] == "need section on Alibaba's IPO regulation"
