"""Base adapter contract for orchestrator source pulls."""

from __future__ import annotations

from abc import ABC, abstractmethod

from contracts import PlannedGap, SourceAvailability, SourceResult


class PullAdapter(ABC):
    """Interface implemented by all source pull adapters.

    Rules:
    - Pull methods return `SourceResult` and never raise to router.
    - Pull methods must respect timeout controls from caller.
    """

    source_id: str = ""
    source_type: str = ""

    @abstractmethod
    def is_available(self, availability: SourceAvailability) -> bool:
        """Return true when adapter can execute under current source availability."""

    @abstractmethod
    def pull(
        self,
        gap: PlannedGap,
        query: str,
        run_dir: str,
        timeout_seconds: int = 60,
    ) -> SourceResult:
        """Execute one query and return a typed result regardless of outcome."""

    def validate(self, availability: SourceAvailability) -> str:
        """Return empty string when ready; otherwise human-readable reason."""

        return ""
