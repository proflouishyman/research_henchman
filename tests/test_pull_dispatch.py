"""Tests for layers/pull_dispatch.py.

Network/browser-bound shims (EBSCO, HathiTrust, ProQuest, SEC) are
monkeypatched so the dispatcher can be exercised end-to-end against
sqlite without any real I/O.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from adapters.gap_tree import ensure_gap_tree_schema, insert_node
from layers import pull_dispatch
from layers.gap_query_planner import (
    SRC_EBSCO,
    SRC_HATHI,
    SRC_PQ_INTL,
    SRC_PQ_US,
    SRC_SEC_10K,
)


class FakeLLM:
    def __init__(self, responses: List[str]):
        self._iter = iter(responses)

    def complete(self, *, system: str, prompt: str, temperature: float = 0.2) -> str:
        try:
            return next(self._iter)
        except StopIteration:
            return ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "gt.sqlite"))
    conn.row_factory = sqlite3.Row
    ensure_gap_tree_schema(conn)
    return conn


@pytest.fixture
def patched_pullers(monkeypatch):
    """Replace each puller shim with a deterministic stub.

    Each stub records (gap_id, query, source_id) and returns a fixed count.
    """
    calls: List[Dict[str, Any]] = []

    def stub_sec(*, entity, gap_id, pull_root, user_agent, **kwargs):
        calls.append({"source": SRC_SEC_10K, "gap": gap_id, "q": entity})
        out = Path(pull_root) / gap_id / SRC_SEC_10K
        out.mkdir(parents=True, exist_ok=True)
        (out / "stub.json").write_text("[]")
        return 5, out / "stub.json", []

    def stub_ebsco(*, query, gap_id, claim_text, pull_root, timeout_seconds=60):
        calls.append({"source": SRC_EBSCO, "gap": gap_id, "q": query})
        return 7, None, []

    def stub_hathi(*, page, query, gap_id, pull_root):
        calls.append({"source": SRC_HATHI, "gap": gap_id, "q": query})
        return 3, None, []

    def stub_proquest(*, page, query, gap_id, source_id, pull_root):
        calls.append({"source": source_id, "gap": gap_id, "q": query})
        return 2, None, []

    monkeypatch.setattr(pull_dispatch, "_pull_sec_edgar", stub_sec)
    monkeypatch.setattr(pull_dispatch, "_pull_ebsco",     stub_ebsco)
    monkeypatch.setattr(pull_dispatch, "_pull_hathitrust", stub_hathi)
    monkeypatch.setattr(pull_dispatch, "_pull_proquest",   stub_proquest)
    return calls


# ---------------------------------------------------------------------------
# pull_gap routing tests
# ---------------------------------------------------------------------------


class TestPullGap:
    def test_company_profile_routes_4_sources(self, db, tmp_path, patched_pullers):
        insert_node(
            db, gap_id="CP_TEST", tier=1, gap_type="company_profile",
            evidence_target=200, claim_text="Mercado Libre",
            detector_pass="F",
        )
        node = dict(db.execute(
            "SELECT * FROM gap_tree WHERE gap_id='CP_TEST'"
        ).fetchone())
        result = pull_dispatch.pull_gap(
            db, node, run_id="r", llm=FakeLLM(["meli AND retail"]),
            pull_root=tmp_path / "po",
        )
        sources = {c["source"] for c in patched_pullers}
        assert SRC_SEC_10K in sources
        assert SRC_EBSCO in sources
        assert SRC_HATHI in sources
        assert SRC_PQ_US in sources
        assert result["queries_run"] == 4
        assert result["records_pulled"] == 5 + 7 + 3 + 2
        # Status flipped to pulled
        s = db.execute(
            "SELECT status FROM gap_tree WHERE gap_id='CP_TEST'"
        ).fetchone()[0]
        assert s == "pulled"

    def test_intro_promise_tier_1_includes_intl(self, db, tmp_path, patched_pullers):
        insert_node(
            db, gap_id="IP_TEST", tier=1, gap_type="intro_promise",
            evidence_target=120, claim_text="China retail market",
            detector_pass="A",
        )
        node = dict(db.execute(
            "SELECT * FROM gap_tree WHERE gap_id='IP_TEST'"
        ).fetchone())
        pull_dispatch.pull_gap(
            db, node, run_id="r",
            llm=FakeLLM(["broad q", "narrow q"]),
            pull_root=tmp_path / "po",
        )
        sources = {c["source"] for c in patched_pullers}
        assert SRC_PQ_INTL in sources
        # 3 sources × 2 queries + intl × 1 = 7
        assert len(patched_pullers) == 7

    def test_editorial_todo_skipped(self, db, tmp_path, patched_pullers):
        insert_node(
            db, gap_id="TODO_TEST", tier=3, gap_type="editorial_todo",
            evidence_target=0, claim_text="rewrite intro",
            detector_pass="B",
        )
        node = dict(db.execute(
            "SELECT * FROM gap_tree WHERE gap_id='TODO_TEST'"
        ).fetchone())
        result = pull_dispatch.pull_gap(
            db, node, run_id="r", llm=FakeLLM([]),
            pull_root=tmp_path / "po",
        )
        assert result["skipped"] is True
        assert patched_pullers == []

    def test_already_pulled_gap_skipped(self, db, tmp_path, patched_pullers):
        insert_node(
            db, gap_id="DONE", tier=1, gap_type="research_gap",
            evidence_target=120, claim_text="claim", detector_pass="B",
            status="pulled",
        )
        node = dict(db.execute(
            "SELECT * FROM gap_tree WHERE gap_id='DONE'"
        ).fetchone())
        result = pull_dispatch.pull_gap(
            db, node, run_id="r", llm=FakeLLM(["q"]),
            pull_root=tmp_path / "po",
        )
        assert result["skipped"] is True
        assert result["reason"] == "already_pulled"
        assert patched_pullers == []


# ---------------------------------------------------------------------------
# fetch_pullable_nodes
# ---------------------------------------------------------------------------


class TestFetchPullableNodes:
    def test_filters_editorial_and_pulled(self, db):
        insert_node(db, gap_id="A1", tier=1, gap_type="research_gap",
                    evidence_target=120, claim_text="x", detector_pass="B")
        insert_node(db, gap_id="A2", tier=1, gap_type="editorial_todo",
                    evidence_target=0, claim_text="x", detector_pass="B")
        insert_node(db, gap_id="A3", tier=1, gap_type="intro_promise",
                    evidence_target=120, claim_text="x", detector_pass="A",
                    status="pulled")
        rows = pull_dispatch.fetch_pullable_nodes(db)
        ids = {r["gap_id"] for r in rows}
        assert ids == {"A1"}

    def test_explicit_gap_ids(self, db):
        insert_node(db, gap_id="X1", tier=1, gap_type="research_gap",
                    evidence_target=60, claim_text="x", detector_pass="B")
        rows = pull_dispatch.fetch_pullable_nodes(db, gap_ids=["X1", "MISSING"])
        assert len(rows) == 1
        assert rows[0]["gap_id"] == "X1"


# ---------------------------------------------------------------------------
# SEC EDGAR shim live (mocked HTTP) — exercises the seed-JSON write path
# ---------------------------------------------------------------------------


class TestSecEdgarShim:
    def test_writes_seed_json(self, tmp_path, monkeypatch):
        from adapters import sec_edgar

        # Stub out the internal HTTP layer.
        def fake_http(url, user_agent, *, timeout=30):
            if url == sec_edgar._COMPANY_TICKERS_URL:
                return json.dumps({
                    "0": {"cik_str": 1018724, "ticker": "AMZN",
                          "title": "AMAZON COM INC"},
                }).encode()
            return json.dumps({
                "filings": {"recent": {
                    "form": ["10-K", "10-K"],
                    "accessionNumber": ["0000000000-24-000001",
                                          "0000000000-23-000001"],
                    "filingDate": ["2024-01-01", "2023-01-01"],
                    "reportDate": ["2023-12-31", "2022-12-31"],
                    "primaryDocument": ["amzn.htm", "amzn.htm"],
                }}
            }).encode()

        monkeypatch.setattr(sec_edgar, "_http_get", fake_http)
        monkeypatch.setattr(
            sec_edgar, "_DEFAULT_CACHE_PATH", tmp_path / "cache.json"
        )

        n, path, errs = pull_dispatch._pull_sec_edgar(
            entity="Amazon", gap_id="CP1",
            pull_root=tmp_path / "po",
            user_agent="t",
        )
        assert n == 2
        assert errs == []
        assert path.exists()
        rows = json.loads(path.read_text())
        assert all(r["form"] == "10-K" for r in rows)
        assert all(r["source"] == "sec_edgar" for r in rows)
