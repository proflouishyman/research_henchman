"""Tests for manuscript-centric artifact bundle export."""

from __future__ import annotations

import json
import socket
import threading
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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

    # Legacy flat report still exists (backwards compat)
    report_path = bundle_root / "gap_report_run_demo.md"
    assert report_path.exists()
    report_text = report_path.read_text(encoding="utf-8")
    assert "TODO: add source for this claim." in report_text
    # New format uses "Gap N:" heading; claim text appears as link text
    assert "Claim needs support" in report_text or "Gap 1" in report_text

    # New historian-friendly structure: gaps/<chapter--claim>/documents/<source>/
    # Slug is derived from chapter + claim: "Chapter-One--Claim-needs-support"
    gaps_dir = bundle_root / "gaps"
    assert gaps_dir.exists()
    gap_folders = list(gaps_dir.iterdir())
    assert len(gap_folders) == 1
    gap_folder = gap_folders[0]
    # Folder name should embed chapter slug (chapter prefix is shortened: "Chapter One" → "chOne")
    assert "ch" in gap_folder.name.lower() and "Claim" in gap_folder.name

    copied_packet = gap_folder / "documents" / "world_bank" / "packet.json"
    copied_pdf = gap_folder / "documents" / "world_bank" / "evidence.pdf"
    assert copied_packet.exists()
    assert copied_pdf.exists()

    # README and sources files should be present
    assert (gap_folder / "_README.md").exists()
    assert (gap_folder / "_SOURCES.md").exists()

    url_file = gap_folder / "related_urls.txt"
    assert url_file.exists()
    assert "https://example.org/demo" in url_file.read_text(encoding="utf-8")

    # Master index and bibliography
    assert (bundle_root / "_INDEX.md").exists()
    assert (bundle_root / "_BIBLIOGRAPHY.md").exists()

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


