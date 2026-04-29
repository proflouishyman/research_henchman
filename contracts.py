"""Layer contracts and API models for orchestrator v2.

Rules:
- Layer boundaries use dataclasses from this module.
- Persistence uses JSON-safe dict conversion helpers.
- Enums centralize valid statuses and source categories.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Type, TypeVar, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, Field


# Shared enumerations


class GapType(str, Enum):
    """Gap kind from manuscript analysis."""

    EXPLICIT = "explicit"
    IMPLICIT = "implicit"


class GapPriority(str, Enum):
    """Gap urgency for downstream routing."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SourceType(str, Enum):
    """Supported source execution family."""

    FREE_API = "free_api"
    KEYED_API = "keyed_api"
    PLAYWRIGHT = "playwright"


class ClaimKind(str, Enum):
    """High-level semantic claim category for source routing."""

    HISTORICAL_NARRATIVE = "historical_narrative"
    QUANTITATIVE_MACRO = "quantitative_macro"
    QUANTITATIVE_LABOR = "quantitative_labor"
    LEGAL_REGULATORY = "legal_regulatory"
    COMPANY_OPERATIONS = "company_operations"
    BIOGRAPHICAL = "biographical"
    OTHER = "other"


class EvidenceNeed(str, Enum):
    """Evidence shape required to support the claim."""

    SCHOLARLY_SECONDARY = "scholarly_secondary"
    PRIMARY_SOURCE = "primary_source"
    OFFICIAL_STATISTICS = "official_statistics"
    LEGAL_TEXT = "legal_text"
    NEWS_ARCHIVE = "news_archive"
    MIXED = "mixed"


class RunStatus(str, Enum):
    """Top-level pipeline status values."""

    QUEUED = "queued"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    PULLING = "pulling"
    INGESTING = "ingesting"
    FITTING = "fitting"
    RENDERING = "rendering"
    COMPLETE = "complete"
    FAILED = "failed"
    PARTIAL = "partial"


# Layer 1 output


@dataclass
class Gap:
    """A single evidentiary gap identified in the manuscript."""

    gap_id: str = ""
    chapter: str = ""
    claim_text: str = ""
    gap_type: GapType = GapType.IMPLICIT
    priority: GapPriority = GapPriority.MEDIUM
    suggested_queries: List[str] = field(default_factory=list)
    source_text_excerpt: str = ""
    analysis_method: str = "heuristic"


@dataclass
class GapMap:
    """Full gap analysis output for a manuscript."""

    manuscript_path: str = ""
    manuscript_fingerprint: str = ""
    gaps: List[Gap] = field(default_factory=list)
    section_count: int = 0
    char_count: int = 0
    explicit_count: int = 0
    implicit_count: int = 0
    analysis_method: str = "heuristic"
    analysis_model: str = ""
    fallback_reason: str = ""


# Layer 2 output


@dataclass
class SourceAvailability:
    """Source readiness snapshot built at run start."""

    free_apis: List[str] = field(default_factory=list)
    keyed_apis: List[str] = field(default_factory=list)
    playwright_sources: List[str] = field(default_factory=list)
    missing_keys: Dict[str, str] = field(default_factory=dict)
    playwright_unavailable_reason: str = ""


@dataclass
class PlannedGap:
    """Gap annotated with reflected pull plan fields."""

    gap_id: str = ""
    chapter: str = ""
    claim_text: str = ""
    gap_type: GapType = GapType.IMPLICIT
    priority: GapPriority = GapPriority.MEDIUM
    search_queries: List[str] = field(default_factory=list)
    source_types: List[SourceType] = field(default_factory=list)
    preferred_sources: List[str] = field(default_factory=list)
    rationale: str = ""
    skip: bool = False
    skip_reason: str = ""
    claim_kind: ClaimKind = ClaimKind.OTHER
    evidence_need: EvidenceNeed = EvidenceNeed.MIXED
    route_confidence: float = 0.0
    route_reason: str = ""
    needs_review: bool = False
    review_notes: str = ""
    query_ladder: Dict[str, object] = field(default_factory=dict)
    review_method: str = ""


@dataclass
class ResearchPlan:
    """Reflected research plan from LLM (or heuristic fallback)."""

    run_id: str = ""
    manuscript_path: str = ""
    plan_summary: str = ""
    gaps: List[PlannedGap] = field(default_factory=list)
    estimated_pull_count: int = 0
    reflection_model: str = ""
    reflection_method: str = "heuristic_fallback"
    source_availability: SourceAvailability = field(default_factory=SourceAvailability)
    routing_method: str = "policy_v1"
    review_required_count: int = 0
    review_resolved_count: int = 0
    created_at: str = ""


# Layer 3 output


@dataclass
class SourceResult:
    """One adapter/query execution result."""

    source_id: str = ""
    source_type: SourceType = SourceType.FREE_API
    query: str = ""
    gap_id: str = ""
    document_count: int = 0
    run_dir: str = ""
    artifact_type: str = "json_records"
    status: str = "failed"
    error: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GapPullResult:
    """Aggregated pull results for a single gap."""

    gap_id: str = ""
    planned_gap: PlannedGap = field(default_factory=PlannedGap)
    results: List[SourceResult] = field(default_factory=list)
    total_documents: int = 0
    sources_attempted: List[str] = field(default_factory=list)
    sources_succeeded: List[str] = field(default_factory=list)
    sources_failed: List[str] = field(default_factory=list)
    status: str = "completed"


# Layer 4 output


@dataclass
class IngestResult:
    """Ingest result scoped to one gap's pulled artifacts."""

    gap_id: str = ""
    run_id: str = ""
    ingested: bool = False
    documents_upserted: int = 0
    results_upserted: int = 0
    fit_links_upserted: int = 0
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""


