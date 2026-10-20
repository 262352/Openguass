from __future__ import annotations

import json
import signal
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

from app.multi_agent.executor import ExperimentExecutor, stable_hash
from app.multi_agent.coordinator import Coordinator
from app.multi_agent.experts import DeepSeekExpertBackend, ExpertPanel
from app.multi_agent.graph import GraphStore
from app.multi_agent.models import IncumbentSnapshot, ValidationOutcome, utc_now
from app.multi_agent.reporting import ReportBuilder
from app.multi_agent.runner import MultiAgentRunner, _append
from app.multi_agent.validator import EvidenceValidator
from app.phase2.bayesian_baseline import load_source_config

from .baseline import build_default_baseline
from .diagnosis_control import ActionConsistencyAuditor, HypothesisStateTable
from .knowledge_retrieval import retrieve_diagnostic_bundles
from .search_space import BoundedSearchPlanner, SearchSpaceCompiler


class TwitterContinuousRunner:
    """Single-workload, default-baseline, exact expert-action diagnosis."""

    def __init__(self, root: Path, experiment_id: str, *, max_rounds: int = 12,
                 max_measurements: int = 60, max_wall_seconds: int = 7200,
                 max_model_calls: int = 36, duration: int = 120,
                 warmup_seconds: int = 30, terminals: int = 8,
                 seed: int = 20261015, baseline_repeats: int = 3,
                 baseline_stability_retries: int = 1,
                 drift_percent: float = 25.0, minimum_tps_gain_percent: float = 5.0,
                 workload: str = "twitter", search_rounds: int = 12,
                 baseline_config: Path | None = None):
        if workload not in {"tpcc", "twitter"}:
            raise ValueError(f"unsupported continuous workload: {workload}")
        self.root, self.experiment_id = root, experiment_id
        self.workload = workload
        self.directory = root / "artifacts" / f"{workload}_continuous" / experiment_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.max_rounds, self.max_measurements = max_rounds, max_measurements
        self.search_rounds = search_rounds
        self.max_wall_seconds, self.max_model_calls = max_wall_seconds, max_model_calls
        self.baseline_repeats = baseline_repeats
        self.baseline_config = baseline_config
        self.baseline_stability_retries = max(0, baseline_stability_retries)
        self.executor = ExperimentExecutor(self.directory, workload, duration=duration,
            warmup_seconds=warmup_seconds, terminals=terminals, seed=seed)
        self.validator = EvidenceValidator(drift_percent=drift_percent,
            repeat_spread_percent=drift_percent,
            minimum_tps_gain_percent=minimum_tps_gain_percent)
        self.reporter = ReportBuilder(self.directory)
        self.ledger = self.directory / "experiment_ledger.jsonl"
        self.panel: ExpertPanel | None = None
        self.model_calls = 0
        self.initial_tps: float | None = None
        self.stop_requested = False

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        original, hardware = self.executor.capture()
        catalog = self.executor.executor.capture_catalog()
        baseline_plan = build_default_baseline(catalog, self.executor.space, original, self.workload)
        if self.baseline_config is not None:
            common_config = load_source_config(self.baseline_config, self.executor.space)
            baseline_plan = type(baseline_plan)(
                config=common_config,
                classifications={name: "common_bo_diagnosis_baseline" for name in self.executor.space.names},
                exceptions={"source": {
                    "path": str(self.baseline_config),
                    "reason": "the BO and diagnosis tracks must start from the identical canonical config",
                }},
                tunable_knobs=tuple(self.executor.space.names),
            )
        plan_record = {
            "schema_version": "andromeda.single-workload-default-baseline/v1",
            "captured_at": utc_now(), "original_effective_config": original,
            "baseline_requested_config": baseline_plan.config,
            "classifications": baseline_plan.classifications,
            "exceptions": baseline_plan.exceptions,
            "tunable_knobs": list(baseline_plan.tunable_knobs),
            "catalog_knob_count": len(self.executor.space),
            "catalog_knob_names": self.executor.space.names,
            "external_infrastructure_exceptions": {
                "track_io_timing/track_wal_io_timing": "fixed diagnostic instrumentation; restored with postgresql.auto.conf",
                "shared_preload_libraries/pg_stat_statements": "fixed observation extension outside the catalog action space",
                "connection/data_directory/port/role": "fixed experiment infrastructure outside the action space",
            },
            "restore": "system/postgresql.auto.conf.before plus final_rollback.json",
        }
        self._write("default_baseline_plan.json", plan_record)
        baseline_hash = stable_hash(baseline_plan.config)
        context = self.executor.context(baseline_hash, hardware)
        manifest = {
            "schema_version": "andromeda.single-workload-continuous/v1",
            "experiment_id": self.experiment_id, "workload": self.workload,
            "created_at": utc_now(), "context": context,
            "objective": f"effective {self.workload} throughput_rps",
            "online_inputs": ["current episode measurements", "versioned evidence graph"],
            "forbidden_online_inputs": ["Bayesian/GP result", f"historical {self.workload} best TPS",
                                        "other-workload configuration"],
            "algorithm": ("graph-frontier expert diagnosis" if self.search_rounds == 0 else
                          "graph-frontier expert diagnosis followed by diagnosis-bounded parameter search"),
            "action_mode": {"parameters": True, "ddl": False, "sql_rewrite": False, "index_build": False},
            "budgets": {"diagnosis_rounds": self.max_rounds, "search_rounds": self.search_rounds,
                        "workload_measurements": self.max_measurements,
                        "wall_seconds": self.max_wall_seconds, "model_calls": self.max_model_calls,
                        "measurements_per_candidate": 2 * getattr(self.executor, "paired_blocks", 2),
                        "baseline_stability_retries": self.baseline_stability_retries},
            "baseline_initialization": {
                "source_config": str(self.baseline_config) if self.baseline_config else "pg_settings.boot_val",
                "maximum_attempts": self.baseline_stability_retries + 1,
                "repeats_per_attempt": self.baseline_repeats,
                "unstable_fallback": "median throughput of the final attempt",
            },
            "acceptance_rule": (
                "randomized paired blocks; regime-matched block-bootstrap 90% CI; accept only when its lower "
                "bound exceeds the minimum worthwhile TPS gain; latency is diagnostic only"),
            "evidence_refresh": (
                "after every intervention, all untested sibling hypotheses are superseded and all experts "
                "must observe the result before proposing the next graph frontier"),
            "mechanism_action_policy": (
                "diagnosed phenomena are joined with the versioned literature relation database and the live "
                "PostgreSQL knob catalog; every actionable knob in the selected mechanism is tested jointly"),
            "non_improvement_policy": (
                "equivalent TPS is inconclusive and triggers a larger same-direction step; only a materially "
                "harmful paired confidence interval refutes the direction and triggers reversal"),
        }
        self._write("manifest.json", manifest)
        self._write("pre_experiment_config.json", {
            "captured_at": utc_now(), "effective_config": original,
            "config_hash": stable_hash(original),
            "rollback_source": "system/postgresql.auto.conf.before",
        })
        graph = GraphStore(self.directory, baseline_hash)
        coordinator = Coordinator(
            self.executor.space,
            required_measurements=2 * getattr(self.executor, "paired_blocks", 2))
        hypotheses = HypothesisStateTable(self.directory)
        action_auditor = ActionConsistencyAuditor(self.directory, self.executor.space)
        diagnosis_history: list[dict[str, Any]] = []
        consecutive_no_new_actions = 0
        unexplained = False
        stop_reason = "budget_exhausted"
        completed_rounds = 0
        completed_search_rounds = 0
        incumbent: IncumbentSnapshot | None = None
        latest_experiment_result: dict[str, Any] | None = None
        previous_handlers = {}

        def request_stop(signum, _frame):
            self.stop_requested = True
            self._write("STOP_REQUESTED.json", {
                "requested_at": utc_now(), "signal": signal.Signals(signum).name,
                "policy": "finish current atomic database/measurement operation, then rollback",
            })

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_stop)
        try:
            self._workload_data_preflight()
            baseline_attempts = []
            baselines: list[dict[str, Any]] = []
            baseline_applies: list[dict[str, Any]] = []
            stability: dict[str, Any] = {"stable": False, "reason": "baseline has not run"}
            for attempt in range(1, self.baseline_stability_retries + 2):
                baselines, baseline_applies = [], []
                for index in range(self.baseline_repeats):
                    value, applied = self.executor.measure(
                        baseline_plan.config, f"default_baseline_attempt_{attempt}_{index + 1}")
                    baselines.append(value); baseline_applies.append(applied)
                    if self.stop_requested:
                        raise KeyboardInterrupt("stop requested after atomic baseline measurement")
                stability = self.validator.baseline_stability(baselines)
                attempt_record = {"attempt": attempt, **stability,
                    "action": ("accepted" if stability.get("stable") else
                               "restore_snapshot_quiesce_and_retry" if attempt <= self.baseline_stability_retries
                               else "fallback_to_final_attempt_median")}
                baseline_attempts.append(attempt_record)
                _append(self.ledger, {"event": "baseline_stability_checked", **attempt_record})
                if stability.get("stable"):
                    break
            fallback_used = not bool(stability.get("stable"))
            stability = {**stability, "attempt": len(baseline_attempts),
                         "maximum_retries": self.baseline_stability_retries,
                         "maximum_attempts": self.baseline_stability_retries + 1,
                         "attempts": baseline_attempts,
                         "fallback_used": fallback_used,
                         "initialization_allowed": True,
                         "initialization_score_method": (
                             "median_of_final_three_repeats" if fallback_used
                             else "minimum_of_stable_repeats"),
                         "initialization_tps": (
                             median(float(item["throughput_rps"]) for item in baselines)
                             if fallback_used else
                             min(float(item["throughput_rps"]) for item in baselines)),
                         "fallback_reason": (
                             "two baseline attempts remained outside the drift threshold; "
                             "continue from the median of the final three measurements"
                             if fallback_used else None)}
            self._write("baseline_stability.json", stability)
            self._write("baseline_stability_attempts.json", {
                "schema_version": "andromeda.baseline-stability-attempts/v1",
                "hard_gate": False, "attempts": baseline_attempts,
                "final_stable": bool(stability.get("stable")),
                "fallback_used": fallback_used,
                "fallback_policy": "median throughput of the final three repeats"})
            observed = baseline_applies[-1].get("observed", {}) if baseline_applies else {}
            self._write("default_baseline_effective.json", {
                "requested": baseline_plan.config, "observed_global_settings": observed,
                "session_settings": baseline_applies[-1].get("session_settings", {}) if baseline_applies else {},
                "pending_restart": [name for name, row in observed.items()
                                    if isinstance(row, dict) and row.get("pending_restart") == "t"],
                "stability": stability,
            })
            incumbent = self._incumbent(
                baseline_plan.config, context, baselines, "default_baseline",
                use_median_score=fallback_used)
            self.initial_tps = float(incumbent.verified_performance["throughput_rps"])
            self._write("incumbent.json", incumbent.model_dump(mode="json"))
            try:
                self.panel = ExpertPanel(DeepSeekExpertBackend(self.directory / "llm"))
            except Exception as error:
                _append(self.ledger, {"event": "model_initialization_failed", "error": repr(error),
                                      "effect": "diagnosis cannot propose an executable mechanism action"})
            coordinator_summary = "no graph-frontier selection has run"
            for round_number in range(1, self.max_rounds + 1):
                completed_rounds = round_number
                if self.stop_requested:
                    stop_reason = "signal_requested"; break
                remaining = self.max_measurements - self.executor.measurement_count
                if remaining < 2 * getattr(self.executor, "paired_blocks", 2):
                    stop_reason = "measurement_budget_exhausted"; break
                if time.monotonic() - started >= self.max_wall_seconds:
                    stop_reason = "wall_time_budget_exhausted"; break
                measurement = incumbent.evidence
                anomalies = self._anomalies(measurement, 0)
                knob_experiment_counts = Counter(
                    change["knob"]
                    for record in hypotheses.records.values()
                    if record["status"] in {"testing", "tested_supported", "tested_refuted",
                                            "tested_inconclusive", "underpowered"}
                    for change in record.get("knob_changes", []))
                diagnostic_knowledge = retrieve_diagnostic_bundles(
                    anomalies, measurement, self.executor.space,
                    dict(knob_experiment_counts), incumbent.config)
                self._write(
                    f"diagnostic_knowledge_round_{round_number:03d}.json",
                    diagnostic_knowledge)
                priority_uncovered = [
                    knob
                    for bundle in diagnostic_knowledge["bundles"]
                    for knob in bundle["required_knobs"]
                    if knob_experiment_counts[knob] == 0
                ]
                proposals = []
                # A graph frontier is valid for one intervention only. After a
                # test, sibling proposals are superseded below and every expert
                # receives the new evidence before this next consultation.
                candidates = hypotheses.proposed(incumbent.config_hash)
                consulted_experts = False
                consultation_succeeded = False
                if self.panel is not None and self.model_calls + 3 <= self.max_model_calls:
                    consulted_experts = True
                    payload = {
                        "optimization_mode": "continuous_optimization", "round": round_number,
                        "current_incumbent": MultiAgentRunner._visible_incumbent(incumbent),
                        "current_measurement": MultiAgentRunner._visible_measurement(measurement, include_diagnostics=True),
                        "diagnostic_alerts": MultiAgentRunner._diagnostic_alerts(measurement, incumbent),
                        "allowed_knobs": [item for item in self.executor.space.describe()
                                          if item["name"] in baseline_plan.tunable_knobs],
                        "graph_frontier": MultiAgentRunner._compact_graph(graph.snapshot("pending")),
                        "tested_action_summary": MultiAgentRunner._compact_graph(graph.snapshot("tested")),
                        "rejected_hypothesis_feedback": hypotheses.feedback(),
                        "latest_experiment_result": latest_experiment_result,
                        "diagnostic_knowledge": diagnostic_knowledge,
                        "coverage_state": {
                            "catalog_knob_count": len(self.executor.space),
                            "experiment_count_by_knob": dict(knob_experiment_counts),
                            "priority_uncovered_knobs": list(dict.fromkeys(priority_uncovered)),
                            "instruction": (
                                "Treat priority_uncovered_knobs as unresolved competing mechanisms. "
                                "An in-scope expert must propose an executable test or return observation-only "
                                "and state the exact missing scalar evidence; listing it only as an alternative "
                                "does not satisfy coverage."),
                        },
                        "evidence_refresh_policy": {
                            "shared_with_all_experts": True,
                            "instruction": (
                                "revise or withdraw earlier advice after observing this result; "
                                "check overlap, interaction, thresholds and alternative knob values"),
                            "stale_sibling_actions_are_executable": False,
                        },
                        "hypothesis_status_counts": dict(Counter(
                            item["status"] for item in hypotheses.records.values())),
                        "policy": "parameter actions only; SQL/index opportunities are recorded but never executed",
                        "remaining_budget": {"rounds": self.max_rounds - round_number + 1,
                                             "workload_measurements": remaining,
                                             "model_calls": self.max_model_calls - self.model_calls},
                    }
                    self.model_calls += 3
                    try:
                        proposals = self.panel.propose_parallel(payload)
                        for failure in getattr(self.panel, "last_proposal_failures", []):
                            _append(self.directory / "expert_failure_log.jsonl", failure)
                            _append(self.ledger, failure)
                        consultation_succeeded = True
                        graph.record_hypothesis_pool(proposals)
                    except Exception as error:
                        for failure in getattr(self.panel, "last_proposal_failures", []):
                            _append(self.directory / "expert_failure_log.jsonl", failure)
                            _append(self.ledger, failure)
                        _append(self.ledger, {"event": "expert_panel_failed", "round": round_number,
                            "error_type": type(error).__name__, "error": str(error)[:2000],
                            "effect": "this round has no expert action"})
                if consulted_experts and consultation_succeeded:
                    added = hypotheses.register_round(round_number, proposals, incumbent.config_hash)
                    graph_proposals = [p for p in proposals
                                       if hypotheses.records[p.proposal_id]["status"] == "proposed"]
                    if graph_proposals:
                        graph.add_proposals(graph_proposals)
                    consecutive_no_new_actions = 0 if added else consecutive_no_new_actions + 1
                    candidates = hypotheses.proposed(incumbent.config_hash)
                executable = []
                for proposal in candidates:
                    check = coordinator.select([proposal], [], current_config=incumbent.config,
                                               remaining_measurements=remaining)
                    if check.selected_proposal_id:
                        executable.append(proposal)
                    else:
                        reason = check.rejected[0]["reason"] if check.rejected else check.reason
                        status = "rejected_unsafe" if any(word in reason for word in
                            ("illegal value", "outside safe range", "unknown knob")) else "rejected_infeasible"
                        hypotheses.transition(proposal.proposal_id, status, reason)
                        graph.reject_proposal(proposal, status, reason)
                candidates = executable
                selection = coordinator.select(candidates, [], current_config=incumbent.config,
                    remaining_measurements=remaining,
                    knob_experiment_counts=knob_experiment_counts)
                coordinator_summary = selection.reason
                chosen = next((item for item in candidates
                               if item.proposal_id == selection.selected_proposal_id), None)
                if chosen is None:
                    _append(self.ledger, {"event": "no_new_executable_mechanism", "round": round_number,
                        "consecutive_rounds_without_new_proposed": consecutive_no_new_actions,
                        "selection": selection.model_dump(mode="json"),
                        "hypothesis_status": list(hypotheses.records.values())})
                    if hypotheses.can_exhaust(consecutive_no_new_actions):
                        stop_reason = "diagnosis_exhausted"
                        break
                    continue
                hypotheses.transition(chosen.proposal_id, "testing", selection.reason)
                candidate, consistency = action_auditor.freeze(round_number, chosen, incumbent.config)
                if candidate is None:
                    status = "rejected_unsafe" if any("safe catalog" in item for item in consistency["reasons"]) \
                        else "rejected_infeasible"
                    reason = "; ".join(consistency["reasons"])
                    hypotheses.transition(chosen.proposal_id, status, reason)
                    graph.reject_proposal(chosen, status, reason)
                    latest_experiment_result = {
                        "proposal_id": chosen.proposal_id,
                        "action": {item.knob: item.new_value for item in chosen.knob_changes},
                        "validity": status, "reason": reason,
                    }
                    # A schema/safety rejection produced no workload evidence;
                    # keep sibling hypotheses executable instead of discarding
                    # an entire expert consultation.
                    superseded: list[str] = []
                    _append(self.ledger, {"event": "action_rejected", "round": round_number,
                        "proposal_id": chosen.proposal_id, "status": status,
                        "action_consistency": consistency,
                        "superseded_sibling_proposal_ids": superseded})
                    continue
                group = f"expert:{chosen.expert}"
                reason = selection.reason
                stage = "mechanism_diagnosis"
                decision = {"event": "candidate_frozen", "round": round_number, "stage": stage,
                    "anomalies": anomalies, "analysis_priority": self._analysis_priority(anomalies),
                    "diagnostic_knowledge": diagnostic_knowledge,
                    "scheduler_reason": reason, "group": group,
                    "expert_proposals": [p.model_dump(mode="json") for p in proposals],
                    "selected": chosen.model_dump(mode="json"),
                    "action_consistency": consistency,
                    "candidate_config_hash": stable_hash(candidate),
                    "action_boundaries": {"ddl_executed": False, "sql_rewrite_executed": False,
                                          "index_build_executed": False}}
                self._write(f"round_{round_number:03d}_decision.json", decision)
                _append(self.ledger, decision)
                self._record_sql_opportunities(round_number, measurement, proposals)
                experiment_id = f"experiment:{round_number:03d}:{uuid.uuid4().hex[:8]}"
                try:
                    paired = self.executor.run_paired(incumbent.config, candidate, round_number)
                except Exception as error:
                    invalid = self._invalid(experiment_id, chosen, error)
                    graph.apply_validation(chosen, invalid)
                    hypotheses.finish_test(chosen, invalid)
                    latest_experiment_result = {
                        "experiment_id": experiment_id, "proposal_id": chosen.proposal_id,
                        "action": {item.knob: item.new_value for item in chosen.knob_changes},
                        "validity": "invalid", "error_type": type(error).__name__,
                        "error": str(error)[:1000],
                    }
                    # A failed execution has no comparable treatment evidence.
                    # Other admitted proposals remain on the frontier.
                    superseded: list[str] = []
                    diagnosis_history.append({"proposal": chosen, "outcome": invalid})
                    self.reporter.append_round(round_number, chosen.proposal_id, [], [], invalid)
                    _append(self.ledger, {"event": "candidate_failed", "round": round_number,
                        "error_type": type(error).__name__, "error": str(error)[:2000],
                        "recovery": "reapply incumbent"})
                    try:
                        self.executor.executor.apply(incumbent.config, self.executor.space)
                    except Exception as recovery_error:
                        stop_reason = "database_recovery_failed"
                        _append(self.ledger, {"event": stop_reason, "error": repr(recovery_error)})
                        break
                    continue
                requested = {knob: value for knob, value in candidate.items()
                             if str(value).lower() != str(incumbent.config.get(knob)).lower()}
                applied_consistency = action_auditor.applied(round_number, chosen, requested,
                                                             paired["observed_candidate_settings"])
                applied_fidelity_violation = applied_consistency["verdict"] == "action_substitution"
                outcome = self.validator.validate(experiment_id, chosen,
                    incumbent_tps=float(incumbent.verified_performance["throughput_rps"]),
                    controls=paired["controls"], treatments=paired["treatments"],
                    requested_settings=requested, observed_settings=paired["observed_candidate_settings"],
                    expected_context=context,
                    actual_contexts=[m.get("experiment_context", context)
                                     for m in paired["controls"] + paired["treatments"]])
                graph.apply_validation(chosen, outcome)
                hypotheses.finish_test(chosen, outcome)
                diagnosis_history.append({"proposal": chosen, "outcome": outcome})
                self.reporter.append_round(round_number, chosen.proposal_id,
                                           paired["controls"], paired["treatments"], outcome)
                _append(self.ledger, {"event": "experiment_completed", "round": round_number,
                    "experiment_id": experiment_id, "stage": stage, "group": group,
                    "frozen_action": requested, "order": paired["order"],
                    "action_consistency": applied_consistency,
                    "raw_measurements": {"controls": paired["controls"], "treatments": paired["treatments"]},
                    "apply_results": paired["applies"], "performance_verdict": outcome.performance_verdict,
                    "mechanism_verdict": outcome.mechanism_verdict,
                    "outcome": outcome.model_dump(mode="json")})
                latest_experiment_result = self._shared_experiment_result(
                    chosen, requested, paired, outcome)
                evidence_epoch = hypotheses.advance_epoch(experiment_id)
                superseded = hypotheses.supersede_pending(
                    tested_proposal_id=chosen.proposal_id,
                    experiment_result=latest_experiment_result)
                for proposal_id in superseded:
                    graph.reject_proposal(hypotheses.proposals[proposal_id],
                        "parked",
                        f"new evidence from {chosen.proposal_id} requires expert revision")
                _append(self.ledger, {"event": "evidence_broadcast_to_all_experts",
                    "round": round_number, "tested_proposal_id": chosen.proposal_id,
                    "evidence_epoch": evidence_epoch,
                    "superseded_sibling_proposal_ids": superseded,
                    "latest_experiment_result": latest_experiment_result})
                if outcome.accepted_as_incumbent:
                    unexplained |= outcome.unexplained_performance
                    old_hash = incumbent.config_hash
                    incumbent = IncumbentSnapshot(config=candidate, config_hash=stable_hash(candidate),
                        verified_performance=outcome.verified_performance or {},
                        guardrails={"validity": outcome.validity},
                        workload_fingerprint=context["workload_fingerprint"],
                        workload_fingerprint_hash=stable_hash(context["workload_fingerprint"]),
                        database_state=context, evidence=paired["treatments"][-1],
                        accepted_experiment_id=experiment_id)
                    self._write("incumbent.json", incumbent.model_dump(mode="json"))
                    graph.mark_stale(incumbent.config_hash, f"incumbent changed from {old_hash}")
                else:
                    self.executor.executor.apply(incumbent.config, self.executor.space)
                if applied_fidelity_violation:
                    stop_reason = "action_fidelity_violation"
                    _append(self.ledger, {"event": stop_reason, "round": round_number,
                        "proposal_id": chosen.proposal_id,
                        "action_consistency": applied_consistency})
                    break

            # Resolve proposals tied to an older incumbent before compiling the
            # bounded search space; they must never silently survive as active.
            hypotheses.proposed(incumbent.config_hash)
            exhaustion = hypotheses.write_exhaustion_report(stop_reason=stop_reason,
                consecutive_no_new=consecutive_no_new_actions,
                coordinator_summary=coordinator_summary)
            frozen_space = SearchSpaceCompiler(self.directory, self.executor.space).compile(
                hypotheses, diagnosis_history, incumbent.config)
            planner = BoundedSearchPlanner(frozen_space, self.executor.space)
            search_stop_reason = "search_space_empty" if not planner.plans else "search_round_budget_exhausted"
            search_blocked = stop_reason in {"action_fidelity_violation", "database_recovery_failed", "signal_requested"}
            if search_blocked:
                search_stop_reason = f"search_not_started_after_{stop_reason}"
            for search_index in range(1, 0 if search_blocked else self.search_rounds + 1):
                if self.stop_requested:
                    search_stop_reason = "signal_requested"; break
                remaining = self.max_measurements - self.executor.measurement_count
                if remaining < 4:
                    search_stop_reason = "measurement_budget_exhausted"; break
                if time.monotonic() - started >= self.max_wall_seconds:
                    search_stop_reason = "wall_time_budget_exhausted"; break
                item = planner.candidate(incumbent.config,
                    round_number=self.max_rounds + search_index,
                    config_hash=incumbent.config_hash,
                    workload_hash=incumbent.workload_fingerprint_hash,
                    duration=self.executor.duration, workload=self.workload)
                if item is None:
                    search_stop_reason = "frozen_search_space_exhausted"; break
                candidate, proposal = item
                completed_search_rounds = search_index
                graph.add_proposals([proposal])
                experiment_id = f"search:{search_index:03d}:{uuid.uuid4().hex[:8]}"
                try:
                    paired = self.executor.run_paired(incumbent.config, candidate,
                                                      self.max_rounds + search_index)
                except Exception as error:
                    invalid = self._invalid(experiment_id, proposal, error)
                    graph.apply_validation(proposal, invalid)
                    self.reporter.append_round(self.max_rounds + search_index,
                                               proposal.proposal_id, [], [], invalid)
                    _append(self.ledger, {"event": "bounded_search_failed",
                        "search_round": search_index, "experiment_id": experiment_id,
                        "error_type": type(error).__name__, "error": str(error)[:2000]})
                    self.executor.executor.apply(incumbent.config, self.executor.space)
                    continue
                requested = {knob: value for knob, value in candidate.items()
                             if str(value).lower() != str(incumbent.config.get(knob)).lower()}
                outcome = self.validator.validate(experiment_id, proposal,
                    incumbent_tps=float(incumbent.verified_performance["throughput_rps"]),
                    controls=paired["controls"], treatments=paired["treatments"],
                    requested_settings=requested, observed_settings=paired["observed_candidate_settings"],
                    expected_context=context,
                    actual_contexts=[m.get("experiment_context", context)
                                     for m in paired["controls"] + paired["treatments"]])
                graph.apply_validation(proposal, outcome)
                self.reporter.append_round(self.max_rounds + search_index, proposal.proposal_id,
                                           paired["controls"], paired["treatments"], outcome)
                _append(self.ledger, {"event": "bounded_search_completed", "search_round": search_index,
                    "stage": "diagnosis_bounded_search", "experiment_id": experiment_id,
                    "frozen_action": requested, "outcome": outcome.model_dump(mode="json")})
                if outcome.accepted_as_incumbent:
                    unexplained |= outcome.unexplained_performance
                    old_hash = incumbent.config_hash
                    incumbent = IncumbentSnapshot(config=candidate, config_hash=stable_hash(candidate),
                        verified_performance=outcome.verified_performance or {},
                        guardrails={"validity": outcome.validity},
                        workload_fingerprint=context["workload_fingerprint"],
                        workload_fingerprint_hash=stable_hash(context["workload_fingerprint"]),
                        database_state=context, evidence=paired["treatments"][-1],
                        accepted_experiment_id=experiment_id)
                    self._write("incumbent.json", incumbent.model_dump(mode="json"))
                    graph.mark_stale(incumbent.config_hash, f"search incumbent changed from {old_hash}")
                else:
                    self.executor.executor.apply(incumbent.config, self.executor.space)
            stop_reason = search_stop_reason
            report = self.reporter.build(incumbent, graph, {
                "stop_reason": stop_reason, "completed_rounds": completed_rounds,
                "completed_diagnosis_rounds": completed_rounds,
                "completed_search_rounds": completed_search_rounds,
                "workload_measurements": self.executor.measurement_count,
                "episode_initial_tps": self.initial_tps,
                "model_calls_scheduled": self.model_calls,
                "unexplained_performance": unexplained,
                "baseline_stable": stability.get("stable"),
                "baseline_fallback_used": fallback_used,
                "baseline_initialization_score_method": stability["initialization_score_method"],
                "diagnosis_exhaustion": exhaustion,
                "frozen_search_parameter_count": len(frozen_space["parameters"]),
                "claim": "budgeted experiment result; no global-optimum or convergence claim",
            })
            return report
        finally:
            try:
                rollback = self.executor.restore()
            except Exception as error:
                rollback = {"restored": False, "error_type": type(error).__name__, "error": str(error)[:2000]}
            self._write("final_rollback.json", rollback)
            self.reporter.add_operational_cost({
                "workload_measurements": self.executor.measurement_count,
                "warmup_runs": self.executor.warmup_count,
                "postgresql_restarts": self.executor.restart_count,
                "configuration_applications": self.executor.apply_count,
                "model_calls": self.model_calls, "model_tokens": self._model_tokens(),
                "elapsed_seconds": round(time.monotonic() - started, 3), "final_rollback": rollback,
            })
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

    @staticmethod
    def _anomalies(measurement: dict[str, Any], low_gain: int) -> list[str]:
        values = list(measurement.get("telemetry", {}).get("anomalies", []))
        client = measurement.get("client", {})
        if client.get("bottleneck_summary", {}).get("detected"):
            values.append("transaction_terminal_saturation")
        query = measurement.get("query_evidence", {})
        if query.get("status") == "available" and 0 < query.get("template_count", 0) <= 5:
            values.append("few_dominant_query_templates")
        if low_gain >= 2:
            values.append("tps_stagnant_deepen_query_or_cross_group")
        return list(dict.fromkeys(values))

    @staticmethod
    def _analysis_priority(anomalies: list[str]) -> str:
        resource = {"wal_sync_latency", "wal_wait", "host_io_pressure", "lock_contention",
                    "temp_spill", "low_buffer_hit_ratio", "checkpoint_write_pressure"}
        if resource & set(anomalies):
            return "resource_or_wait_path_plus_contributing_sql"
        if "transaction_terminal_saturation" in anomalies:
            return "transaction_and_query_critical_path"
        if "few_dominant_query_templates" in anomalies:
            return "bounded_representative_plan_review"
        if "tps_stagnant_deepen_query_or_cross_group" in anomalies:
            return "deeper_query_evidence_or_unexplored_parameter_group"
        return "parallel_lightweight_evidence_plus_cross_group_exploration"

    def _record_sql_opportunities(self, round_number: int, measurement: dict[str, Any], proposals: list[Any]) -> None:
        query = measurement.get("query_evidence", {})
        record = {"round": round_number, "execution_enabled": False,
            "actions_executed": [], "query_evidence_status": query.get("status"),
            "template_candidates": query.get("leaderboards", {}),
            "expert_non_parameter_notes": [item for proposal in proposals
                for item in proposal.cross_domain_requirements
                if any(word in item.lower() for word in ("sql", "index", "application", "transaction"))],
            "required_followup": "separate explicit SQL/index experiment with build and write/WAL cost controls"}
        _append(self.directory / "sql_opportunities.jsonl", record)

    @staticmethod
    def _shared_experiment_result(proposal: Any, requested: dict[str, Any],
                                  paired: dict[str, Any],
                                  outcome: ValidationOutcome) -> dict[str, Any]:
        """Compact result broadcast to every expert before the next proposal."""
        def view(item: dict[str, Any]) -> dict[str, Any]:
            client = item.get("client", {})
            return {
                "throughput_rps": item.get("throughput_rps"),
                "p95_us": item.get("p95_us"), "p99_us": item.get("p99_us"),
                "transaction_latency": client.get("transaction_latency", {}),
                "successful_transactions": client.get("successful_transactions"),
                "transaction_attempts": client.get("transaction_attempts"),
                "server_retry_transactions": client.get("server_retry_transactions"),
                "server_retry_ratio": client.get("server_retry_ratio"),
                "final_failures": client.get("final_failures"),
                "telemetry": item.get("telemetry", {}),
                "query_evidence": item.get("query_evidence", {}),
            }
        paired_decision = (outcome.verified_performance or {}).get("paired_decision", {})
        verdict = paired_decision.get("verdict")
        if outcome.validity != "valid":
            adaptation = "repeat_after_environment_recovers"
        elif verdict == "accept":
            adaptation = "accepted_update_incumbent"
        elif verdict == "reject_harmful":
            adaptation = "reverse_direction_with_equal_or_larger_amplitude"
        elif verdict == "reject_equivalent":
            adaptation = "increase_amplitude_in_same_direction"
        else:
            adaptation = "collect_more_evidence_before_changing_direction"
        old_values = {item.knob: item.old_value for item in proposal.knob_changes}
        return {
            "experiment_id": outcome.experiment_id,
            "tested_proposal_id": proposal.proposal_id,
            "tested_expert": proposal.expert,
            "tested_action": requested,
            "tested_changes": {name: {"old_value": old_values.get(name), "new_value": value}
                               for name, value in requested.items()},
            "controls": [view(item) for item in paired["controls"]],
            "treatments": [view(item) for item in paired["treatments"]],
            "validity": outcome.validity,
            "validity_reasons": outcome.validity_reasons,
            "performance_verdict": outcome.performance_verdict,
            "mechanism_verdict": outcome.mechanism_verdict,
            "accepted_as_incumbent": outcome.accepted_as_incumbent,
            "verified_performance": outcome.verified_performance,
            "edge_results": outcome.edge_results,
            "adaptation_policy": adaptation,
            "required_expert_response": (
                "reassess overlap, interactions, thresholds and knob values before submitting a revised action"),
        }

    def _incumbent(self, config: dict[str, Any], context: dict[str, Any],
                   measurements: list[dict[str, Any]], experiment_id: str,
                   *, use_median_score: bool = False) -> IncumbentSnapshot:
        values = [float(item["throughput_rps"]) for item in measurements]
        score = median(values) if use_median_score else min(values)
        return IncumbentSnapshot(config=config, config_hash=stable_hash(config),
            verified_performance={"throughput_rps": score, "median_throughput_rps": median(values),
                                  "replicate_throughput_rps": values,
                                  "score_method": ("median_of_final_three_unstable_baseline_fallback"
                                                   if use_median_score
                                                   else "minimum_of_stable_repeats")},
            guardrails={"healthy": all(item.get("healthy", False) for item in measurements)},
            workload_fingerprint=context["workload_fingerprint"],
            workload_fingerprint_hash=stable_hash(context["workload_fingerprint"]),
            database_state=context, evidence=measurements[-1], accepted_experiment_id=experiment_id)

    @staticmethod
    def _invalid(experiment_id: str, proposal: Any, error: Exception) -> ValidationOutcome:
        return ValidationOutcome(experiment_id=experiment_id, validity="invalid",
            validity_reasons=[f"executor failure: {type(error).__name__}: {str(error)[:500]}"],
            performance_verdict="invalid", mechanism_verdict="inconclusive",
            accepted_as_incumbent=False, unexplained_performance=False,
            edge_results={edge.edge_id: {"state": "inconclusive", "reason": "executor failure", "observations": []}
                          for edge in proposal.edges}, actual_settings_match=False)

    def _model_tokens(self) -> int:
        total = 0
        for path in (self.directory / "llm").glob("*_metadata.json"):
            try: total += int(json.loads(path.read_text()).get("usage", {}).get("total_tokens", 0) or 0)
            except (OSError, ValueError, TypeError): pass
        return total

    def _workload_data_preflight(self) -> None:
        xml_path = self.root / "benchmarks" / "postgres" / f"{self.workload}.xml"
        scale = float(ET.parse(xml_path).getroot().findtext("scalefactor", "0"))
        if self.workload == "twitter":
            expected = int(round(500 * scale))
            actual = int(self.executor.executor._psql("select count(*) from user_profiles"))
            object_name = "user_profiles"
            basis = "BenchBase TwitterConstants.NUM_USERS=500 multiplied by XML scale factor"
        else:
            expected = int(round(scale))
            actual = int(self.executor.executor._psql("select count(*) from warehouse"))
            object_name = "warehouses"
            basis = "TPC-C scale factor equals warehouse count"
        result = {"workload": self.workload, "scale_factor": scale,
                  "expected_objects": expected, "actual_objects": actual, "object": object_name,
                  "compatible": actual == expected, "basis": basis}
        self._write("workload_data_preflight.json", result)
        if not result["compatible"]:
            raise RuntimeError(
                f"{self.workload} snapshot/data mismatch: scale factor {scale:g} requires {expected} "
                f"{object_name}, found {actual}; rebuild snapshots with bootstrap --force-reload"
            )

    def _write(self, name: str, value: Any) -> None:
        (self.directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
