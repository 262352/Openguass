from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import ExpertProposal, GraphEdge, GraphNode, GraphSnapshot, ValidationOutcome, utc_now


class GraphStore:
    """Versioned pending/tested/effective graphs with an append-only event ledger."""

    def __init__(self, directory: Path, context_hash: str):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.context_hash = context_hash
        self.version = 0
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.ledger = self.directory / "graph_events.jsonl"

    def _append(self, event: str, payload: dict[str, Any]) -> None:
        record = {"at": utc_now(), "event": event, "graph_version": self.version, **payload}
        with self.ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def add_proposals(self, proposals: list[ExpertProposal]) -> None:
        for proposal in proposals:
            if proposal.incumbent_config_hash != self.context_hash:
                raise ValueError("proposal was not generated from the current incumbent")
            for node in proposal.nodes:
                existing = self.nodes.get(node.node_id)
                if existing:
                    if existing.kind != node.kind or existing.label != node.label:
                        raise ValueError(f"conflicting node identity: {node.node_id}")
                    if proposal.proposal_id not in existing.proposal_ids:
                        existing.proposal_ids.append(proposal.proposal_id)
                else:
                    self.nodes[node.node_id] = GraphNode(
                        node_id=node.node_id, kind=node.kind, label=node.label,
                        context_hash=self.context_hash, proposal_ids=[proposal.proposal_id], attributes=node.attributes,
                    )
            predictions = {p.prediction_id: p for p in proposal.predictions}
            for edge in proposal.edges:
                if edge.context_hash != self.context_hash:
                    raise ValueError(f"edge {edge.edge_id} has the wrong context hash")
                if edge.edge_id in self.edges:
                    raise ValueError(f"edge id already exists: {edge.edge_id}")
                self.edges[edge.edge_id] = GraphEdge(
                    edge_id=edge.edge_id, source=edge.source, target=edge.target, relation=edge.relation,
                    context_hash=edge.context_hash, proposed_by=[proposal.expert], proposal_ids=[proposal.proposal_id],
                    frozen_predictions=[predictions[p] for p in edge.prediction_ids],
                )
            self._append("proposal_added", {"proposal": proposal.model_dump(mode="json")})
        self.version += 1
        self.write()

    def record_hypothesis_pool(self, proposals: list[ExpertProposal]) -> None:
        path = self.directory / "hypothesis_pool.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            for proposal in proposals:
                stream.write(json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n")

    def merge_provenance(self, canonical: ExpertProposal, duplicates: list[ExpertProposal]) -> None:
        """Merge only coordinator-approved semantic duplicates; never infer by keywords."""
        for duplicate in duplicates:
            if duplicate.merge_signature() != canonical.merge_signature():
                raise ValueError("only semantically identical proposals can merge")
            for edge in self.edges.values():
                if canonical.proposal_id in edge.proposal_ids:
                    if duplicate.proposal_id not in edge.proposal_ids:
                        edge.proposal_ids.append(duplicate.proposal_id)
                    if duplicate.expert not in edge.proposed_by:
                        edge.proposed_by.append(duplicate.expert)
        self.version += 1
        self._append("proposal_provenance_merged", {
            "canonical": canonical.proposal_id, "contributors": [p.proposal_id for p in duplicates]
        })
        self.write()

    def apply_validation(self, proposal: ExpertProposal, outcome: ValidationOutcome) -> None:
        for edge in proposal.edges:
            stored = self.edges[edge.edge_id]
            result = outcome.edge_results.get(edge.edge_id, {"state": "inconclusive", "reason": "edge was not evaluated"})
            stored.state = result["state"]
            stored.validation_experiment_ids.append(outcome.experiment_id)
            stored.observations.append(deepcopy(result))
        self.version += 1
        self._append("experiment_validated", {
            "proposal_id": proposal.proposal_id, "outcome": outcome.model_dump(mode="json")
        })
        self.write()

    def mark_stale(self, new_context_hash: str, reason: str) -> None:
        changed = []
        for edge in self.edges.values():
            if edge.state in {"proposed", "tested_supported", "inconclusive"}:
                edge.state = "stale"
                changed.append(edge.edge_id)
        self.version += 1
        self._append("context_changed", {"old_context_hash": self.context_hash, "new_context_hash": new_context_hash,
                                         "reason": reason, "stale_edges": changed})
        self.context_hash = new_context_hash
        self.write()

    def snapshot(self, kind: str) -> GraphSnapshot:
        if kind == "pending":
            edges = [e for e in self.edges.values() if e.state == "proposed"]
        elif kind == "tested":
            edges = [e for e in self.edges.values() if e.state != "proposed"]
        elif kind == "effective":
            supported = [e for e in self.edges.values() if e.state == "tested_supported"]
            # A valid effective branch needs a fully supported path from intervention to objective.
            forward = {n.node_id for n in self.nodes.values() if n.kind == "intervention"}
            changed = True
            while changed:
                changed = False
                for edge in supported:
                    if edge.source in forward and edge.target not in forward:
                        forward.add(edge.target); changed = True
            backward = {n.node_id for n in self.nodes.values() if n.kind == "objective"}
            changed = True
            while changed:
                changed = False
                for edge in supported:
                    if edge.target in backward and edge.source not in backward:
                        backward.add(edge.source); changed = True
            connected = forward & backward
            edges = [e for e in supported if e.source in connected and e.target in connected]
        else:
            raise ValueError(kind)
        node_ids = {x for e in edges for x in (e.source, e.target)}
        return GraphSnapshot(graph_kind=kind, version=self.version,
                             nodes=[n for n in self.nodes.values() if n.node_id in node_ids], edges=edges)

    def write(self) -> None:
        version_dir = self.directory / "graph_versions"
        version_dir.mkdir(parents=True, exist_ok=True)
        for kind in ("pending", "tested", "effective"):
            snapshot = self.snapshot(kind)
            target = self.directory / f"{kind}_graph.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(target)
            versioned = version_dir / f"v{self.version:04d}_{kind}.json"
            versioned.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")

    def trace_edge(self, edge_id: str) -> dict[str, Any]:
        edge = self.edges[edge_id]
        return {
            "edge": edge.model_dump(mode="json"),
            "frozen_predictions": [p.model_dump(mode="json") for p in edge.frozen_predictions],
            "experiments": list(edge.validation_experiment_ids), "observations": deepcopy(edge.observations),
        }

    def effective_path_count(self) -> int:
        graph = self.snapshot("effective")
        return len({e.source for e in graph.edges if self.nodes[e.source].kind == "intervention"})
