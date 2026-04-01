"""JSON-backed persistence for orchestrator run/event state."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def now_utc() -> str:
    """Return timezone-aware ISO timestamp for persistence fields."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OrchestratorStore:
    """Small file-backed store for runs and events.

    Non-obvious logic:
    - Writes use atomic rename, so interrupted writes do not corrupt JSON.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._paths = {
            "runs": self.root / "runs.json",
            "events": self.root / "events.json",
        }
        self._ensure_files()

    def _ensure_files(self) -> None:
        for key, path in self._paths.items():
            if path.exists():
                continue
            seed: Any = {} if key == "runs" else []
            self._write(path, seed)

    def _read(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write(self, path: Path, value: Any) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def upsert_run(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Insert or update one run row."""

        run_id = str(rec.get("run_id", "")).strip()
        if not run_id:
            raise ValueError("missing run_id")
        with self._lock:
            runs = self._read(self._paths["runs"], {})
            merged = dict(runs.get(run_id, {}))
            merged.update(rec)
            runs[run_id] = merged
            self._write(self._paths["runs"], runs)
        return merged

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return run row by ID, if present."""

        with self._lock:
            runs = self._read(self._paths["runs"], {})
        rec = runs.get(run_id)
        return dict(rec) if isinstance(rec, dict) else None

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return newest runs first."""

        with self._lock:
            runs = self._read(self._paths["runs"], {})
        rows = [dict(v) for v in runs.values() if isinstance(v, dict)]
        rows.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
        return rows[: max(1, int(limit))]

    def append_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Append one event row."""

        with self._lock:
            events = self._read(self._paths["events"], [])
            events.append(event)
            self._write(self._paths["events"], events)
        return event

    def list_events(self, run_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Return newest `limit` events for one run."""

        with self._lock:
            events = self._read(self._paths["events"], [])
        filtered = [row for row in events if row.get("run_id") == run_id]
        return filtered[-max(1, int(limit)) :]
