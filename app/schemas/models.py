from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class Decision(str, Enum):
    recommend_change = "recommend_change"
    insufficient_evidence = "insufficient_evidence"
    no_safe_change = "no_safe_change"
    manual_action_required = "manual_action_required"


class CandidateKnob(BaseModel):
    knob: str
    rank: int = Field(ge=1)
    evidence_metric_ids: list[str]
    knowledge_source_ids: list[str]
    reason: str
    confidence: float = Field(ge=0, le=1)
    expected_metric_changes: dict[str, Literal["increase", "decrease", "stable"]]
    alternative_explanation: str


class KnobIdentification(BaseModel):
    decision: Decision
    candidates: list[CandidateKnob] = Field(default_factory=list)


class ValueRecommendation(BaseModel):
    decision: Decision
    knob: str | None = None
    current_value: int | float | str | None = None
    recommended_value: int | float | str | None = None
    unit: str | None = None
    scope: Literal["GLOBAL", "SESSION"] | None = None
    dynamic: bool | None = None
    restart_required: bool | None = None
    risk_level: Literal["low", "medium", "high"] | None = None
    rationale: str
    expected_improvement: str
    verification_metrics: list[str] = Field(default_factory=list)
    rollback_value: int | float | str | None = None

    @model_validator(mode="after")
    def require_change_fields(self) -> "ValueRecommendation":
        if self.decision == Decision.recommend_change:
            required = (self.knob, self.current_value, self.recommended_value, self.scope)
            if any(value is None for value in required):
                raise ValueError("recommend_change requires knob, values, and scope")
        return self


class KnowledgeDocument(BaseModel):
    source_id: str
    title: str
    url: str
    mysql_version: str
    section: str
    imported_at: str
    text: str
    knobs: list[str]


class TelemetrySummary(BaseModel):
    metrics: dict[str, float]
    anomalies: list[str]
    samples: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    verdict: Literal["supported", "not_supported", "inconclusive", "experiment_failed"]
    criteria: dict[str, bool]
    explanation: str