def test_export_run_bundle_fetches_seed_urls(settings_factory, write_docx, tmp_path):
    settings = settings_factory(
        ORCH_DATA_ROOT="state",
        ORCH_PULL_OUTPUT_ROOT="pull_outputs",
    )
    workspace = settings.workspace
    manuscript = workspace / "Manuscript" / "URL Fetch Draft.docx"
    write_docx(manuscript, ["A claim needing online support."])

    # Local HTTP fixture with one HTML page linking to a PDF.
    web_root = tmp_path / "web"
    web_root.mkdir(parents=True, exist_ok=True)
    (web_root / "doc.pdf").write_bytes(b"%PDF-1.4 local test pdf")
    (web_root / "style.css").write_text("body { color: black; }", encoding="utf-8")
    (web_root / "index.html").write_text(
        '<html><body><a href="/doc.pdf">doc</a><a href="/style.css">css</a></body></html>',
        encoding="utf-8",
    )

    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_root), **kwargs)

        def log_message(self, format, *args):  # noqa: A003
            return

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
    server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        run_dir = workspace / "pull_outputs" / "run_urlfetch" / "AUTO-01-G1" / "jstor"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "seed.json").write_text(
            json.dumps([{"title": "seed", "url": f"http://127.0.0.1:{port}/index.html", "quality_label": "seed"}]),
            encoding="utf-8",
        )

        rec = RunRecord(
            run_id="run_urlfetch",
            manuscript_path=str(manuscript.relative_to(workspace)),
            gap_map=GapMap(
                manuscript_path=str(manuscript.relative_to(workspace)),
                manuscript_fingerprint="url123",
                gaps=[
                    Gap(
                        gap_id="AUTO-01-G1",
                        chapter="Body",
                        claim_text="Claim",
                        gap_type=GapType.IMPLICIT,
                        priority=GapPriority.MEDIUM,
                        source_text_excerpt="A claim needing online support.",
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
                        query="q",
                        gap_id="AUTO-01-G1",
                        run_dir=str(run_dir),
                        status="completed",
                    )
                ],
                status="completed",
            )
        ]

        bundle_root = export_run_bundle(rec, settings)
        assert bundle_root is not None
        # Find the gap folder (slug-named)
        gap_folders = list((bundle_root / "gaps").iterdir())
        assert gap_folders
        gap_folder = gap_folders[0]
        fetched_root = gap_folder / "documents" / "jstor" / "_fetched_urls"
        fetched_files = list(fetched_root.glob("*"))
        assert fetched_files, "expected fetched URL artifacts"
        assert any(p.suffix.lower() == ".html" for p in fetched_files)
        assert any(p.suffix.lower() == ".pdf" for p in fetched_files)
        assert not any(p.suffix.lower() == ".css" for p in fetched_files)
        assert not any(p.name.endswith(".bin") for p in fetched_files)

        # Legacy report still contains key info
        report_text = (bundle_root / "gap_report_run_urlfetch.md").read_text(encoding="utf-8")
        assert "gap" in report_text.lower() or "Auto" in report_text
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_export_run_bundle_replaces_stale_gap_exports(settings_factory, write_docx):
    settings = settings_factory(
        ORCH_DATA_ROOT="state",
        ORCH_PULL_OUTPUT_ROOT="pull_outputs",
    )
    workspace = settings.workspace
    manuscript = workspace / "Manuscript" / "Stale Cleanup Draft.docx"
    write_docx(manuscript, ["A claim."])

    old_dir = workspace / "pull_outputs" / "run_old" / "AUTO-01-G1" / "world_bank"
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "old.json").write_text(json.dumps([{"url": "https://example.org/old"}]), encoding="utf-8")

    rec_old = RunRecord(
        run_id="run_old",
        manuscript_path=str(manuscript.relative_to(workspace)),
        gap_map=GapMap(
            manuscript_path=str(manuscript.relative_to(workspace)),
            manuscript_fingerprint="stale123",
            gaps=[
                Gap(
                    gap_id="AUTO-01-G1",
                    chapter="Body",
                    claim_text="Claim",
                    gap_type=GapType.IMPLICIT,
                    priority=GapPriority.MEDIUM,
                    source_text_excerpt="A claim.",
                )
            ],
        ),
    )
    rec_old.pull_results = [
        GapPullResult(
            gap_id="AUTO-01-G1",
            planned_gap=PlannedGap(gap_id="AUTO-01-G1"),
            results=[
                SourceResult(
                    source_id="world_bank",
                    source_type=SourceType.FREE_API,
                    query="old q",
                    gap_id="AUTO-01-G1",
                    run_dir=str(old_dir),
                    status="completed",
                )
            ],
            status="completed",
        )
    ]
    bundle_root = export_run_bundle(rec_old, settings)
    assert bundle_root is not None
    # Find the slug-named gap folder
    gap_folders_old = list((bundle_root / "gaps").iterdir())
    assert gap_folders_old
    gap_folder = gap_folders_old[0]
    stale_path = gap_folder / "documents" / "world_bank" / "old.json"
    assert stale_path.exists(), f"expected {stale_path}, got: {list(gap_folder.rglob('*.json'))}"

    new_dir = workspace / "pull_outputs" / "run_new" / "AUTO-01-G1" / "jstor"
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / "new.json").write_text(json.dumps([{"url": "https://example.org/new"}]), encoding="utf-8")

    rec_new = RunRecord(
        run_id="run_new",
        manuscript_path=str(manuscript.relative_to(workspace)),
        gap_map=GapMap(
            manuscript_path=str(manuscript.relative_to(workspace)),
            manuscript_fingerprint="stale123",
            gaps=[
                Gap(
                    gap_id="AUTO-01-G1",
                    chapter="Body",
                    claim_text="Claim",
                    gap_type=GapType.IMPLICIT,
                    priority=GapPriority.MEDIUM,
                    source_text_excerpt="A claim.",
                )
            ],
        ),
    )
    rec_new.pull_results = [
        GapPullResult(
            gap_id="AUTO-01-G1",
            planned_gap=PlannedGap(gap_id="AUTO-01-G1"),
            results=[
                SourceResult(
                    source_id="jstor",
                    source_type=SourceType.PLAYWRIGHT,
                    query="new q",
                    gap_id="AUTO-01-G1",
                    run_dir=str(new_dir),
                    status="completed",
                )
            ],
            status="completed",
        )
    ]
    export_run_bundle(rec_new, settings)

    # After refresh, new run's folder exists and old one is gone
    gap_folders_new = list((bundle_root / "gaps").iterdir())
    assert gap_folders_new
    new_gap_folder = gap_folders_new[0]
    assert (new_gap_folder / "documents" / "jstor" / "new.json").exists()
    assert not (new_gap_folder / "documents" / "world_bank" / "old.json").exists()
