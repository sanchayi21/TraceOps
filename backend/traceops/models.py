"""Shapes of the data TraceOps stores and returns.

The report deliberately separates three things:
  * symptoms   - OBSERVATIONS measured directly from telemetry
  * hypotheses - INFERENCES about where the failure most likely started
  * evidence   - the raw items (metric values, traces, log lines) that both
                 refer to by id, so every claim can be checked
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    id: str  # "E1", "E2", ... referenced by symptoms and hypotheses
    kind: Literal["metric", "trace", "log", "health"]
    service: str | None = None
    title: str
    detail: str
    query: str | None = None  # exact query used, so it can be re-run by hand
    trace_id: str | None = None
    data: dict = Field(default_factory=dict)


class Symptom(BaseModel):
    service: str
    signal: Literal["error_rate", "latency_p95", "availability"]
    description: str
    value: float | None = None
    baseline: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    rank: int
    service: str
    component: str
    failure_type: str
    confidence: float
    confidence_label: Literal["high", "medium", "low"]
    reasoning: str
    evidence_ids: list[str] = Field(default_factory=list)


class PropagationHop(BaseModel):
    service: str
    operation: str
    kind: str
    status: Literal["error", "ok", "slow"]
    http_status: int | None = None
    duration_ms: float
    self_time_ms: float
    is_origin: bool = False
    error: str | None = None


class InvestigationReport(BaseModel):
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    summary: str
    symptoms: list[Symptom]
    hypotheses: list[Hypothesis]
    propagation: list[PropagationHop]
    representative_trace_id: str | None = None
    evidence: list[Evidence]
    uncertainty: list[str]
    stats: dict
    data_sources: dict[str, str]


class AlertRecord(BaseModel):
    fingerprint: str
    alertname: str
    service: str | None = None
    severity: str = "warning"
    summary: str = ""
    status: Literal["firing", "resolved"]
    starts_at: datetime
    ends_at: datetime | None = None


class Explanation(BaseModel):
    provider: str
    model: str | None = None
    text: str
    generated_at: datetime


class Incident(BaseModel):
    id: int
    title: str
    status: Literal["open", "resolved"]
    investigation_status: Literal["pending", "running", "completed", "failed"]
    severity: str
    source: Literal["alertmanager", "manual"]
    services: list[str]
    window_start: datetime
    window_end: datetime
    alerts: list[AlertRecord] = Field(default_factory=list)
    report: InvestigationReport | None = None
    explanation: Explanation | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None


class IncidentSummary(BaseModel):
    """Lightweight row for the incident list."""

    id: int
    title: str
    status: str
    investigation_status: str
    severity: str
    source: str
    services: list[str]
    top_hypothesis: str | None = None
    confidence_label: str | None = None
    top_service: str | None = None
    top_failure_type: str | None = None
    top_confidence: float | None = None
    symptoms: list[Symptom] = Field(default_factory=list)
    created_at: datetime
    resolved_at: datetime | None = None


class ManualInvestigationRequest(BaseModel):
    service: str | None = None
    minutes: int = Field(default=10, ge=1, le=120)
    title: str | None = None
