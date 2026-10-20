from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from openai import OpenAI
from pydantic import ValidationError

from app.config import load_settings

from .models import CrossReview, ExpertProposal, ExpertRole, utc_now
from .validator import _metric


ROLE_SCOPES: dict[ExpertRole, str] = {
    "query_execution": "critical SQL, EXPLAIN plans, index/sequential scans, estimates, execution latency, statistics and planner/executor knobs",
    "memory_storage": "cache, temp spill, transaction commit latency, WAL sync/write latency, host I/O pressure, background writer, checkpoints and memory/storage knobs",
    "concurrency_maintenance": "SERIALIZABLE retries, predicate-lock promotion, lock waits, conflicts, connections, transaction concurrency, autovacuum and autoanalyze knobs",
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
                                    timeout=settings.timeout_seconds, max_retries=settings.max_retries)

    def _call(self, name: str, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = {"model": self.model, "messages": [{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False,
                                                               sort_keys=True, separators=(",", ":"))}],
                   "response_format": {"type": "json_object"},
                   "temperature": 0, "max_tokens": 8192,
                   "extra_body": {"thinking": {"type": "disabled"}}}
        (self.directory / f"{name}_request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        response = self.client.chat.completions.create(**request)
        raw = response.choices[0].message.content or ""
        (self.directory / f"{name}_response.json").write_text(raw, encoding="utf-8")
        metadata = {"at": utc_now(), "model": self.model, "response_id": getattr(response, "id", None),
                    "finish_reason": getattr(response.choices[0], "finish_reason", None),
                    "usage": getattr(getattr(response, "usage", None), "model_dump", lambda: {})()}
        (self.directory / f"{name}_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return json.loads(raw)

    def propose(self, role: ExpertRole, payload: dict[str, Any]) -> ExpertProposal:
        schema = ExpertProposal.model_json_schema()
        allowed_knobs = payload.get("allowed_knobs", [])
        system = (
            "Return exactly one concise JSON object matching the ExpertProposal schema below. "
            "This is the mechanism-diagnosis phase. Diagnose only the supplied verified incumbent. "
            "Before proposing, inspect latest_experiment_result. It is new shared evidence visible to every expert. "
            "Reconsider your previous mechanism against that intervention and outcome: revise, narrow, withdraw, or "
            "replace a stale action instead of mechanically replaying the previous frontier. "
            "Do not use GP, EI, Bayesian search, historical best configurations or target-reference TPS. "
            "Propose the exact executable knob values that the evidence justifies. The executor will test these exact "
            "values as one coherent mechanism action; it will not replace them with a parameter-group grid point. "
            "Use only allowed knobs and keep every old_value equal to the incumbent. State a defensible exact value and "
            "its bounded range in numerical_basis. Before returning knob_changes, compare every new_value with the "
            "incumbent value. Omit any knob whose "
            "new_value is unchanged; an executable proposal must contain at least one effective change. "
            "For every numeric action, populate recommended_min, recommended_max and "
            "initial_step; the exact new_value must lie inside that range. For categorical actions use the exact value "
            "and leave numeric bounds null. These fields will be frozen for a later bounded search. "
            "Use observed metric IDs and freeze directional predictions. Every prediction metric_id must resolve "
            "to one scalar leaf value in the supplied measurement. Never predict against an aggregate object or "
            "container path: for a gauge such as telemetry.gauges.wal_waiting_backends use an explicit leaf such "
            "as telemetry.gauges.wal_waiting_backends.median, .p95, or .max, matching the stated aggregation. "
            "Do not invent a leaf that is absent from the supplied evidence. "
            "Graph identity is strict: declare short symbolic node_id values such as node_intervention, node_mechanism, "
            "and node_objective. Every edge source and target must copy one of those exact node_id strings. Never put "
            "labels, knob assignments, arrows, units, or descriptive prose in source/target. For example, "
            "source='node_intervention' is valid; source='shared_buffers 8192 -> 32768' is invalid. "
            "Every numeric new_value must be inside the exact low/high bounds in ALLOWED_KNOBS. "
            "Prioritize diagnostic_alerts plus per-transaction terminal_capacity_fraction. Overall p95 may hide a "
            "rare transaction whose latency consumes nearly all terminal time. Low WAL bytes or WAL counts do not rule "
            "out slow fsync; use wal_timing, wait events, and host_io together. When one diagnosed mechanism spans "
            "several knobs, use diagnostic_knowledge as the literature-backed mechanism-to-knob database. For your "
            "mandatory_bundle_by_role entry, an executable proposal MUST contain every knob in required_knobs as one "
            "coherent joint action. Do not reduce the bundle to one representative knob. Choose exact values from the "
            "live ranges and explain each knob's mechanism role. Transaction metrics "
            "use IDs such as client.transaction_latency.InsertTweet.p50_us. Use query_evidence window deltas, "
            "template leaderboards and query wait samples when present. Treat client.server_retry_ratio and PostgreSQL "
            "rollback_ratio as separate evidence: BenchBase server retries are confirmed serialization failures. "
            "For SERIALIZABLE TPC-C, explicitly compare max_pred_locks_per_transaction, "
            "max_pred_locks_per_relation and max_pred_locks_per_page before attributing a retry regime to WAL. "
            "Do not repeat the latest mechanism class unless a new scalar prediction, threshold, or interaction "
            "can distinguish the revised claim. "
            "Read coverage_state.priority_uncovered_knobs before choosing an action. If low_buffer_hit_ratio is "
            "active and shared_buffers is still uncovered, the memory_storage specialist must either propose a "
            "bounded executable shared_buffers mechanism test or return observation-only and identify the exact "
            "missing scalar evidence. Mentioning shared_buffers only inside alternatives is not sufficient. "
            "Likewise, concurrency_maintenance must address uncovered max_pred_locks_* dimensions when the "
            "SERIALIZABLE retry regime is active. "
            "Interpret the latest paired decision causally: reject_equivalent means the direction remains unresolved, "
            "so retry the same mechanism group with a larger safe amplitude; it is not refutation. reject_harmful is "
            "the only performance result that falsifies the tested direction, so reverse the group direction with an "
            "equal or larger safe amplitude. unresolved requires more evidence rather than a direction change. "
            "The sole objective is successful completed transaction Goodput. Latency, retry ratio, and mechanism "
            "metrics explain the result but are not hidden acceptance objectives. When Goodput rises while retry ratio "
            "falls, discuss changed completion semantics instead of treating latency alone as a failed action. "
            "Healthy metrics mean no diagnosed anomaly, not proof of a global optimum. SQL/index opportunities "
            "must remain non-executed requirements. Include an alternative with a different measurable prediction. "
            "If no new action is justified, return an observation-only proposal with knob_changes=[]; never repeat an "
            "action already present in tested_action_summary. Keep title/reason fields under 80 words and mechanism "
            "under 180 words.\nSCHEMA=" + json.dumps(schema, sort_keys=True, separators=(",", ":"))
            + "\nALLOWED_KNOBS=" + json.dumps(allowed_knobs, sort_keys=True, separators=(",", ":"))
            + f"\nSPECIALIST_ROLE={role}; SCOPE={ROLE_SCOPES[role]}"
        )
        dynamic_payload = {key: value for key, value in payload.items()
                           if key not in {"allowed_knobs", "policy"}}
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                retry_payload = dict(dynamic_payload)
                if last_error:
                    retry_payload["previous_validation_error"] = str(last_error)[:1200]
                value = self._call(f"round_{payload['round']:03d}_{role}_proposal_attempt_{attempt}", system, retry_payload)
                value["expert"] = role
                value["incumbent_config_hash"] = payload["current_incumbent"]["config_hash"]
                value["workload_fingerprint_hash"] = payload["current_incumbent"]["workload_fingerprint_hash"]
                for edge in value.get("edges", []):
                    edge["context_hash"] = payload["current_incumbent"]["config_hash"]
                self._normalize_nonclaims(value)
                self._validate_raw_graph_references(value)
                self._namespace_proposal(value, f"r{payload['round']:03d}_{role}")
                proposal = ExpertProposal.model_validate(value)
                self._validate_knob_changes(proposal, payload)
                self._validate_knowledge_bundle(proposal, payload)
                self._validate_adaptation_policy(proposal, payload)
                self._validate_prediction_metric_leaves(proposal, payload.get("current_measurement", {}))
                return proposal
            except (json.JSONDecodeError, ValidationError, KeyError, TypeError, ValueError) as error:
                last_error = error
        raise RuntimeError(f"{role} failed strict ExpertProposal validation after three attempts: {last_error}")

    @staticmethod
    def _validate_raw_graph_references(value: dict[str, Any]) -> None:
        """Turn opaque namespace KeyErrors into actionable retry feedback."""
        node_ids = {item.get("node_id") for item in value.get("nodes", [])}
        edge_ids = {item.get("edge_id") for item in value.get("edges", [])}
        prediction_ids = {item.get("prediction_id") for item in value.get("predictions", [])}
        for edge in value.get("edges", []):
            for field in ("source", "target"):
                if edge.get(field) not in node_ids:
                    raise ValueError(
                        f"edge {edge.get('edge_id')!r} {field}={edge.get(field)!r} is not a declared node_id"
                    )
            unknown = set(edge.get("prediction_ids", [])) - prediction_ids
            if unknown:
                raise ValueError(f"edge {edge.get('edge_id')!r} references unknown prediction_ids {sorted(unknown)}")
        for prediction in value.get("predictions", []):
            if prediction.get("edge_id") not in edge_ids:
                raise ValueError(
                    f"prediction {prediction.get('prediction_id')!r} references unknown edge_id "
                    f"{prediction.get('edge_id')!r}"
                )

    @staticmethod
    def _validate_knob_changes(proposal: ExpertProposal, payload: dict[str, Any]) -> None:
        """Validate names, types, ranges and effective changes before graph admission."""
        allowed = {item["name"]: item for item in payload.get("allowed_knobs", [])}
        incumbent = payload.get("current_incumbent", {}).get("config", {})
        seen: set[str] = set()

        def numeric(value: Any, name: str) -> float:
            if isinstance(value, bool):
                raise ValueError(f"invalid_knob {name}: boolean is not a numeric value")
            try:
                return float(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid_knob {name}: expected a numeric value, got {value!r}") from error

        for change in proposal.knob_changes:
            name = change.knob
            if name in seen:
                raise ValueError(f"invalid_knob {name}: duplicate knob_changes entry")
            seen.add(name)
            if name not in allowed:
                raise ValueError(f"invalid_knob {name}: knob is not in the allowed catalog")
            if name not in incumbent:
                raise ValueError(f"invalid_knob {name}: incumbent value is unavailable")
            spec = allowed[name]
            vartype = spec.get("vartype")
            current = incumbent[name]
            if vartype in {"integer", "real"}:
                old, new = numeric(change.old_value, name), numeric(change.new_value, name)
                actual = numeric(current, name)
                if old != actual:
                    raise ValueError(f"invalid_knob {name}: old_value {change.old_value!r} != incumbent {current!r}")
                if new == actual:
                    raise ValueError(f"invalid_knob {name}: new_value equals incumbent value {current!r}")
                if vartype == "integer" and not new.is_integer():
                    raise ValueError(f"invalid_knob {name}: integer knob received {change.new_value!r}")
                low, high = float(spec["low"]), float(spec["high"])
                if not low <= new <= high:
                    raise ValueError(f"invalid_knob {name}: value {new:g} outside safe range [{low:g}, {high:g}]")
            else:
                old, new, actual = str(change.old_value).lower(), str(change.new_value).lower(), str(current).lower()
                if old != actual:
                    raise ValueError(f"invalid_knob {name}: old_value {change.old_value!r} != incumbent {current!r}")
                if new == actual:
                    raise ValueError(f"invalid_knob {name}: new_value equals incumbent value {current!r}")
                choices = {str(item).lower() for item in spec.get("choices", [])}
                if choices and new not in choices:
                    raise ValueError(f"invalid_knob {name}: value {change.new_value!r} not in {sorted(choices)}")

    @staticmethod
    def _validate_prediction_metric_leaves(proposal: ExpertProposal,
                                           measurement: dict[str, Any]) -> None:
        """Reject known container paths before they consume workload measurements."""
        for prediction in proposal.predictions:
            if prediction.comparison_method == "plan_changed":
                continue
            observed = _metric(measurement, prediction.metric_id)
            if isinstance(observed, (dict, list, tuple, set)):
                available = ", ".join(sorted(str(key) for key in observed)) if isinstance(observed, dict) else ""
                suffix = f"; available leaves: {available}" if available else ""
                raise ValueError(
                    f"prediction metric_id {prediction.metric_id!r} resolves to a container, not a scalar"
                    f"{suffix}; select one explicit leaf metric"
                )

    @staticmethod
    def _validate_knowledge_bundle(proposal: ExpertProposal,
                                   payload: dict[str, Any]) -> None:
        """Prevent an expert from silently shrinking a diagnosed mechanism to one knob."""
        if not proposal.knob_changes:
            return
        mandatory = payload.get("diagnostic_knowledge", {}).get(
            "mandatory_bundle_by_role", {}).get(proposal.expert)
        if not mandatory:
            return
        required = set(mandatory.get("required_knobs", []))
        proposed = {item.knob for item in proposal.knob_changes}
        missing = sorted(required - proposed)
        if missing:
            raise ValueError(
                f"invalid_knob_group {mandatory.get('bundle_id')}: executable proposal must include "
                f"all literature-linked actionable knobs; missing={missing}")

    @staticmethod
    def _validate_adaptation_policy(proposal: ExpertProposal,
                                    payload: dict[str, Any]) -> None:
        """Enforce larger same-direction steps after equivalence and reversal after harm."""
        latest = payload.get("latest_experiment_result") or {}
        if not proposal.knob_changes or latest.get("tested_expert") != proposal.expert:
            return
        policy = latest.get("adaptation_policy")
        if policy not in {"increase_amplitude_in_same_direction",
                          "reverse_direction_with_equal_or_larger_amplitude"}:
            return
        previous = latest.get("tested_changes", {})
        current = payload.get("current_incumbent", {}).get("config", {})
        proposed = {item.knob: item for item in proposal.knob_changes}
        comparable = 0
        violations = []
        for name, old_new in previous.items():
            if name not in proposed or name not in current:
                continue
            try:
                old = float(old_new["old_value"])
                prior_new = float(old_new["new_value"])
                new = float(proposed[name].new_value)
                base = float(current[name])
            except (TypeError, ValueError, KeyError):
                continue
            prior_delta, next_delta = prior_new - old, new - base
            if prior_delta == 0:
                continue
            comparable += 1
            if policy == "increase_amplitude_in_same_direction":
                valid = prior_delta * next_delta > 0 and abs(next_delta) > abs(prior_delta)
            else:
                valid = prior_delta * next_delta < 0 and abs(next_delta) >= abs(prior_delta)
            if not valid:
                violations.append(name)
        if comparable and violations:
            raise ValueError(f"invalid_adaptation {policy}: violating knobs={sorted(violations)}")

    def review(self, role: ExpertRole, payload: dict[str, Any]) -> list[CrossReview]:
        schema = CrossReview.model_json_schema()
        system = (
            "Review only other experts' PostgreSQL proposals. Return one concise JSON object with a reviews array; "
            "every item must match this schema: " + json.dumps(schema, sort_keys=True, separators=(",", ":"))
            + ". Feedback must reference a proposal ID plus a metric/prediction/experiment ID. "
            "Allowed feedback: metric supplement, competing explanation, side effect, exact same mechanism, or pre-registered interaction. "
            "Do not vote and do not rewrite proposals. Keep each statement under 100 words."
            + f" SPECIALIST_ROLE={role}; SCOPE={ROLE_SCOPES[role]}"
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

    def __init__(self, backend: ExpertBackend, roles: tuple[ExpertRole, ...] | None = None):
        self.backend = backend
        self.roles = roles or self.ROLES
        if not self.roles or not set(self.roles) <= set(self.ROLES):
            raise ValueError("expert roles must be a non-empty subset of the supported roles")
        self.last_proposal_failures: list[dict[str, Any]] = []
        self.last_review_failures: list[dict[str, Any]] = []

    def propose_parallel(self, payload: dict[str, Any]) -> list[ExpertProposal]:
        self.last_proposal_failures = []
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="andromeda-expert") as pool:
            futures = [(role, pool.submit(self.backend.propose, role, payload)) for role in self.roles]
            proposals = []
            for role, future in futures:
                try:
                    proposals.append(future.result())
                except Exception as error:
                    message = str(error)[:2000]
                    self.last_proposal_failures.append({
                        "event": "expert_failed", "round": payload.get("round"), "expert": role,
                        "error_type": type(error).__name__, "error": message,
                        "failure_kind": "invalid_knob" if "invalid_knob" in message else "invalid_structure",
                        "at": utc_now(),
                    })
        if not proposals:
            details = "; ".join(f"{item['expert']}: {item['error']}" for item in self.last_proposal_failures)
            raise RuntimeError(f"all experts failed independently: {details}")
        hashes = {(p.incumbent_config_hash, p.workload_fingerprint_hash) for p in proposals}
        if len(hashes) != 1:
            raise RuntimeError("experts did not diagnose the same incumbent snapshot")
        return proposals

    def review_parallel(self, round_number: int, incumbent: dict[str, Any], proposals: list[ExpertProposal]) -> list[CrossReview]:
        def payload(role: ExpertRole) -> dict[str, Any]:
            return {"round": round_number, "current_incumbent": incumbent,
                    "other_proposals": [p.model_dump(mode="json") for p in proposals if p.expert != role]}
        self.last_review_failures = []
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="andromeda-review") as pool:
            futures = [(role, pool.submit(self.backend.review, role, payload(role))) for role in self.roles]
            reviews = []
            for role, future in futures:
                try:
                    reviews.extend(future.result())
                except Exception as error:
                    self.last_review_failures.append({
                        "event": "expert_review_failed", "round": round_number, "expert": role,
                        "error_type": type(error).__name__, "error": str(error)[:2000], "at": utc_now(),
                    })
        by_id = {p.proposal_id: p for p in proposals}
        for review in reviews:
            if review.proposal_id not in by_id:
                raise ValueError(f"review references unknown proposal: {review.proposal_id}")
            if by_id[review.proposal_id].expert == review.reviewer:
                raise ValueError("experts may review only another expert's proposal")
        return reviews
