"""Pydantic contracts for orchestrator API endpoints."""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class IntentCreateInput(BaseModel):
    """User-provided intake payload for building one research intent."""

    input_mode: Literal["manuscript", "search_plan", "both"] = "both"
    manuscript_path: str = ""
    search_plan_path: str = ""
    gap_ids: List[str] = Field(default_factory=list)
    max_queries: int = 100
    notes: str = ""


class RunCreateInput(BaseModel):
    """Run request containing mode/provider and optional existing run handoff."""

    intent_id: str = ""
    pull_mode: Literal["api", "playwright", "auto"] = "auto"
    pull_provider: str = "ebscohost"
    existing_run_id: str = ""
    existing_run_dir: str = ""
    artifact_type: Literal["ebsco_manifest_pair", "external_packet"] = "ebsco_manifest_pair"
    gap_id: str = ""
    force: bool = False


class RetryInput(BaseModel):
    """Retry request for a failed or partially completed run."""

    force: bool = False


class ConnectionSaveInput(BaseModel):
    """Updates to merge into `.env`."""

    updates: Dict[str, str] = Field(default_factory=dict)


class ConnectionSchemaResponse(BaseModel):
    """Connection field schema for the frontend settings form."""

    mode: str
    provider: str
    fields: List[Dict[str, object]]


class RunSummary(BaseModel):
    """Normalized run summary payload for UI polling."""

    run_id: str
    status: str
    stage: str
    created_at: str
    updated_at: str
    payload: Dict[str, object]
    result: Dict[str, object] = Field(default_factory=dict)
    error: Optional[str] = None

