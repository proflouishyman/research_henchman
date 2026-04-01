"""Adapter link emission tests for click-through document UX."""

from __future__ import annotations

import json
from pathlib import Path

from app.adapters.document_links import provider_search_url
from app.adapters.keyed_apis import EbscoApiAdapter
from app.adapters.playwright_adapters import JstorPlaywrightAdapter
from app.contracts import GapPriority, GapType, PlannedGap


def _gap() -> PlannedGap:
    return PlannedGap(
        gap_id="AUTO-01-G1",
        chapter="Chapter One",
        claim_text="Claim text",
        gap_type=GapType.IMPLICIT,
        priority=GapPriority.MEDIUM,
    )


def test_provider_search_url_builds_jstor_query() -> None:
    out = provider_search_url("jstor", "john mcdonogh supercargo")
    assert "jstor.org" in out
    assert "john+mcdonogh+supercargo" in out


def test_ebsco_adapter_emits_clickthrough_rows(tmp_path) -> None:
    adapter = EbscoApiAdapter()
    run_dir = tmp_path / "runs"
    result = adapter.pull(_gap(), "john mcdonogh supercargo", str(run_dir))

    assert result.document_count >= 1
    assert result.status in {"completed", "partial"}
    artifact = Path(result.run_dir) / "john_mcdonogh_supercargo.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and payload
    assert any(str(row.get("url", "")).startswith("http") for row in payload)


def test_playwright_seed_adapter_emits_clickthrough_rows(tmp_path) -> None:
    adapter = JstorPlaywrightAdapter()
    run_dir = tmp_path / "runs"
    result = adapter.pull(_gap(), "john mcdonogh supercargo", str(run_dir))

    assert result.document_count >= 1
    artifact = Path(result.run_dir) / "john_mcdonogh_supercargo.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert any(str(row.get("url", "")).startswith("http") for row in payload)
