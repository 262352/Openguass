from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


ExpertRole = Literal["query_execution", "memory_storage", "concurrency_maintenance"]
NodeKind = Literal["context", "intervention", "mechanism", "evidence", "query_effect", "objective", "interaction"]
EdgeState = Literal["proposed", "tested_supported", "tested_refuted", "inconclusive", "stale"]
ReviewKind = Literal["metric_supplement", "competing_explanation", "side_effect", "same_mechanism", "interaction"]


class MetricSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: str
    source: str
    measurement_type: Literal["raw_count", "rate_per_second", "per_transaction", "per_query", "state", "plan"]
    unit: str
    aggregation: str
    sampling_window_seconds: float = Field(gt=0)
    observed_value: float | str | bool | None = None


class KnobChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knob: str
    old_value: str | int | float | bool
    new_value: str | int | float | bool
    numerical_basis: str
    mechanism_role: str


class FrozenPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prediction_id: str
    edge_id: str
    metric_id: str
    expected_direction: Literal["increase", "decrease", "unchanged"]
    minimum_change: float = Field(ge=0)
    comparison_method: Literal["relative_percent", "absolute_delta", "percentage_points", "plan_changed"]
    rationale: str


class AlternativeExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alternative_id: str
    statement: str
    discriminating_metric_id: str
    expected_direction: Literal["increase", "decrease", "unchanged"]


class CostEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    restart_required: bool
    workload_measurements: int = Field(ge=1)
    estimated_seconds: int = Field(ge=1)


class InteractionDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    factor_a_knobs: list[str] = Field(min_length=1)
    factor_b_knobs: list[str] = Field(min_length=1)
    predicted_tps: dict[Literal["baseline", "a_only", "b_only", "a_plus_b"], float]
    comparison_scale: Literal["absolute_tps_additivity"] = "absolute_tps_additivity"
    minimum_nonadditivity_percent: float = Field(default=5.0, gt=0)
    repeats_per_cell: int = Field(default=2, ge=2)

    @model_validator(mode="after")
    def factors_do_not_overlap(self):
        if set(self.factor_a_knobs) & set(self.factor_b_knobs):
            raise ValueError("interaction factors must use disjoint knob sets")
        if set(self.predicted_tps) != {"baseline", "a_only", "b_only", "a_plus_b"}:
            raise ValueError("interaction requires baseline/A/B/A+B predictions")
        return self


class ProposedNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    kind: NodeKind
    label: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class ProposedEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edge_id: str
    source: str
    target: str
    relation: str
    context_hash: str
    prediction_ids: list[str] = Field(min_length=1)


