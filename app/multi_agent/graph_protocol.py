from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


PatchKind = Literal["add_hypothesis", "add_observation_request", "add_intervention",
                    "add_prediction", "challenge", "merge_or_refine", "report_result"]


class PatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str
    kind: PatchKind
    subject_id: str
    payload: dict[str, Any]
    references: list[str] = Field(default_factory=list)


class GraphPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["andromeda.graph-patch/v1"] = "andromeda.graph-patch/v1"
    patch_id: str
    episode_id: str
    workload: Literal["tpcc", "twitter"]
    author: str
    base_version: int = Field(ge=0)
    operations: list[PatchOperation] = Field(min_length=1, max_length=32)
    confidence: float = Field(ge=0, le=1)
    created_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def unique_operations(self):
        ids = [item.operation_id for item in self.operations]
        if len(ids) != len(set(ids)):
            raise ValueError("patch operation ids must be unique")
        return self


class EvidenceBlackboard:
    """Transactional append-only GraphPatch store; experts never mutate it directly."""

    def __init__(self, directory: Path, episode_id: str, workload: str):
        self.directory = directory
        self.episode_id = episode_id
        self.workload = workload
        self.version = 0
        self.known_ids: set[str] = set()
        self.patches: list[dict[str, Any]] = []
        self.ledger = directory / "graph_patches.jsonl"

    def commit(self, patch: GraphPatch) -> int:
        if patch.episode_id != self.episode_id or patch.workload != self.workload:
            raise ValueError("patch episode/workload does not match blackboard")
        if patch.base_version != self.version:
            raise ValueError(f"stale graph patch: expected base_version={self.version}")
        local = set(self.known_ids)
        for operation in patch.operations:
            missing = [item for item in operation.references if item not in local]
            if missing:
                raise ValueError(f"operation {operation.operation_id} references unknown ids: {missing}")
            if operation.operation_id in local or operation.subject_id in local:
                raise ValueError("graph patch may not overwrite an existing id")
            local.update((operation.operation_id, operation.subject_id))
        self.version += 1
        self.known_ids = local
        record = {**patch.model_dump(mode="json"), "committed_version": self.version, "committed_at": utc_now()}
        self.patches.append(record)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._write_snapshot()
        return self.version

    def _write_snapshot(self) -> None:
        value = {"schema_version": "andromeda.evidence-blackboard/v1", "episode_id": self.episode_id,
                 "workload": self.workload, "version": self.version, "known_ids": sorted(self.known_ids),
                 "patches": self.patches}
        target = self.directory / "evidence_graph.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)


class AgentGraph:
    """Bounded communication audit graph, separate from database evidence."""

    def __init__(self, directory: Path, episode_id: str, roles: list[str], topology: str = "blackboard",
                 max_expert_calls_per_round: int = 3, max_messages_per_round: int = 6):
        if topology not in {"blackboard", "coordinator_star"}:
            raise ValueError("unsupported agent communication topology")
        self.path = directory / "agent_graph.json"
        self.value = {"schema_version": "andromeda.agent-graph/v1", "episode_id": episode_id,
                      "topology": topology, "roles": roles, "coordinator": "coordinator",
                      "limits": {"expert_calls_per_round": max_expert_calls_per_round,
                                 "messages_per_round": max_messages_per_round}, "rounds": []}
        self.write()

    def record_round(self, round_number: int, calls: list[dict[str, Any]], messages: list[dict[str, Any]]) -> None:
        limits = self.value["limits"]
        if len(calls) > limits["expert_calls_per_round"] or len(messages) > limits["messages_per_round"]:
            raise ValueError("agent graph round exceeds configured communication bounds")
        self.value["rounds"].append({"round": round_number, "calls": calls, "messages": messages,
                                     "recorded_at": utc_now()})
        self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.value, ensure_ascii=False, indent=2), encoding="utf-8")
