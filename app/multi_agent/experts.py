from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from openai import OpenAI
from pydantic import ValidationError

from app.config import load_settings

from .models import CrossReview, ExpertProposal, ExpertRole, utc_now


ROLE_SCOPES: dict[ExpertRole, str] = {
    "query_execution": "critical SQL, EXPLAIN plans, index/sequential scans, estimates, execution latency, statistics and planner/executor knobs",
    "memory_storage": "cache, temp spill, data-file I/O, background writer, WAL, checkpoints and memory/storage knobs",
    "concurrency_maintenance": "lock waits, conflicts, connections, transaction concurrency, autovacuum and autoanalyze knobs",
}


class ExpertBackend(Protocol):
    def propose(self, role: ExpertRole, payload: dict[str, Any]) -> ExpertProposal: ...
    def review(self, role: ExpertRole, payload: dict[str, Any]) -> list[CrossReview]: ...


class DeepSeekExpertBackend:
    """Audited structured calls; requests contain current incumbent history only."""

    def __init__(self, directory: Path, client: OpenAI | None = None):
        settings = load_settings()
        self.model = settings.model
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client = client or OpenAI(api_key=settings.api_key, base_url=settings.base_url,
                                    timeout=settings.timeout_seconds, max_retries=1)

    def _call(self, name: str, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = {"model": self.model, "messages": [{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                   "response_format": {"type": "json_object"}}
        (self.directory / f"{name}_request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        response = self.client.chat.completions.create(**request)
        raw = response.choices[0].message.content or ""
        (self.directory / f"{name}_response.json").write_text(raw, encoding="utf-8")
        metadata = {"at": utc_now(), "model": self.model, "response_id": getattr(response, "id", None),
                    "usage": getattr(getattr(response, "usage", None), "model_dump", lambda: {})()}
        (self.directory / f"{name}_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return json.loads(raw)

    def propose(self, role: ExpertRole, payload: dict[str, Any]) -> ExpertProposal:
        schema = ExpertProposal.model_json_schema()
        system = (
            f"You are the PostgreSQL {role} specialist. Scope: {ROLE_SCOPES[role]}. "
            "Return one strict JSON ExpertProposal. Diagnose only the supplied current verified incumbent. "
            "Do not use GP, EI, Bayesian search, historical best configurations or target-reference TPS. "
            "Use observed metric IDs, give a coherent mechanism-specific knob group, freeze directional predictions, "
            "and include an alternative with a different measurable prediction. If no action is justified, return an "
            "observation-only proposal with knob_changes=[]; never invent evidence. Schema: " + json.dumps(schema)
        )
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                retry_payload = dict(payload)
                if last_error:
                    retry_payload["previous_validation_error"] = str(last_error)[:1200]
                value = self._call(f"round_{payload['round']:03d}_{role}_proposal_attempt_{attempt}", system, retry_payload)
                value["expert"] = role
                value["incumbent_config_hash"] = payload["current_incumbent"]["config_hash"]
                value["workload_fingerprint_hash"] = payload["current_incumbent"]["workload_fingerprint_hash"]
                for edge in value.get("edges", []):
                    edge["context_hash"] = payload["current_incumbent"]["config_hash"]
                self._normalize_nonclaims(value)
                self._namespace_proposal(value, f"r{payload['round']:03d}_{role}")
                return ExpertProposal.model_validate(value)
            except (json.JSONDecodeError, ValidationError, KeyError, TypeError, ValueError) as error:
                last_error = error
        raise RuntimeError(f"{role} failed strict ExpertProposal validation after three attempts: {last_error}")

    def review(self, role: ExpertRole, payload: dict[str, Any]) -> list[CrossReview]:
        schema = CrossReview.model_json_schema()
        system = (
            f"You are the PostgreSQL {role} specialist doing one cross-domain review. "
            "Review only other experts' proposals. Return JSON object {\"reviews\": [...]} where every item follows this schema: "
            + json.dumps(schema) + ". Feedback must reference a proposal ID plus a metric/prediction/experiment ID. "
            "Allowed feedback: metric supplement, competing explanation, side effect, exact same mechanism, or pre-registered interaction. "
            "Do not vote and do not rewrite proposals."
        )
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                retry_payload = dict(payload)
                if last_error:
                    retry_payload["previous_validation_error"] = str(last_error)[:1200]
                value = self._call(f"round_{payload['round']:03d}_{role}_review_attempt_{attempt}", system, retry_payload)
                reviews = []
                for index, item in enumerate(value.get("reviews", []), 1):
                    item["reviewer"] = role
                    item["review_id"] = f"r{payload['round']:03d}_{role}_review_{index}"
                    reviews.append(CrossReview.model_validate(item))
                return reviews
            except (json.JSONDecodeError, ValidationError, KeyError, TypeError, ValueError) as error:
                last_error = error
        raise RuntimeError(f"{role} failed strict CrossReview validation after three attempts: {last_error}")

    @staticmethod
    def _normalize_nonclaims(value: dict[str, Any]) -> None:
        """Demote incomplete interactions; add representation nodes without inventing evidence."""
        factors = value.get("interaction_factors") or []
        predictions = value.get("interaction_predictions") or {}
        design = value.get("interaction_design")
        if factors and not design:
            requirements = value.setdefault("cross_domain_requirements", [])
            requirements.extend(f"unverified interaction note (demoted to pending constraint): {item}" for item in factors)
            value["interaction_factors"] = []
            value["interaction_predictions"] = {}
            value["interaction_design"] = None
        else:
            value["interaction_factors"] = factors
            value["interaction_predictions"] = predictions
        if not value.get("knob_changes"):
            nodes = value.setdefault("nodes", [])
            kinds = {item.get("kind") for item in nodes}
            if "mechanism" not in kinds:
                nodes.append({"node_id": "observation_only_mechanism", "kind": "mechanism",
                              "label": value.get("mechanism") or "mechanism requires more observation", "attributes": {"observation_only": True}})
            if "objective" not in kinds:
                nodes.append({"node_id": "observation_only_objective", "kind": "objective",
                              "label": "target throughput remains unexplained", "attributes": {"observation_only": True}})

    @staticmethod
    def _namespace_proposal(value: dict[str, Any], prefix: str) -> None:
        value["proposal_id"] = prefix
        node_map = {item["node_id"]: f"{prefix}_{item['node_id']}" for item in value.get("nodes", [])}
        edge_map = {item["edge_id"]: f"{prefix}_{item['edge_id']}" for item in value.get("edges", [])}
        prediction_map = {item["prediction_id"]: f"{prefix}_{item['prediction_id']}" for item in value.get("predictions", [])}
        for item in value.get("nodes", []): item["node_id"] = node_map[item["node_id"]]
        for item in value.get("edges", []):
            item["edge_id"] = edge_map[item["edge_id"]]
            item["source"] = node_map[item["source"]]
            item["target"] = node_map[item["target"]]
            item["prediction_ids"] = [prediction_map[x] for x in item.get("prediction_ids", [])]
        for item in value.get("predictions", []):
            item["prediction_id"] = prediction_map[item["prediction_id"]]
            item["edge_id"] = edge_map[item["edge_id"]]


class ExpertPanel:
    ROLES: tuple[ExpertRole, ...] = ("query_execution", "memory_storage", "concurrency_maintenance")

    def __init__(self, backend: ExpertBackend):
        self.backend = backend

    def propose_parallel(self, payload: dict[str, Any]) -> list[ExpertProposal]:
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="andromeda-expert") as pool:
            futures = [pool.submit(self.backend.propose, role, payload) for role in self.ROLES]
            proposals = [future.result() for future in futures]
        hashes = {(p.incumbent_config_hash, p.workload_fingerprint_hash) for p in proposals}
        if len(hashes) != 1:
            raise RuntimeError("experts did not diagnose the same incumbent snapshot")
        return proposals

    def review_parallel(self, round_number: int, incumbent: dict[str, Any], proposals: list[ExpertProposal]) -> list[CrossReview]:
        def payload(role: ExpertRole) -> dict[str, Any]:
            return {"round": round_number, "current_incumbent": incumbent,
                    "other_proposals": [p.model_dump(mode="json") for p in proposals if p.expert != role]}
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="andromeda-review") as pool:
            futures = [pool.submit(self.backend.review, role, payload(role)) for role in self.ROLES]
            reviews = [review for future in futures for review in future.result()]
        by_id = {p.proposal_id: p for p in proposals}
        for review in reviews:
            if review.proposal_id not in by_id:
                raise ValueError(f"review references unknown proposal: {review.proposal_id}")
            if by_id[review.proposal_id].expert == review.reviewer:
                raise ValueError("experts may review only another expert's proposal")
        return reviews
