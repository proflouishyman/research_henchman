"""Tests for manuscript-centric artifact bundle export."""

from __future__ import annotations

import json
from pathlib import Path

from artifact_export import export_run_bundle
from contracts import Gap, GapMap, GapPriority, GapPullResult, GapType, PlannedGap, RunRecord, SourceResult, SourceType


def test_export_run_bundle_writes_expected_structure(settings_factory, write_docx):
    settings = settings_factory(
        ORCH_DATA_ROOT="state",
        ORCH_PULL_OUTPUT_ROOT="pull_outputs",
    )
    workspace = settings.workspace

    manuscript = workspace / "Manuscript" / "Sample Research Draft.docx"
    write_docx(manuscript, ["TODO: add source for this claim.", "This paragraph needs more evidence."])

    run_dir = workspace / "pull_outputs" / "run_demo" / "AUTO-01-G1" / "world_bank"
    run_dir.mkdir(parents=True, exist_ok=True)
    json_packet = run_dir / "packet.json"
    json_packet.write_text(
        json.dumps(
            [
                {
                    "title": "Demo Link",
                    "url": "https://example.org/demo",
                    "quality_label": "seed",
                    "quality_rank": "20",
                }
            ]
        ),
        encoding="utf-8",
    )
    pdf_file = run_dir / "evidence.pdf"
    pdf_file.write_text("pdf-bytes", encoding="utf-8")

    rec = RunRecord(
        run_id="run_demo",
        manuscript_path=str(manuscript.relative_to(workspace)),
        gap_map=GapMap(
            manuscript_path=str(manuscript.relative_to(workspace)),
            manuscript_fingerprint="abc123",
            gaps=[
                Gap(
                    gap_id="AUTO-01-G1",
                    chapter="Chapter One",
                    claim_text="Claim needs support",
                    gap_type=GapType.EXPLICIT,
                    priority=GapPriority.HIGH,
                    source_text_excerpt="TODO: add source for this claim.",
                )
            ],
        ),
    )
    rec.pull_results = [
        GapPullResult(
            gap_id="AUTO-01-G1",
            planned_gap=PlannedGap(gap_id="AUTO-01-G1"),
            results=[
                SourceResult(
                    source_id="world_bank",
                    source_type=SourceType.FREE_API,
                    query="demo query",
                    gap_id="AUTO-01-G1",
                    run_dir=str(run_dir),
                    artifact_type="json_records",
                    status="completed",
                )
            ],
            total_documents=2,
            status="completed",
        )
    ]

    bundle_root = export_run_bundle(rec, settings)
    assert bundle_root is not None
    assert bundle_root.exists()

    copied_doc = bundle_root / manuscript.name
    assert copied_doc.exists()

    report_path = bundle_root / "gap_report_run_demo.md"
    assert report_path.exists()
    report_text = report_path.read_text(encoding="utf-8")
    assert "## AUTO-01-G1" in report_text
    assert "TODO: add source for this claim." in report_text
    assert "Quality Mix: high=0, medium=0, seed=1" in report_text
    assert "Seed-only retrieval." in report_text

    copied_packet = bundle_root / "gaps" / "AUTO-01-G1" / "related_documents" / "world_bank" / "packet.json"
    copied_pdf = bundle_root / "gaps" / "AUTO-01-G1" / "related_documents" / "world_bank" / "evidence.pdf"
    assert copied_packet.exists()
    assert copied_pdf.exists()

    url_file = bundle_root / "gaps" / "AUTO-01-G1" / "related_urls.txt"
    assert url_file.exists()
    assert "https://example.org/demo" in url_file.read_text(encoding="utf-8")

    manifest = bundle_root / "bundle_manifest_run_demo.json"
    assert manifest.exists()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run_demo"
    assert payload["gap_count"] == 1
    assert int(payload["gaps"]["AUTO-01-G1"]["quality_seed"]) == 1


def test_export_run_bundle_dedupes_repeated_run_dirs(settings_factory, write_docx):
    settings = settings_factory(
        ORCH_DATA_ROOT="state",
        ORCH_PULL_OUTPUT_ROOT="pull_outputs",
    )
    workspace = settings.workspace
    manuscript = workspace / "Manuscript" / "Dedup Draft.docx"
    write_docx(manuscript, ["Some claim text here."])

    run_dir = workspace / "pull_outputs" / "run_dup" / "AUTO-01-G1" / "jstor"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "one.json").write_text(json.dumps([{"url": "https://example.org/a"}]), encoding="utf-8")

    rec = RunRecord(
        run_id="run_dup",
        manuscript_path=str(manuscript.relative_to(workspace)),
        gap_map=GapMap(
            manuscript_path=str(manuscript.relative_to(workspace)),
            manuscript_fingerprint="dup123",
            gaps=[
                Gap(
                    gap_id="AUTO-01-G1",
                    chapter="Body",
                    claim_text="Claim",
                    gap_type=GapType.IMPLICIT,
                    priority=GapPriority.MEDIUM,
                    source_text_excerpt="Some claim text here.",
                )
            ],
        ),
    )
    rec.pull_results = [
        GapPullResult(
            gap_id="AUTO-01-G1",
            planned_gap=PlannedGap(gap_id="AUTO-01-G1"),
            results=[
                SourceResult(
                    source_id="jstor",
                    source_type=SourceType.PLAYWRIGHT,
                    query="q1",
                    gap_id="AUTO-01-G1",
                    run_dir=str(run_dir),
                    status="completed",
                ),
                SourceResult(
                    source_id="jstor",
                    source_type=SourceType.PLAYWRIGHT,
                    query="q2",
                    gap_id="AUTO-01-G1",
                    run_dir=str(run_dir),
                    status="completed",
                ),
            ],
            status="completed",
        )
    ]

    bundle_root = export_run_bundle(rec, settings)
    assert bundle_root is not None
    manifest = bundle_root / "bundle_manifest_run_dup.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    gap_meta = payload["gaps"]["AUTO-01-G1"]
    assert int(gap_meta["source_count"]) == 1
    assert int(gap_meta["file_count"]) == 1


def test_export_run_bundle_returns_none_for_missing_manuscript(settings_factory):
    settings = settings_factory()
    rec = RunRecord(
        run_id="run_missing",
        manuscript_path="Manuscript/does_not_exist.docx",
    )
    assert export_run_bundle(rec, settings) is None
