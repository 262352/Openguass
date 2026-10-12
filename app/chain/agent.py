from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import load_settings
from app.phase2.space import CATALOG, FullKnobSpace
from .contracts import CounterfactualContract, InterventionGroup, KnobAction, Prediction
from .models import GraphEdge, GraphNode, HypothesisRecord, ValidationFrontier


FORBIDDEN_ONLINE_TERMS = (
    "tps_ref", "reference_tps", "reference_config", "initial_gap", "current_gap",
    "coverage", "no_tps_gap", "gp_label", "best_known", "target_reference",
)


def _knob_family(name: str) -> str:
    if name.startswith(("autovacuum", "vacuum")):
        return "vacuum_and_statistics"
    if name.startswith(("wal", "checkpoint", "commit")) or name in {"min_wal_size", "max_wal_size"}:
        return "wal_commit_and_checkpoint"
    if name.startswith("bgwriter") or name == "backend_flush_after":
        return "buffer_writeback"
    if "mem" in name or name in {"shared_buffers", "effective_cache_size"}:
        return "memory_and_cache"
    if "parallel" in name or "worker" in name:
        return "parallelism_and_workers"
    if "lock" in name or name == "deadlock_timeout":
        return "locking"
    if name.startswith("log") or name == "logging_collector":
        return "logging_overhead"
    if name in {"random_page_cost", "seq_page_cost", "jit", "default_statistics_target",
                "from_collapse_limit", "join_collapse_limit", "effective_io_concurrency"}:
        return "planner_and_execution"
    return "capacity_or_replication"


KNOB_METADATA = {item["name"]: item for item in json.loads(CATALOG.read_text(encoding="utf-8"))["knobs"]}


class ProposedKnobChange(BaseModel):
    knob: str
    proposed_value: str | int | float | bool
    numerical_basis: str
    mechanism_role: str
    activation_check: str

    @model_validator(mode="after")
    def executable(self):
        if not all((self.knob, str(self.proposed_value), self.numerical_basis,
                    self.mechanism_role, self.activation_check)):
            raise ValueError("every knob-group item requires a value, basis, role and activation check")
        return self


class AgentDecision(BaseModel):
    hypothesis_action: Literal["continue", "revise", "branch", "abandon", "close_hypothesis", "propose_next_bottleneck", "stop"]
    active_hypothesis_id: str | None = None
    result_interpretation: str = ""
    resolved_evidence_ids: list[str] = Field(default_factory=list)
    persistent_evidence_ids: list[str] = Field(default_factory=list)
    emerged_evidence_ids: list[str] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)
    remaining_bottleneck_assessment: str = ""
    stop_decision: Any = None
    graph_patch: Any = Field(default_factory=dict)
    frontier: Any = Field(default_factory=dict)
    next_contract: Any = Field(default_factory=dict)
    stop_reason: str | None = None
    parent_hypothesis_id: str | None = None
    hypothesis: str = ""
    mechanism: str = ""
    evidence_metric_id: str = ""
    evidence_expected_direction: Literal["increase", "decrease", "unchanged"] = "decrease"
    evidence_minimum_change_percent: float = Field(default=5, ge=0)
    knob: str = ""
    proposed_value: str | int | float | bool = ""
    knob_to_evidence_reason: str = ""
    evidence_to_tps_reason: str = ""
    confidence: float = Field(default=.5, ge=0, le=1)
    risk: str = ""
    why_exceeds_incumbent: str = ""
    mechanism_measurement: str = ""
    activation_check: str = ""
    resource_guardrails: list[str] = Field(default_factory=list)
    knob_group: list[ProposedKnobChange] = Field(default_factory=list)
    group_name: str = ""
    coupling_rationale: str = ""
    ranked_candidate_groups: list[str] = Field(default_factory=list)
    expected_tps_impact_percent: float = Field(default=0, ge=0)
    numerical_basis: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value):
        if isinstance(value, str):
            normalized = value.strip().lower()
            qualitative = {"low": 0.25, "medium": 0.5, "moderate": 0.5, "high": 0.75}
            if normalized in qualitative:
                return qualitative[normalized]
        return value

    @field_validator(
        "resource_guardrails", "alternative_explanations", "resolved_evidence_ids",
        "persistent_evidence_ids", "emerged_evidence_ids", "ranked_candidate_groups", mode="before",
    )
    @classmethod
    def normalize_string_lists(cls, value):
        """DeepSeek sometimes emits one item as a scalar despite JSON-schema instructions."""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [value]
        return value

    @model_validator(mode="after")
    def action_is_complete(self):
        if self.hypothesis_action == "stop":
            if self.stop_reason not in {"no_actionable_hypothesis", "blocked_by_model"}:
                raise ValueError("stop requires an allowed stop_reason")
        elif self.hypothesis_action in {"abandon", "close_hypothesis"}:
            if not self.result_interpretation:
                raise ValueError("closing an active hypothesis requires an interpretation")
        elif not all((self.hypothesis, self.mechanism, self.evidence_metric_id,
                      self.knob_to_evidence_reason, self.evidence_to_tps_reason, self.risk)):
            raise ValueError("an executable proposal requires every diagnosis field")
        elif not all((self.why_exceeds_incumbent, self.mechanism_measurement, self.activation_check,
                      self.alternative_explanations, self.resource_guardrails)):
            raise ValueError("an executable proposal requires incumbent rationale, measurement, alternative, activation and guardrails")
        elif not self.knob_group:
            raise ValueError("an executable proposal requires one coherent knob group")
        elif not all((self.group_name, self.coupling_rationale, self.ranked_candidate_groups)):
            raise ValueError("an executable proposal requires a named group, coupling rationale and ranked candidate groups")
        elif self.ranked_candidate_groups[0] != self.group_name:
            raise ValueError("the selected knob group must be first in the expected-impact ranking")
        return self


