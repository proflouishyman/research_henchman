"""Layer package for orchestrator v2 pipeline."""

from .analysis import analyze_manuscript
from .fit import fit_gap
from .ingest import ingest_gap_result
from .pull import SOURCE_REGISTRY, build_source_availability, pull_for_plan
from .reflection import reflect_on_gaps

__all__ = [
    "analyze_manuscript",
    "reflect_on_gaps",
    "build_source_availability",
    "pull_for_plan",
    "ingest_gap_result",
    "fit_gap",
    "SOURCE_REGISTRY",
]
