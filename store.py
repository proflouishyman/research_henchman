"""Simple JSON persistence for orchestrator intents, runs, and events."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def now_utc() -> str:
    """Return stable ISO timestamp for audit fields."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OrchestratorStore:
    """File-backed store with coarse-grained lock for orchestrator state.

    Non-obvious logic:
    - Writes use atomic rename via temporary files to avoid partially written
      state when process termination occurs during file I/O.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._paths = {
            "intents": self.root / "intents.json",
            "runs": self.root / "runs.json",
            "events": self.root / "events.json",
        }
        self._ensure_files()

    def _ensure_files(self) -> None:
        for name, path in self._paths.items():
            if path.exists():
                continue
            data: Any = {} if name in {"intents", "runs"} else []
            self._write(path, data)

    def _read(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write(self, path: Path, value: Any) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def upsert_intent(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        intent_id = str(rec.get("intent_id", "")).strip()
        if not intent_id:
            raise ValueError("missing intent_id")
        with self._lock:
            intents = self._read(self._paths["intents"], {})
            existing = intents.get(intent_id, {})
            merged = dict(existing)
            merged.update(rec)
            intents[intent_id] = merged
            self._write(self._paths["intents"], intents)
        return merged

    def get_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            intents = self._read(self._paths["intents"], {})
        return intents.get(intent_id)

    def upsert_run(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(rec.get("run_id", "")).strip()
        if not run_id:
            raise ValueError("missing run_id")
        with self._lock:
            runs = self._read(self._paths["runs"], {})
            existing = runs.get(run_id, {})
            merged = dict(existing)
            merged.update(rec)
            runs[run_id] = merged
            self._write(self._paths["runs"], runs)
        return merged

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            runs = self._read(self._paths["runs"], {})
        return runs.get(run_id)

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            runs = self._read(self._paths["runs"], {})
        rows = list(runs.values())
        rows = sorted(rows, key=lambda x: x.get("created_at", ""), reverse=True)
        return rows[: max(1, limit)]

    def append_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            events = self._read(self._paths["events"], [])
            events.append(event)
            self._write(self._paths["events"], events)
        return event

    def list_events(self, run_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            events = self._read(self._paths["events"], [])
        filtered = [row for row in events if row.get("run_id") == run_id]
        return filtered[-max(1, limit) :]