# Layer 5 output


@dataclass
class FitResult:
    """LLM fit result scoped to one gap."""

    gap_id: str = ""
    run_id: str = ""
    links_scored: int = 0
    links_skipped: int = 0
    model: str = ""
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""


# Layer 6 output


@dataclass
class RenderResult:
    """Chart rendering result scoped to one gap."""

    gap_id: str = ""
    run_id: str = ""
    charts_generated: int = 0
    chart_paths: List[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""


@dataclass
class FetchDocumentsResult:
    """Summary of a post-run document fetch pass."""

    items_found: int = 0
    abstracts_saved: int = 0
    seeds_attempted: int = 0
    seeds_ok: int = 0
    seeds_failed: int = 0
    pdfs_attempted: int = 0
    pdfs_ok: int = 0
    pdfs_failed: int = 0
    articles_extracted: int = 0


@dataclass
class RunRecord:
    """Persisted run state. Owns manuscript->plan->execution lifecycle."""

    run_id: str = ""
    manuscript_path: str = ""
    status: RunStatus = RunStatus.QUEUED
    stage_detail: str = ""
    gap_map: Optional[GapMap] = None
    research_plan: Optional[ResearchPlan] = None
    pull_results: List[GapPullResult] = field(default_factory=list)
    ingest_results: List[IngestResult] = field(default_factory=list)
    fit_results: List[FitResult] = field(default_factory=list)
    render_results: List[RenderResult] = field(default_factory=list)
    # Post-run document fetch state (independent of pipeline status)
    fetch_status: str = ""                              # pending | running | complete | failed
    fetch_result: Optional[FetchDocumentsResult] = None
    created_at: str = ""
    updated_at: str = ""
    error: str = ""
    force: bool = False
    pull_timeout_seconds: int = 60


# API request contracts


class RunCreateInput(BaseModel):
    """Run creation payload from UI."""

    manuscript_path: str
    force: bool = False
    pull_timeout_seconds: int = 60


class RetryInput(BaseModel):
    """Retry request for an existing run."""

    force: bool = False
    from_stage: str = ""


class ConnectionSaveInput(BaseModel):
    """`.env` update payload."""

    updates: Dict[str, str] = Field(default_factory=dict)


class SignInPreflightInput(BaseModel):
    """Pre-run manuscript planning payload for sign-in target generation."""

    manuscript_path: str


class SignInTestInput(BaseModel):
    """Provider sign-in probe payload."""

    manuscript_path: str = ""
    source_ids: List[str] = Field(default_factory=list)


class SignInOpenInput(BaseModel):
    """Open provider sign-in pages in attached CDP browser session."""

    source_ids: List[str] = Field(default_factory=list)
    urls: List[str] = Field(default_factory=list)


T = TypeVar("T")


def to_primitive(value: Any) -> Any:
    """Recursively convert dataclass/enum values into JSON-safe primitives."""

    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {k: to_primitive(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): to_primitive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_primitive(v) for v in value]
    return value


def from_primitive(cls: Type[T], data: Any) -> T:
    """Instantiate a dataclass from plain dict/list primitives."""

    if not is_dataclass(cls):
        raise TypeError(f"from_primitive target must be dataclass type: {cls}")
    if not isinstance(data, dict):
        return cls()  # type: ignore[call-arg]

    hints = get_type_hints(cls)
    kwargs: Dict[str, Any] = {}
    for f in fields(cls):
        raw = data.get(f.name)
        field_type = hints.get(f.name, f.type)
        kwargs[f.name] = _coerce_value(field_type, raw)
    return cls(**kwargs)  # type: ignore[call-arg]


def run_record_to_dict(record: RunRecord) -> Dict[str, Any]:
    """Serialize RunRecord to persistence-safe dictionary."""

    return to_primitive(record)


def run_record_from_dict(data: Dict[str, Any]) -> RunRecord:
    """Deserialize persisted run dictionary into RunRecord."""

    return from_primitive(RunRecord, data)


def _coerce_value(tp: Any, raw: Any) -> Any:
    """Best-effort coercion for dataclass field values.

    Assumption:
    - Invalid persisted values should degrade safely to defaults instead of
      raising during run recovery.
    """

    origin = get_origin(tp)
    args = get_args(tp)

    if origin is Union:
        non_none = [arg for arg in args if arg is not type(None)]
        if raw is None:
            return None
        if non_none:
            return _coerce_value(non_none[0], raw)
        return raw

    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(raw)
        except Exception:
            return list(tp)[0]

    if isinstance(tp, type) and is_dataclass(tp):
        if isinstance(raw, dict):
            return from_primitive(tp, raw)
        return tp()  # type: ignore[call-arg]

    if origin in (list, List):
        item_type = args[0] if args else Any
        if not isinstance(raw, list):
            return []
        return [_coerce_value(item_type, item) for item in raw]

    if origin in (dict, Dict):
        key_type = args[0] if args else str
        val_type = args[1] if len(args) > 1 else Any
        if not isinstance(raw, dict):
            return {}
        out: Dict[Any, Any] = {}
        for key, val in raw.items():
            coerced_key = str(key) if key_type in (str, Any) else key
            out[coerced_key] = _coerce_value(val_type, val)
        return out

    if tp is bool:
        return bool(raw)
    if tp is int:
        try:
            return int(raw)
        except Exception:
            return 0
    if tp is float:
        try:
            return float(raw)
        except Exception:
            return 0.0
    if tp is str:
        return "" if raw is None else str(raw)

    return raw