class ExpertProposal(BaseModel):
    """Strict, executable local hypothesis submitted against one incumbent snapshot."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["andromeda.expert-proposal/v1"] = "andromeda.expert-proposal/v1"
    proposal_id: str
    expert: ExpertRole
    incumbent_config_hash: str
    workload_fingerprint_hash: str
    title: str
    anomaly: MetricSpec
    configuration_cause: str
    mechanism: str
    intermediate_metrics: list[MetricSpec] = Field(min_length=1, max_length=6)
    query_effect: str
    expected_tps_effect: str
    expected_latency_effect: str
    knob_changes: list[KnobChange] = Field(default_factory=list, max_length=12)
    predictions: list[FrozenPrediction] = Field(min_length=1, max_length=12)
    alternatives: list[AlternativeExplanation] = Field(min_length=1, max_length=5)
    cross_domain_requirements: list[str] = Field(default_factory=list, max_length=6)
    why_exceeds_incumbent: str
    cost: CostEstimate
    nodes: list[ProposedNode] = Field(min_length=2, max_length=16)
    edges: list[ProposedEdge] = Field(min_length=1, max_length=16)
    interaction_factors: list[str] = Field(default_factory=list, max_length=4)
    interaction_predictions: dict[str, float] = Field(default_factory=dict)
    interaction_design: InteractionDesign | None = None
    created_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def coherent_and_bound(self):
        prediction_ids = {p.prediction_id for p in self.predictions}
        node_ids = {n.node_id for n in self.nodes}
        edge_ids = {e.edge_id for e in self.edges}
        if len(node_ids) != len(self.nodes) or len(edge_ids) != len(self.edges):
            raise ValueError("proposal node and edge ids must be unique")
        for edge in self.edges:
            if edge.source not in node_ids or edge.target not in node_ids:
                raise ValueError(f"edge {edge.edge_id} references an unknown local node")
            if not set(edge.prediction_ids) <= prediction_ids:
                raise ValueError(f"edge {edge.edge_id} references an unknown prediction")
        if not {p.edge_id for p in self.predictions} <= edge_ids:
            raise ValueError("every prediction must bind a proposed edge")
        mechanisms = {n.node_id for n in self.nodes if n.kind == "mechanism"}
        objectives = {n.node_id for n in self.nodes if n.kind == "objective"}
        if not mechanisms or not objectives:
            raise ValueError("proposal requires mechanism and objective nodes")
        if self.knob_changes and not any(n.kind == "intervention" for n in self.nodes):
            raise ValueError("an executable proposal requires an intervention node")
        if self.interaction_factors:
            if not self.interaction_design:
                raise ValueError("interaction requires a pre-registered factorial design")
            action_knobs = {x.knob for x in self.knob_changes}
            factor_knobs = set(self.interaction_design.factor_a_knobs) | set(self.interaction_design.factor_b_knobs)
            if action_knobs != factor_knobs:
                raise ValueError("interaction factors must partition all action knobs")
        return self

    @property
    def executable(self) -> bool:
        return bool(self.knob_changes)

    def merge_signature(self) -> tuple[Any, ...]:
        """Keywords are insufficient: object, mechanism, directions and context must agree."""
        directions = tuple(sorted((p.metric_id, p.expected_direction) for p in self.predictions))
        objects = tuple(sorted(change.knob for change in self.knob_changes))
        return objects, self.mechanism.strip().lower(), directions, self.incumbent_config_hash, self.workload_fingerprint_hash


class CrossReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["andromeda.cross-review/v1"] = "andromeda.cross-review/v1"
    review_id: str
    reviewer: ExpertRole
    proposal_id: str
    kind: ReviewKind
    referenced_metric_ids: list[str] = Field(default_factory=list)
    referenced_experiment_ids: list[str] = Field(default_factory=list)
    statement: str
    discriminating_prediction: FrozenPrediction | None = None
    side_effect_constraints: list[str] = Field(default_factory=list)
    same_mechanism: bool = False
    created_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def references_something(self):
        if not self.proposal_id:
            raise ValueError("review must reference a proposal")
        if self.kind in {"metric_supplement", "competing_explanation", "interaction"} and not (
            self.referenced_metric_ids or self.discriminating_prediction
        ):
            raise ValueError("evidence review requires a metric or discriminating prediction")
        return self


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    kind: NodeKind
    label: str
    context_hash: str
    proposal_ids: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edge_id: str
    source: str
    target: str
    relation: str
    context_hash: str
    proposed_by: list[ExpertRole]
    proposal_ids: list[str]
    frozen_predictions: list[FrozenPrediction]
    validation_experiment_ids: list[str] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    state: EdgeState = "proposed"


class GraphSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["andromeda.incremental-causal-graph/v1"] = "andromeda.incremental-causal-graph/v1"
    graph_kind: Literal["pending", "tested", "effective"]
    version: int = Field(ge=0)
    generated_at: str = Field(default_factory=utc_now)
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class IncumbentSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]
    config_hash: str
    verified_performance: dict[str, Any]
    guardrails: dict[str, Any]
    workload_fingerprint: dict[str, Any]
    workload_fingerprint_hash: str
    database_state: dict[str, Any]
    evidence: dict[str, Any]
    accepted_experiment_id: str
    measured_at: str = Field(default_factory=utc_now)


class ExperimentSelection(BaseModel):
    selected_proposal_id: str | None
    score: float | None = None
    reason: str
    rejected: list[dict[str, str]] = Field(default_factory=list)
    merged_proposal_ids: list[str] = Field(default_factory=list)
    requires_more_observation: bool = False


class ValidationOutcome(BaseModel):
    experiment_id: str
    validity: Literal["valid", "unstable", "invalid", "inconclusive"]
    validity_reasons: list[str]
    performance_verdict: Literal["verified_improvement", "no_improvement", "inconclusive", "invalid"]
    mechanism_verdict: Literal["supported", "refuted", "inconclusive"]
    accepted_as_incumbent: bool
    unexplained_performance: bool
    edge_results: dict[str, dict[str, Any]]
    verified_performance: dict[str, Any] | None = None
    actual_settings_match: bool = False