def assert_no_online_leakage(value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False).lower()
    hits = [term for term in FORBIDDEN_ONLINE_TERMS if re.search(rf"\b{re.escape(term)}\b", text)]
    if hits:
        raise ValueError(f"online decision payload contains forbidden historical-reference fields: {hits}")


class SingleAgentChainDiagnoser:
    """One persistent logical agent: each call sees the same chain history and proposes one next test."""

    def __init__(self, artifact_dir: Path, client: OpenAI | None = None):
        self.dir = artifact_dir / "llm"
        self.dir.mkdir(parents=True, exist_ok=True)
        settings = load_settings()
        self.model = settings.model
        if self.model != "deepseek-flash" or settings.base_url.rstrip("/") != "https://api.deepseek.com":
            raise RuntimeError("the v3/v4 chain requires deepseek-flash at https://api.deepseek.com")
        self.client = client or OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=settings.timeout_seconds, max_retries=1)

    def decide(self, step: int, source: str, target: str, state: dict[str, Any], history: list[dict[str, Any]],
               space: FullKnobSpace, active_hypothesis: dict[str, Any] | None = None,
               remaining_budget: dict[str, Any] | None = None, candidate_graph: dict[str, Any] | None = None,
               effective_graph: dict[str, Any] | None = None) -> AgentDecision:
        allowed_metrics = sorted(self._metric_values(state).keys())
        catalog = [{"name": d.name, "current": state["config"].get(d.name), "low": d.low, "high": d.high,
                    "choices": list(d.choices), "execution_class": d.execution_class,
                    "range_position": round(d.encode(state["config"].get(d.name, d.current)), 4),
                    "mechanism_family": _knob_family(d.name),
                    "description": KNOB_METADATA.get(d.name, {}).get("short_desc", "")} for d in space.dimensions]
        measurement = state["measurement"]
        concise_measurement = {"throughput_rps": measurement.get("throughput_rps"), "p95_us": measurement.get("p95_us"),
                               "telemetry": measurement.get("telemetry", {}), "plan_evidence": measurement.get("plan_evidence", {})}
        incumbent = state.get("incumbent", {})
        concise_incumbent = {key: incumbent.get(key) for key in (
            "config_hash", "verified_performance", "accepted_experiment_id", "accepted_at", "evidence_summary",
            "workload_fingerprint", "database_state", "experiment_conditions",
        ) if key in incumbent}
        payload = {"research_question": "Rank coherent mechanism-specific knob groups by expected TPS impact, then test exactly one configuration for the highest-impact evidence-backed group.",
                   "source_workload": source, "target_workload": target,
                   "workload_fingerprints": state.get("workload_fingerprints", {}),
                   "current_target_measurement": concise_measurement,
                   "current_incumbent": concise_incumbent,
                   "initial_target_performance": state.get("initial_target_performance"),
                   "current_config": state["config"], "allowed_evidence_metrics": allowed_metrics,
                   "knowledge_results": {"postgresql_14_knob_catalog": catalog}, "prior_chain_events": history[-8:],
                   "candidate_graph": candidate_graph or {}, "effective_graph": effective_graph or {},
                   "active_hypothesis": active_hypothesis, "remaining_budget": remaining_budget or {},
                   "required_output_fields": ["hypothesis_action", "active_hypothesis_id", "result_interpretation",
                       "parent_hypothesis_id", "hypothesis", "mechanism", "evidence_metric_id",
                       "evidence_expected_direction", "evidence_minimum_change_percent", "knob_group", "group_name",
                       "coupling_rationale", "ranked_candidate_groups",
                       "knob_to_evidence_reason", "evidence_to_tps_reason", "confidence", "risk",
                       "why_exceeds_incumbent", "mechanism_measurement", "activation_check", "resource_guardrails",
                       "expected_tps_impact_percent",
                       "resolved_evidence_ids", "persistent_evidence_ids", "emerged_evidence_ids",
                       "alternative_explanations", "remaining_bottleneck_assessment", "stop_decision", "stop_reason",
                       "graph_patch", "frontier", "next_contract"],
                   "field_constraints": {"confidence": "JSON number from 0.0 to 1.0 (never low/medium/high)",
                       "evidence_minimum_change_percent": "non-negative JSON number",
                       "stop_reason": "empty string unless hypothesis_action is stop; then no_actionable_hypothesis or blocked_by_model",
                       "knob_group": "JSON array with 1 or more changes. Every item requires knob, proposed_value, numerical_basis, mechanism_role, activation_check. Include dependent knobs needed to keep the group internally valid. Use multiple knobs when they jointly control the diagnosed mechanism; do not mix unrelated mechanisms.",
                       "group_name": "short stable mechanism-group name",
                       "coupling_rationale": "why these knobs must be changed and evaluated together for this mechanism",
                       "ranked_candidate_groups": "JSON array of 3-8 short mechanism-group names ordered by expected TPS impact; group_name must be first",
                       "expected_tps_impact_percent": "non-negative estimate used for ranking, not an acceptance threshold",
                       "resource_guardrails": "JSON array of one or more short strings, never a scalar string",
                       "alternative_explanations": "JSON array of one or more strings"}}
        assert_no_online_leakage(payload)
        system = (
            "You are the single PostgreSQL diagnosis agent in a sequential counterfactual loop. "
            "Return strict JSON matching the requested schema. Rank 3-8 coherent knob groups by expected TPS impact, then propose one causal explanation and one executable group configuration for the highest-ranked evidence-backed mechanism. "
            "Use only a measured metric from allowed_evidence_metrics. The chain is workload context -> mechanism -> measured evidence or plan behavior -> target TPS. "
            "Select all knobs that jointly control that one mechanism and give one catalog-valid value for each. A group may contain one knob only when the mechanism is truly isolated. Do not bundle independent mechanisms. The executor tests exactly this one group configuration in one C1/T1/C2/T2 experiment. Do not invent metrics. Do not discuss prior optimums, comparison tuners, or external target values. "
            "Explain why the action can exceed the incumbent, how the mechanism is independently measured, how activation is checked, alternatives, resource guardrails and rollback. "
            "Raw xact_commit, idx_scan, or seq_scan rates rise mechanically with throughput and cannot prove a mechanism; use a per_transaction metric or plan evidence. "
            "Statistics/autovacuum claims require an observed ANALYZE/autoanalyze event. Performance acceptance is independent from mechanism support. "
            "On the first step use propose_next_bottleneck. With an active hypothesis use continue, revise, branch, abandon, or close_hypothesis. "
            "Do not stop while screening_status says too few unique mechanism groups have been tested or ranked_group_backlog is non-empty. Choose stop only when no measured evidence supports any safe group action after required screening. Think briefly and emit the JSON promptly. Keep every text field concise. "
            "graph_patch/frontier/next_contract are concise audit summaries; execution is schema-validated separately."
        )
        user = json.dumps(payload, ensure_ascii=False)
        (self.dir / f"step_{step:03d}_request.json").write_text(json.dumps({"system": system, "payload": payload}, ensure_ascii=False, indent=2), encoding="utf-8")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error: Exception | None = None
        for attempt in (1, 2, 3):
            response = self.client.chat.completions.create(
                model=self.model, messages=messages, response_format={"type": "json_object"}, temperature=0, max_tokens=8192,
            )
            choice = response.choices[0]
            raw = choice.message.content or ""
            metadata = {"finish_reason": choice.finish_reason, "content_length": len(raw),
                        "usage": response.usage.model_dump() if response.usage else None, "attempt": attempt}
            (self.dir / f"step_{step:03d}_response_attempt_{attempt}.json").write_text(raw, encoding="utf-8")
            (self.dir / f"step_{step:03d}_response_metadata_{attempt}.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            try:
                if not raw.strip():
                    raise ValueError("empty model content")
                decision = AgentDecision.model_validate_json(raw)
                self._validate_decision(decision, state, allowed_metrics, space)
                screening = (remaining_budget or {}).get("screening_status", {})
                if decision.hypothesis_action == "stop" and not screening.get("minimum_coverage_reached", True):
                    raise ValueError("minimum mechanism-group screening coverage has not been reached")
                if (decision.hypothesis_action not in {"stop", "abandon", "close_hypothesis"}
                        and decision.group_name in set(screening.get("tested_groups", []))
                        and not screening.get("minimum_coverage_reached", True)):
                    raise ValueError("select an untested mechanism group before refining a tested group")
                (self.dir / f"step_{step:03d}_response.json").write_text(raw, encoding="utf-8")
                return decision
            except Exception as error:
                last_error = error
                messages = messages + [{"role": "assistant", "content": raw or "{}"},
                    {"role": "user", "content": (
                        "The prior object failed executable schema validation. Return one complete corrected JSON object. "
                        "All fields named *_ids, alternative_explanations, and resource_guardrails must be JSON arrays even with one item. "
                        "For an executable action, knob_group must contain every coupled knob with proposed_value, numerical_basis, mechanism_role and activation_check; group_name must be first in ranked_candidate_groups. "
                        "Do not repeat analysis outside JSON. Validation error: " + str(error)[:500])}]
        assert last_error is not None
        (self.dir / f"step_{step:03d}_schema_error.txt").write_text(f"{type(last_error).__name__}: {last_error}", encoding="utf-8")
        raise RuntimeError("DeepSeek returned an invalid or non-executable diagnosis") from last_error

    @staticmethod
    def _metric_values(state: dict[str, Any]) -> dict[str, float]:
        measurement = state["measurement"]
        values = {"target_workload_throughput_rps": float(measurement["throughput_rps"])}
        telemetry = measurement.get("telemetry", {})
        values.update({k: float(v) for k, v in telemetry.get("rates_per_second", {}).items() if isinstance(v, (int, float))})
        values.update({k: float(v) for k, v in telemetry.get("per_transaction", {}).items() if isinstance(v, (int, float))})
        for key, item in telemetry.get("gauges", {}).items():
            if isinstance(item, dict) and isinstance(item.get("p95"), (int, float)):
                values[key] = float(item["p95"])
        for key, item in measurement.get("plan_evidence", {}).items():
            if isinstance(item, bool):
                values[f"plan_{key}"] = float(item)
            elif isinstance(item, (int, float)):
                values[f"plan_{key}"] = float(item)
        return values

    @staticmethod
    def _validate_decision(d: AgentDecision, state: dict[str, Any], metrics: list[str], space: FullKnobSpace) -> None:
        if d.hypothesis_action in {"stop", "abandon", "close_hypothesis"}:
            return
        if d.evidence_metric_id not in metrics or d.evidence_metric_id == "target_workload_throughput_rps":
            raise ValueError("evidence must be a real non-target telemetry metric")
        if d.evidence_metric_id in {"xact_commit", "idx_scan", "seq_scan"}:
            raise ValueError("raw per-second work counters are circular evidence; select normalized or plan evidence")
        names = [item.knob for item in d.knob_group]
        if len(names) != len(set(names)):
            raise ValueError("a knob may appear only once in a knob group")
        unknown = sorted(set(names) - set(space.names))
        if unknown:
            raise ValueError(f"knob group contains unknown knobs: {unknown}")
        for item in d.knob_group:
            proposed = space.clamp(item.knob, item.proposed_value)
            if str(proposed).lower() == str(state["config"][item.knob]).lower():
                raise ValueError(f"proposed value for {item.knob} does not change the current configuration")

    def materialize(self, decision: AgentDecision, step: int, direction: str, state_id: str,
                    state: dict[str, Any], space: FullKnobSpace, duration: int, warmup: int,
                    terminals: int, seed: int, tps_threshold: float,
                    active_hypothesis: HypothesisRecord | None = None) -> dict[str, Any]:
        prefix = f"s{step:03d}"
        continues = decision.hypothesis_action in {"continue", "revise"} and active_hypothesis is not None
        hypothesis_id = active_hypothesis.hypothesis_id if continues else f"hypothesis:{prefix}"
        frontier_id, contract_id = f"frontier:{prefix}", f"contract:{prefix}"
        metric_id = decision.evidence_metric_id
        metric_values = self._metric_values(state)
        trial_config = dict(state["config"])
        for item in decision.knob_group:
            trial_config[item.knob] = space.clamp(item.knob, item.proposed_value)
        reconciled_trial = space.reconcile(trial_config)
        declared = {item.knob for item in decision.knob_group}
        effective = {name for name, value in reconciled_trial.items()
                     if str(value).lower() != str(state["config"][name]).lower()}
        hidden = sorted(effective - declared)
        if hidden:
            raise ValueError(f"dependency reconciliation changes undeclared knobs: {hidden}")
        no_ops = sorted(declared - effective)
        if no_ops:
            raise ValueError(f"group items become no-ops after dependency reconciliation: {no_ops}")
        dimensions = {item.name: item for item in space.dimensions}
        context_id, mechanism_id = f"context:{prefix}", f"mechanism:{prefix}"
        evidence_id, intervention_id, target_id = f"evidence:{prefix}", f"intervention:{prefix}", "target:tps"
        edge_context, edge_mechanism = f"edge:{prefix}:context_mechanism", f"edge:{prefix}:mechanism_evidence"
        edge_direct, edge_target = f"edge:{prefix}:intervention_evidence", f"edge:{prefix}:evidence_tps"
        nodes = [
            GraphNode(node_id=context_id, type="workload_context", name=direction, description=f"observed switch {direction}", status="observed"),
            GraphNode(node_id=mechanism_id, type="mechanism", name=decision.mechanism, description=decision.hypothesis,
                      status="hypothesized", metadata={"mechanism_inferred": True}),
            GraphNode(node_id=evidence_id, type="evidence_metric", name=metric_id, description="measured target-workload evidence",
                      status="observed", source_ids=[state_id], metric_id=metric_id, metric_source="PostgreSQL statistics sampler",
                      sampling_window={"seconds": duration}, current_value=metric_values[metric_id], aggregation="rate_or_p95",
                      direction="current", unit="native"),
            GraphNode(node_id=intervention_id, type="intervention", name=decision.group_name,
                      description=decision.coupling_rationale, status="candidate",
                      metadata={"knobs": [item.knob for item in decision.knob_group]}),
            GraphNode(node_id=target_id, type="target_metric", name="target TPS", description="single optimization objective", status="observed"),
        ]
        edges = [
            GraphEdge(edge_id=edge_context, cause_node_id=context_id, effect_node_id=mechanism_id, relation="may trigger", mechanism_text=decision.hypothesis, confidence=decision.confidence),
            GraphEdge(edge_id=edge_mechanism, cause_node_id=mechanism_id, effect_node_id=evidence_id, relation="may produce", mechanism_text=decision.mechanism, confidence=decision.confidence),
            GraphEdge(edge_id=edge_direct, cause_node_id=intervention_id, effect_node_id=evidence_id, relation="counterfactually changes", mechanism_text=decision.knob_to_evidence_reason, confidence=decision.confidence),
            GraphEdge(edge_id=edge_target, cause_node_id=evidence_id, effect_node_id=target_id, relation="limits", mechanism_text=decision.evidence_to_tps_reason, confidence=decision.confidence),
        ]
        frontier = ValidationFrontier(frontier_id=frontier_id, hypothesis_id=hypothesis_id,
                                      selected_node_ids=[intervention_id, evidence_id, target_id],
                                      selected_edge_ids=[edge_direct, edge_target], evidence_metric_ids=[metric_id],
                                      reason_for_selection=decision.knob_to_evidence_reason, contract_id=contract_id)
        initial_state_id = active_hypothesis.initial_state_id if continues else state_id
        revision = active_hypothesis.revision + 1 if continues else 1
        hypothesis = HypothesisRecord(hypothesis_id=hypothesis_id,
                                      parent_hypothesis_id=(active_hypothesis.hypothesis_id if decision.hypothesis_action == "branch" and active_hypothesis else decision.parent_hypothesis_id),
                                      revision=revision, title=decision.mechanism, statement=decision.hypothesis,
                                      status="revising" if decision.hypothesis_action == "revise" else "proposed",
                                      initial_state_id=initial_state_id, current_state_id=state_id, current_frontier=frontier_id,
                                      candidate_edge_ids=(active_hypothesis.candidate_edge_ids if continues else []) + [e.edge_id for e in edges],
                                      supported_edge_ids=list(active_hypothesis.supported_edge_ids) if continues else [],
                                      refuted_edge_ids=list(active_hypothesis.refuted_edge_ids) if continues else [],
                                      contract_ids=list(active_hypothesis.contract_ids) if continues else [],
                                      experiment_ids=list(active_hypothesis.experiment_ids) if continues else [],
                                      accepted_intervention_ids=list(active_hypothesis.accepted_intervention_ids) if continues else [],
                                      revision_reason=decision.result_interpretation if decision.hypothesis_action == "revise" else None)
        control = {"target_workload": state["workload"], "snapshot_id": f"{state['workload']}.dump", "terminals": terminals,
                   "duration": duration, "seed": seed, "warmup_seconds": warmup, "postgresql_version": "14",
                   "resource_scope": "same host and PostgreSQL cluster", "transaction_mix": "BenchBase XML fixed"}
        evidence_prediction = Prediction(metric_id=metric_id, expected_direction=decision.evidence_expected_direction,
                                         minimum_change=decision.evidence_minimum_change_percent, unit="percent",
                                         target_edge_id=edge_direct, comparison_method="relative_percent")
        target_prediction = Prediction(metric_id="target_workload_throughput_rps", expected_direction="increase",
                                       minimum_change=tps_threshold, unit="percent", target_edge_id=edge_target,
                                       comparison_method="relative_percent")
        actions = tuple(KnobAction(
            knob=item.knob, current_value=state["config"][item.knob], proposed_value=reconciled_trial[item.knob],
            evidence_ids=(evidence_id,), knowledge_ids=(f"postgres14:{item.knob}",),
            mechanism=decision.knob_to_evidence_reason, mechanism_role=item.mechanism_role,
            numerical_basis=item.numerical_basis,
            restart_required=dimensions[item.knob].execution_class == "restart",
            activation_check=item.activation_check,
            resource_guardrails=tuple(decision.resource_guardrails),
        ) for item in decision.knob_group)
        group_type = "coupled" if len(actions) > 1 else "independent"
        contract = CounterfactualContract(contract_id=contract_id, hypothesis_id=hypothesis_id, frontier_id=frontier_id,
            baseline_state_id=state_id, target_edge_ids=tuple(frontier.edge_ids),
            intervention_group=InterventionGroup(group_id=f"group:{prefix}", group_type=group_type,
                knob_actions=actions,
                dependency_knowledge_ids=((f"diagnosis-group:{decision.group_name}",) if group_type == "coupled" else ())),
            baseline_config_hash=state["config_hash"], control_variables=control, direct_predictions=(evidence_prediction,),
            intermediate_predictions=(), target_prediction=target_prediction,
            thresholds={"evidence_percent": decision.evidence_minimum_change_percent, "tps_percent": tps_threshold},
            warmup_seconds=warmup, measurement_seconds=duration, seed=seed, risk=decision.risk,
            restart_required=any(action.restart_required for action in actions), rollback_plan="restore incumbent configuration and target snapshot",
            abort_conditions=("database unhealthy", "benchmark failure", "PostgreSQL apply failure"),
            why_exceeds_incumbent=decision.why_exceeds_incumbent,
            mechanism_measurement=decision.mechanism_measurement,
            alternative_explanations=tuple(decision.alternative_explanations))
        return {"hypothesis": hypothesis, "frontier": frontier, "nodes": nodes, "edges": edges, "contract": contract}
