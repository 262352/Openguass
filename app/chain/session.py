from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import EvidenceGraph, GraphEdge, GraphNode, HypothesisRecord, StatusChange, ValidationFrontier, utc_now
from .visualize import render_graph


TERMINAL = {"supported", "refuted", "inconclusive", "superseded"}


class SequentialGraphSession:
    """Append one diagnosis chain, validate it, then permit the next chain."""

    def __init__(self, directory: Path, title: str, graph: EvidenceGraph | None = None):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = directory / "graph_events.jsonl"
        self.hypotheses_path = directory / "hypotheses.jsonl"
        self.frontiers_path = directory / "frontiers.jsonl"
        self.graph = graph or EvidenceGraph(graph_id=directory.name, graph_kind="candidate", title=title, nodes=[], edges=[])
        self.active_contract_id: str | None = None

    @property
    def active_edges(self) -> list[GraphEdge]:
        return [edge for edge in self.graph.edges if edge.status == "candidate"]

    def propose_step(self, step_id: str, nodes: list[GraphNode], edges: list[GraphEdge], contract_id: str,
                     hypothesis: HypothesisRecord | None = None, frontier: ValidationFrontier | None = None) -> None:
        if self.active_contract_id:
            raise RuntimeError("finish the active validation contract before proposing the next step")
        known = {node.node_id for node in self.graph.nodes}
        self.graph.nodes.extend(node for node in nodes if node.node_id not in known)
        for edge in edges:
            if edge.status != "candidate":
                raise ValueError("new model proposals must enter the candidate graph as candidate edges")
            edge.validation_contract_id = contract_id
            self.graph.edges.append(edge)
        self.graph = EvidenceGraph.model_validate(self.graph.model_dump())
        self.active_contract_id = contract_id
        if hypothesis:
            hypothesis.status = "testing"
            hypothesis.contract_ids.append(contract_id)
            if frontier:
                hypothesis.current_frontier = frontier.frontier_id
            self._append(self.hypotheses_path, hypothesis.model_dump())
        if frontier:
            frontier.status = "testing"
            self._append(self.frontiers_path, frontier.model_dump())
        self._event("step_proposed", {"step_id": step_id, "contract_id": contract_id, "edge_ids": [edge.edge_id for edge in edges]})
        self.write(step_id)

    def resolve_step(self, step_id: str, edge_updates: dict[str, dict[str, Any]], experiment_id: str,
                     frontier: ValidationFrontier | None = None) -> None:
        active = {edge.edge_id: edge for edge in self.active_edges}
        if not self.active_contract_id:
            raise RuntimeError("there is no active validation contract")
        unknown = set(edge_updates) - set(active)
        if unknown:
            raise ValueError(f"validation references inactive edges: {sorted(unknown)}")
        if frontier and set(edge_updates) != set(frontier.edge_ids):
            raise ValueError("validation must resolve exactly the selected frontier edges")
        for edge_id, update in edge_updates.items():
            status = update["status"]
            if status not in TERMINAL:
                raise ValueError(f"edge resolution is not terminal: {status}")
            edge = active[edge_id]
            edge.status = status
            edge.experiment_ids.append(experiment_id)
            if status == "supported":
                edge.validation_scope = "supported_under_current_protocol"
                edge.supporting_experiment_ids.append(experiment_id)
            edge.status_history.append(StatusChange(status=status, reason=update["reason"], experiment_id=experiment_id))
        self.graph = EvidenceGraph.model_validate(self.graph.model_dump())
        self.active_contract_id = None
        if frontier:
            frontier.status = "resolved"
            self._append(self.frontiers_path, frontier.model_dump())
        self._event("step_resolved", {"step_id": step_id, "experiment_id": experiment_id, "updates": edge_updates})
        self.write(step_id)

    def _event(self, event: str, payload: dict[str, Any]) -> None:
        self._append(self.events, {"at": utc_now(), "event": event, **payload})

    def record_hypothesis(self, hypothesis: HypothesisRecord, event: str) -> None:
        hypothesis.updated_at = utc_now()
        self._append(self.hypotheses_path, hypothesis.model_dump())
        self._event(event, {"hypothesis_id": hypothesis.hypothesis_id, "revision": hypothesis.revision,
                            "status": hypothesis.status})

    @staticmethod
    def _append(path: Path, value: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")

    def write(self, step_id: str) -> None:
        render_graph(self.graph, self.directory, "candidate_graph")
        render_graph(self.graph.tested(), self.directory, "tested_graph")
        render_graph(self.graph.effective(), self.directory, "effective_graph")
        render_graph(self.graph, self.directory, "candidate_chain")
        render_graph(self.graph.tested(), self.directory, "tested_chain")
        render_graph(self.graph.effective(), self.directory, "effective_chain")
        render_graph(self.graph, self.directory, "diagnosis_graph")
        render_graph(self.graph, self.directory / "steps", step_id)
