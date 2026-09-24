from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field

class TrialRecord(BaseModel):
    trial_id: int
    phase: Literal['lhs','gp','baseline','repeat']
    workload: str
    normalized: dict[str,float]
    config: dict[str,int|float|str]
    started_at: str
    duration_seconds: int
    terminals: int
    valid: bool
    throughput_rps: float|None=None
    p95_us: float|None=None
    objective: float|None=None
    error: str|None=None
    artifact: str|None=None

class ReferenceRecord(BaseModel):
    workload: str
    selected_trial_id: int
    config: dict[str,int|float|str]
    objective: float
    one_shot_objective: float | None = None
    selection_method: str = 'single_trial_max'
    shortlist: list[dict[str,Any]] = Field(default_factory=list)
    repeat_results: list[dict[str,Any]]
    distribution: dict[str,float]
    created_at: str=Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class TransitionRecord(BaseModel):
    source: str
    target: str
    status: str
    state_trace: list[str]
    source_config: dict[str,int|float|str]
    target_reference_redacted_until_evaluation: bool=True
    source_result: dict[str,Any]|None=None
    transition_result: dict[str,Any]|None=None
    diagnosis_input: dict[str,Any]|None=None
    composed_question: dict[str,Any]|None=None
    recommendation: dict[str,Any]|None=None
    corrected_result: dict[str,Any]|None=None
    target_reference_result: dict[str,Any]|None=None
    comparison: dict[str,Any]|None=None
    error: str|None=None
