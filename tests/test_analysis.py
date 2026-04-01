"""Layer 1 analysis tests."""

from __future__ import annotations

from pathlib import Path

from app.layers import analysis


def test_analysis_heuristic_detects_explicit_and_implicit(settings_factory, write_docx) -> None:
    settings = settings_factory(ORCH_GAP_ANALYSIS_USE_OLLAMA="false")
    manuscript = Path(settings.workspace) / "Manuscript" / "chapter_one.docx"
    write_docx(
        manuscript,
        [
            "Chapter One: Merchant",
            "TODO add citation for this claim.",
            "This section likely suggests market dominance but has no citations or numbers.",
        ],
    )

    out = analysis.analyze_manuscript("Manuscript/chapter_one.docx", settings, refresh=True)

    assert out.manuscript_path == "Manuscript/chapter_one.docx"
    assert out.gaps
    assert out.explicit_count >= 1
    assert out.implicit_count >= 1


def test_analysis_falls_back_when_ollama_errors(settings_factory, monkeypatch) -> None:
    settings = settings_factory(ORCH_GAP_ANALYSIS_USE_OLLAMA="true")
    manuscript = Path(settings.workspace) / "Manuscript" / "chapter_one.txt"
    manuscript.write_text("Chapter One\nClaim without source and maybe could suggest risk.", encoding="utf-8")

    def _boom(**_kwargs):
        raise RuntimeError("ollama unavailable")

    monkeypatch.setattr(analysis, "_call_ollama", _boom)
    out = analysis.analyze_manuscript("Manuscript/chapter_one.txt", settings, refresh=True)

    assert out.analysis_method == "heuristic"
    assert "ollama unavailable" in out.fallback_reason
    assert out.gaps
