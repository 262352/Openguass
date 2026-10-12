from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


NodeType = Literal[
    "knob", "workload_context", "mechanism", "evidence_metric",
    "target_metric", "intervention", "experiment_result", "query_behavior",
]
NodeStatus = Literal["observed", "hypothesized", "candidate", "supported", "refuted", "inconclusive", "superseded"]
EdgeStatus = Literal["candidate", "supported", "refuted", "inconclusive", "superseded"]
HypothesisStatus = Literal[
    "proposed", "testing", "partially_supported", "revising", "supported", "refuted",
    "abandoned", "closed", "unresolved",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GraphNode(BaseModel):
    node_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$")
    type: NodeType
    name: str
    description: str
    source_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)
    status: NodeStatus
    metadata: dict[str, Any] = Field(default_factory=dict)
    metric_id: str | None = None
    metric_source: str | None = None
    sampling_window: dict[str, Any] | None = None
    baseline_value: float | None = None
    current_value: float | None = None
    aggregation: str | None = None
    direction: str | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def evidence_fields_are_real(self):
        if self.type == "evidence_metric":
            required = (self.metric_id, self.metric_source, self.sampling_window, self.aggregation, self.direction, self.unit)
            if any(value is None or value == "" for value in required):
                raise ValueError("evidence_metric requires metric id, source, window, aggregation, direction and unit")
            if self.current_value is None:
                raise ValueError("evidence_metric requires a measured current_value")
        return self


class StatusChange(BaseModel):
    status: EdgeStatus
    at: str = Field(default_factory=utc_now)
    reason: str
    experiment_id: str | None = None


class ValidationFrontier(BaseModel):
    """The small, explicit portion of a hypothesis that one contract can test."""

    frontier_id: str
    hypothesis_id: str
    selected_node_ids: list[str] = Field(default_factory=list)
    selected_edge_ids: list[str] = Field(min_length=1)
    evidence_metric_ids: list[str] = Field(default_factory=list)
    reason_for_selection: str
    contract_id: str | None = None
    status: Literal["proposed", "testing", "resolved", "blocked"] = "proposed"

    @property
    def edge_ids(self) -> list[str]:
        return self.selected_edge_ids


class HypothesisRecord(BaseModel):
    hypothesis_id: str
    parent_hypothesis_id: str | None = None
    revision: int = Field(default=1, ge=1)
    status: HypothesisStatus = "proposed"
    title: str
    statement: str
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    initial_state_id: str
    current_state_id: str
    current_frontier: str | None = None
    candidate_edge_ids: list[str] = Field(default_factory=list)
    supported_edge_ids: list[str] = Field(default_factory=list)
    refuted_edge_ids: list[str] = Field(default_factory=list)
    contract_ids: list[str] = Field(default_factory=list)
    experiment_ids: list[str] = Field(default_factory=list)
    accepted_intervention_ids: list[str] = Field(default_factory=list)
    revision_reason: str | None = None
    closure_reason: str | None = None


class GraphEdge(BaseModel):
    edge_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$")
    cause_node_id: str
    effect_node_id: str
    relation: str
    mechanism_text: str
    status: EdgeStatus = "candidate"
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)
    validation_contract_id: str | None = None
    experiment_ids: list[str] = Field(default_factory=list)
    status_history: list[StatusChange] = Field(default_factory=list)
    validation_scope: str | None = None
    supporting_experiment_ids: list[str] = Field(default_factory=list)


class EvidenceGraph(BaseModel):
    schema_version: str = "andromeda.evidence-graph/v1"
    graph_id: str
    graph_kind: Literal["candidate", "tested", "effective", "combined"]
    title: str
    generated_at: str = Field(default_factory=utc_now)
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references_exist_and_are_unique(self):
        node_ids = [node.node_id for node in self.nodes]
        edge_ids = [edge.edge_id for edge in self.edges]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("duplicate graph node id")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate graph edge id")
        known = set(node_ids)
        for edge in self.edges:
            if edge.cause_node_id not in known or edge.effect_node_id not in known:
                raise ValueError(f"edge {edge.edge_id} references an unknown node")
            if edge.cause_node_id == edge.effect_node_id:
                raise ValueError(f"edge {edge.edge_id} is a self-loop")
        return self

    def effective(self) -> "EvidenceGraph":
        supported = [edge for edge in self.edges if edge.status == "supported"]
        # Keep only supported paths that actually reach the optimization target.
        reachable = {node.node_id for node in self.nodes if node.type == "target_metric"}
        changed = True
        while changed:
            changed = False
            for edge in supported:
                if edge.effect_node_id in reachable and edge.cause_node_id not in reachable:
                    reachable.add(edge.cause_node_id)
                    changed = True
        forward = {node.node_id for node in self.nodes if node.type == "intervention"}
        changed = True
        while changed:
            changed = False
            for edge in supported:
                if edge.cause_node_id in forward and edge.effect_node_id not in forward:
                    forward.add(edge.effect_node_id)
                    changed = True
        connected = reachable & forward
        edges = [edge for edge in supported if edge.cause_node_id in connected and edge.effect_node_id in connected]
        used = {value for edge in edges for value in (edge.cause_node_id, edge.effect_node_id)}
        nodes = [node for node in self.nodes if node.node_id in used]
        return EvidenceGraph(
            graph_id=f"{self.graph_id}:effective", graph_kind="effective",
            title=f"{self.title} — 已验证子图", nodes=nodes, edges=edges,
            metadata={**self.metadata, "derived_from": self.graph_id, "validation_mode": "paired_independent_retest"},
        )

    def tested(self) -> "EvidenceGraph":
        edges = [edge for edge in self.edges if edge.status in {"supported", "refuted", "inconclusive"}]
        used = {value for edge in edges for value in (edge.cause_node_id, edge.effect_node_id)}
        return EvidenceGraph(graph_id=f"{self.graph_id}:tested", graph_kind="tested",
            title=f"{self.title} — 已测试链", nodes=[n for n in self.nodes if n.node_id in used], edges=edges,
            metadata={**self.metadata, "derived_from": self.graph_id})
