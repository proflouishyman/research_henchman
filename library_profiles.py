"""Library profile loading for university-specific Playwright source routing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .config import OrchestratorSettings


DEFAULT_PROFILE_PATH = Path(__file__).resolve().parent / "library_profiles.default.json"


def _read_json(path: Path) -> Dict[str, Any]:
    """Read JSON file and return dict, or empty dict on parse failure."""

    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
        return {}
    except Exception:
        return {}


def _normalize_db_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one university database row into stable API shape."""

    source_id = str(row.get("source_id", "")).strip().lower()
    name = str(row.get("name", source_id or "database")).strip()
    url = str(row.get("url", "")).strip()

    categories = row.get("categories", [])
    categories_list = [str(item).strip() for item in categories if str(item).strip()] if isinstance(categories, list) else []

    claim_kinds = row.get("claim_kinds", [])
    claim_kinds_list = [str(item).strip() for item in claim_kinds if str(item).strip()] if isinstance(claim_kinds, list) else []

    evidence_needs = row.get("evidence_needs", [])
    evidence_needs_list = [str(item).strip() for item in evidence_needs if str(item).strip()] if isinstance(evidence_needs, list) else []

    return {
        "source_id": source_id,
        "name": name,
        "url": url,
        "categories": categories_list,
        "claim_kinds": claim_kinds_list,
        "evidence_needs": evidence_needs_list,
        "source_type": str(row.get("source_type", "playwright")).strip().lower() or "playwright",
        "provider": str(row.get("provider", "library")).strip() or "library",
    }


def load_library_profiles(settings: OrchestratorSettings) -> Dict[str, Any]:
    """Load profile JSON from configured path, falling back to bundled defaults."""

    configured = _read_json(settings.library_profiles_path)
    if configured.get("systems"):
        return configured

    bundled = _read_json(DEFAULT_PROFILE_PATH)
    if bundled.get("systems"):
        return bundled

    return {"systems": {}}


def get_active_library_profile(settings: OrchestratorSettings) -> Dict[str, Any]:
    """Return selected library system profile from loaded profile set."""

    payload = load_library_profiles(settings)
    systems = payload.get("systems", {}) if isinstance(payload, dict) else {}
    if not isinstance(systems, dict):
        systems = {}

    selected = str(settings.library_system or "").strip().lower()
    if selected and selected in systems and isinstance(systems[selected], dict):
        row = dict(systems[selected])
        row.setdefault("key", selected)
        return row

    # Fallback priority: generic -> first available
    if "generic" in systems and isinstance(systems["generic"], dict):
        row = dict(systems["generic"])
        row.setdefault("key", "generic")
        return row

    for key, val in systems.items():
        if isinstance(val, dict):
            row = dict(val)
            row.setdefault("key", str(key))
            return row

    return {"key": "none", "name": "None", "databases": []}


def get_active_university_databases(settings: OrchestratorSettings) -> List[Dict[str, Any]]:
    """Return normalized database rows for the selected library profile."""

    profile = get_active_library_profile(settings)
    rows = profile.get("databases", []) if isinstance(profile, dict) else []
    if not isinstance(rows, list):
        rows = []

    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = _normalize_db_row(row)
        if not normalized["source_id"]:
            continue
        normalized["library_system"] = str(profile.get("key", ""))
        normalized["library_name"] = str(profile.get("name", ""))
        out.append(normalized)
    return out


def get_active_playwright_source_ids(settings: OrchestratorSettings) -> List[str]:
    """Return Playwright source IDs enabled for the selected library profile."""

    ids = [row["source_id"] for row in get_active_university_databases(settings) if row.get("source_type") == "playwright"]
    ids.extend([str(source).strip().lower() for source in settings.playwright_extra_sources if str(source).strip()])

    out: List[str] = []
    seen = set()
    for source_id in ids:
        if source_id in seen:
            continue
        seen.add(source_id)
        out.append(source_id)
    return out
