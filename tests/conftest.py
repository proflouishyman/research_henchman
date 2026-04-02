"""Shared test fixtures for orchestrator v2."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import OrchestratorSettings


@pytest.fixture
def settings_factory(tmp_path, monkeypatch):
    """Return callable that builds settings from isolated temporary workspace."""

    def _build(**env_overrides):
        workspace = tmp_path / "workspace"
        manuscript_dir = workspace / "Manuscript"
        manuscript_dir.mkdir(parents=True, exist_ok=True)

        defaults = {
            "ORCH_WORKSPACE": str(workspace),
            "ORCH_DATA_ROOT": str(tmp_path / "state"),
            "ORCH_AUTO_INGEST": "true",
            "ORCH_AUTO_LLM_FIT": "true",
            "ORCH_GAP_ANALYSIS_USE_OLLAMA": "false",
            "ORCH_REFLECTION_USE_OLLAMA": "false",
            "ORCH_PULL_OUTPUT_ROOT": "pull_outputs",
        }
        defaults.update({k: str(v) for k, v in env_overrides.items()})
        for key, value in defaults.items():
            monkeypatch.setenv(key, value)

        settings = OrchestratorSettings.from_env()
        settings.data_root.mkdir(parents=True, exist_ok=True)
        settings.gap_map_cache_dir.mkdir(parents=True, exist_ok=True)
        return settings

    return _build


@pytest.fixture
def write_docx():
    """Return helper to write a minimal .docx for extraction tests."""

    import zipfile

    def _write(path: Path, paragraphs: list[str]) -> None:
        body_xml = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in paragraphs)
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body_xml}</w:body>"
            "</w:document>"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)

    return _write
