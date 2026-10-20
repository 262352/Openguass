from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from statistics import median
from typing import Any

from .coordinator import Coordinator
from .executor import ExperimentExecutor, stable_hash
from .experts import DeepSeekExpertBackend, ExpertPanel
from .graph import GraphStore
from .graph_protocol import AgentGraph, EvidenceBlackboard, GraphPatch, PatchOperation
from .models import IncumbentSnapshot, utc_now
from .reporting import ReportBuilder
from .validator import EvidenceValidator
from app.experiment.isolation import assert_diagnosis_context_isolated


def _append(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n")


class MultiAgentRunner:
    def __init__(self, root: Path, experiment_id: str, source: str, target: str, *,
                 max_rounds: int = 12, max_measurements: int = 60, duration: int = 30,
                 warmup_seconds: int = 30, terminals: int = 2, seed: int = 20261012,
                 baseline_repeats: int = 3, drift_percent: float = 25.0,
                 source_config: Path | None = None, source_profile_repeats: int = 0,
                 source_profile_terminals: int | None = None,
                 max_observation_rounds: int = 2, communication_topology: str = "blackboard",
                 expert_roles: tuple[str, ...] = ("query_execution", "memory_storage", "concurrency_maintenance"),
                 output_root: Path | None = None):
        self.root, self.experiment_id, self.source, self.target = root, experiment_id, source, target
        self.directory = (output_root or (root / "artifacts" / "multi_agent" / experiment_id)) / f"{source}_to_{target}"
        self.directory.mkdir(parents=True, exist_ok=False)
        self.max_rounds, self.max_measurements, self.baseline_repeats = max_rounds, max_measurements, baseline_repeats
        self.executor = ExperimentExecutor(self.directory, target, duration=duration, warmup_seconds=warmup_seconds,
                                           terminals=terminals, seed=seed)
        self.validator = EvidenceValidator(drift_percent=drift_percent, repeat_spread_percent=drift_percent)
        self.coordinator = Coordinator()
        self.panel: ExpertPanel | None = None
        self.reporter = ReportBuilder(self.directory)
        self.ledger = self.directory / "experiment_ledger.jsonl"
        self.source_config_path = source_config
        self.source_profile_repeats = source_profile_repeats
        self.source_profile_terminals = source_profile_terminals or terminals
        self.max_observation_rounds = max_observation_rounds
        self.communication_topology = communication_topology
        self.expert_roles = expert_roles
        self.episode_initial_tps: float | None = None
        self.source_profile_measurements = 0

    def run(self) -> dict[str, Any]:
        start_config, hardware = self.executor.capture()
        source_file = self.source_config_path or (self.root / "artifacts" / "source_configs" / f"{self.source}.json")
        if source_file.exists():
            start_config = json.loads(source_file.read_text(encoding="utf-8")).get("config", start_config)
        elif self.source_config_path is not None:
            raise FileNotFoundError(f"source BO configuration does not exist: {source_file}")
        requested_start_config = dict(start_config)
        start_config = self.executor.space.reconcile({
            name: self.executor.space.clamp(name, start_config[name])
            for name in self.executor.space.names
        })
        projection = {name: {"requested": requested_start_config[name], "effective": start_config[name]}
                      for name in self.executor.space.names
                      if str(requested_start_config[name]).lower() != str(start_config[name]).lower()}
        (self.directory / "source_config_projection.json").write_text(json.dumps({
            "source_config": str(source_file), "projected_to_current_safe_space": bool(projection),
            "changes": projection}, ensure_ascii=False, indent=2), encoding="utf-8")
        source_hash = stable_hash(start_config)
        context = self.executor.context(source_hash, hardware)
        context_hash = stable_hash(context)
        manifest = {"schema_version": "andromeda.multi-agent-experiment/v2", "experiment_id": self.experiment_id,
            "source": self.source, "target": self.target, "created_at": utc_now(), "context": context,
            "budgets": {"rounds": self.max_rounds, "workload_measurements": self.max_measurements},
            "agent_protocol": {"topology": self.communication_topology, "roles": list(self.expert_roles),
                               "expert_calls_per_round": len(self.expert_roles), "peer_review_calls": 0},
            "policy": "No GP/EI/Bayesian result for the target workload participates in online decisions."}
        (self.directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        graph = GraphStore(self.directory, source_hash)
        blackboard = EvidenceBlackboard(self.directory, self.experiment_id, self.target)
        agent_graph = AgentGraph(self.directory, self.experiment_id, list(self.expert_roles), self.communication_topology,
                                 max_expert_calls_per_round=len(self.expert_roles))
        refuted: set[tuple[Any, ...]] = set()
        unexplained = False
        stop_reason = "budget_exhausted"
        try:
            source_profile = []
            if self.source_profile_repeats:
                source_executor = ExperimentExecutor(self.directory / "source_profile", self.source,
                    duration=self.executor.duration, warmup_seconds=self.executor.warmup_seconds,
                    terminals=self.source_profile_terminals, seed=self.executor.seed)
                source_executor.capture()
                source_context = source_executor.context(source_hash, hardware)
                for i in range(self.source_profile_repeats):
                    value, _ = source_executor.measure(start_config, f"source_steady_{i+1}")
                    source_profile.append(value)
                    self.source_profile_measurements += 1
                (self.directory / "source_workload_profile.json").write_text(json.dumps({
                    "workload": self.source, "config_hash": source_hash, "context": source_context,
                    "measurements": source_profile}, ensure_ascii=False, indent=2), encoding="utf-8")
            baselines, baseline_applies = [], []
            for i in range(self.baseline_repeats):
                measurement, apply_result = self.executor.measure(start_config, f"baseline_{i+1}")
                baselines.append(measurement); baseline_applies.append(apply_result)
            stability = self.validator.baseline_stability(baselines)
            historical = self._historical_baseline_check(context_hash, stability)
            stability["historical_comparison"] = historical
            if not historical["comparable"]:
                stability["stable"] = False
                stability["reason"] = "same-context historical baseline differs by at least 2x"
            (self.directory / "baseline_stability.json").write_text(json.dumps(stability, indent=2), encoding="utf-8")
            if not stability["stable"]:
                stop_reason = "unstable_baseline"
                incumbent = self._incumbent(start_config, context, baselines, "baseline", {"baseline_stability": stability})
                return self.reporter.build(incumbent, graph, self._summary(stop_reason, 0, unexplained))
            incumbent = self._incumbent(start_config, context, baselines, "baseline", {"baseline_stability": stability})
            self.episode_initial_tps = float(incumbent.verified_performance["throughput_rps"])
            (self.directory / "incumbent.json").write_text(incumbent.model_dump_json(indent=2), encoding="utf-8")
            visible_context = {"schema_version": "andromeda.diagnosis-visible-context/v1",
                "episode_id": self.experiment_id, "source_workload": self.source, "target_workload": self.target,
                "episode_start_config": start_config, "source_profile": source_profile,
                "target_initial_measurements": baselines, "target_initial_incumbent": incumbent.model_dump(mode="json"),
                "online_context_only": True}
            (self.directory / "diagnosis_visible_context.json").write_text(
                json.dumps(visible_context, ensure_ascii=False, indent=2), encoding="utf-8")
            self.panel = ExpertPanel(DeepSeekExpertBackend(self.directory / "llm"), self.expert_roles)
            observation_rounds = 0
            for round_number in range(1, self.max_rounds + 1):
                remaining = self.max_measurements - self.executor.measurement_count
                if remaining < 4:
                    stop_reason = "measurement_budget_exhausted"; break
                visible_incumbent = self._visible_incumbent(incumbent)
                payload = {"round": round_number, "current_incumbent": visible_incumbent,
                    "current_measurement": self._visible_measurement(incumbent.evidence, include_diagnostics=True),
                    "diagnostic_alerts": self._diagnostic_alerts(incumbent.evidence, incumbent),
                    "workload_transition_evidence": {
                        "source_workload": self.source,
                        "source_profile": [self._visible_measurement(item, include_diagnostics=True)
                                           for item in source_profile],
                        "target_workload": self.target,
                        "target_initial_measurements": [self._visible_measurement(item, include_diagnostics=False)
                                                        for item in baselines]},
                    "allowed_knobs": self.executor.space.describe(),
                    "graph_frontier": self._compact_graph(graph.snapshot("pending")),
                    "tested_action_summary": self._compact_graph(graph.snapshot("tested")),
                    "remaining_budget": {"rounds": self.max_rounds-round_number+1, "workload_measurements": remaining}}
                assert_diagnosis_context_isolated(payload)
                try:
                    proposals = self.panel.propose_parallel(payload)
                    for failure in getattr(self.panel, "last_proposal_failures", []):
                        _append(self.directory / "expert_failure_log.jsonl", failure)
                        _append(self.ledger, failure)
                except Exception as error:
                    for failure in getattr(self.panel, "last_proposal_failures", []):
                        _append(self.directory / "expert_failure_log.jsonl", failure)
                        _append(self.ledger, failure)
                    stop_reason = "model_unavailable"
                    _append(self.ledger, {"event": "expert_panel_failed", "round": round_number,
                        "error_type": type(error).__name__, "error": str(error)[:2000]})
                    break
                for proposal in proposals:
                    if proposal.incumbent_config_hash != incumbent.config_hash or proposal.workload_fingerprint_hash != incumbent.workload_fingerprint_hash:
                        raise RuntimeError("expert proposal context does not match current incumbent")
                patches = []
                for proposal in proposals:
                    patch = self._proposal_patch(proposal, round_number, blackboard.version)
                    blackboard.commit(patch); patches.append(patch)
                agent_graph.record_round(round_number,
                    [{"agent": p.expert, "kind": "graph_patch", "patch_id": patch.patch_id}
                     for p, patch in zip(proposals, patches)],
                    [{"from": p.expert, "to": "coordinator", "patch_id": patch.patch_id}
                     for p, patch in zip(proposals, patches)])
                canonical, merged = self.coordinator.merge_exact_duplicates(proposals)
                graph.record_hypothesis_pool(proposals)
                graph.add_proposals(canonical)
                for proposal in canonical:
                    graph.merge_provenance(proposal, merged.get(proposal.proposal_id, []))
                selection = self.coordinator.select(canonical, [], current_config=incumbent.config,
                    refuted_signatures=refuted, remaining_measurements=remaining)
                proposal_record = {"event": "proposal_frozen", "round": round_number,
                    "incumbent": incumbent.model_dump(mode="json"), "proposals": [p.model_dump(mode="json") for p in proposals],
                    "graph_patches": [p.model_dump(mode="json") for p in patches],
                    "cross_reviews": [], "selection": selection.model_dump(mode="json")}
                _append(self.ledger, proposal_record)
                (self.directory / f"round_{round_number:03d}_decision.json").write_text(json.dumps(proposal_record, ensure_ascii=False, indent=2), encoding="utf-8")
                if not selection.selected_proposal_id:
                    if selection.requires_more_observation and observation_rounds < self.max_observation_rounds and remaining >= 1:
                        observation_rounds += 1
                        observed, apply_result = self.executor.measure(incumbent.config, f"round_{round_number:03d}_observation")
                        incumbent.evidence = observed
                        observation_refs = [operation.subject_id for patch in patches for operation in patch.operations
                                            if operation.kind == "add_observation_request"]
                        result_patch = GraphPatch(patch_id=f"round-{round_number:03d}-observation-result",
                            episode_id=self.experiment_id, workload=self.target, author="experiment_validator",
                            base_version=blackboard.version, confidence=1.0,
                            operations=[PatchOperation(operation_id=f"op-round-{round_number:03d}-observation-result",
                                kind="report_result", subject_id=f"observation-result-round-{round_number:03d}",
                                references=observation_refs,
                                payload={"measurement": observed, "apply": apply_result,
                                         "reason": "disambiguate observation-only graph frontier"})])
                        blackboard.commit(result_patch)
                        _append(self.ledger, {"event": "observation_completed", "round": round_number,
                            "measurement": observed, "patch_id": result_patch.patch_id})
                        continue
                    stop_reason = "no_executable_or_discriminating_followup"; break
                chosen = next(p for p in canonical if p.proposal_id == selection.selected_proposal_id)
                candidate = dict(incumbent.config)
                for change in chosen.knob_changes: candidate[change.knob] = self.executor.space.clamp(change.knob, change.new_value)
                candidate = self.executor.space.reconcile(candidate)
                experiment_id = f"experiment:{round_number:03d}:{uuid.uuid4().hex[:8]}"
                try:
                    if chosen.interaction_design:
                        paired = self.executor.run_factorial(incumbent.config, candidate, chosen, round_number)
                    else:
                        paired = self.executor.run_paired(incumbent.config, candidate, round_number)
                except Exception as error:
                    stop_reason = "experiment_executor_failed"
                    invalid = self._invalid_outcome(experiment_id, chosen, error)
                    graph.apply_validation(chosen, invalid)
                    _append(self.ledger, {"event": "experiment_failed", "round": round_number,
                        "experiment_id": experiment_id, "proposal_id": chosen.proposal_id,
                        "frozen_action": {x.knob: candidate[x.knob] for x in chosen.knob_changes},
                        "error_type": type(error).__name__, "error": str(error)[:2000],
                        "outcome": invalid.model_dump(mode="json")})
                    self.reporter.append_round(round_number, chosen.proposal_id, [], [], invalid)
                    break
                requested = {x.knob: candidate[x.knob] for x in chosen.knob_changes}
                outcome = self.validator.validate(experiment_id, chosen, incumbent_tps=float(incumbent.verified_performance["throughput_rps"]),
                    controls=paired["controls"], treatments=paired["treatments"], requested_settings=requested,
                    observed_settings=paired["observed_candidate_settings"], expected_context=context,
                    actual_contexts=[m.get("experiment_context", context) for m in (
                        [x for values in paired.get("factorial_measurements", {}).values() for x in values]
                        or paired["controls"] + paired["treatments"])],
                    factorial_measurements=paired.get("factorial_measurements"))
                graph.apply_validation(chosen, outcome)
                _append(self.ledger, {"event": "experiment_completed", "round": round_number,
                    "experiment_id": experiment_id, "proposal_id": chosen.proposal_id, "frozen_action": requested,
                    "order": paired["order"], "raw_measurements": {"controls": paired["controls"], "treatments": paired["treatments"]},
                    "factorial_measurements": paired.get("factorial_measurements"),
                    "apply_results": paired["applies"], "outcome": outcome.model_dump(mode="json")})
                self.reporter.append_round(round_number, chosen.proposal_id, paired["controls"], paired["treatments"], outcome)
                if outcome.mechanism_verdict == "refuted": refuted.add(chosen.merge_signature())
                if outcome.accepted_as_incumbent:
                    unexplained |= outcome.unexplained_performance
                    old_hash = incumbent.config_hash
                    incumbent = IncumbentSnapshot(config=candidate, config_hash=stable_hash(candidate),
                        verified_performance=outcome.verified_performance or {}, guardrails={"validity": outcome.validity},
                        workload_fingerprint=context["workload_fingerprint"], workload_fingerprint_hash=stable_hash(context["workload_fingerprint"]),
                        database_state=context, evidence=paired["treatments"][-1], accepted_experiment_id=experiment_id)
                    (self.directory / "incumbent.json").write_text(incumbent.model_dump_json(indent=2), encoding="utf-8")
                    # The next round uses a new configuration context. Historical evidence stays but becomes stale.
                    graph.mark_stale(incumbent.config_hash, f"incumbent changed from {old_hash}")
                else:
                    self.executor.executor.apply(incumbent.config, self.executor.space)
            return self.reporter.build(incumbent, graph, self._summary(stop_reason, round_number, unexplained))
        finally:
            try:
                rollback = self.executor.restore()
            except Exception as error:
                rollback = {"restored": False, "error_type": type(error).__name__, "error": str(error)[:2000]}
            (self.directory / "final_rollback.json").write_text(json.dumps(rollback, indent=2), encoding="utf-8")
            self.reporter.add_operational_cost({
                "workload_measurements": self.executor.measurement_count,
                "source_profile_measurements": self.source_profile_measurements,
                "warmup_runs": self.executor.warmup_count,
                "postgresql_restarts": self.executor.restart_count,
                "configuration_applications": self.executor.apply_count,
                **self._model_usage(), "final_rollback": rollback,
            })

    @staticmethod
    def _visible_measurement(measurement: dict[str, Any], *, include_diagnostics: bool) -> dict[str, Any]:
        """Return the audited model view without duplicating bulky run metadata."""
        result = {key: measurement.get(key) for key in (
            "label", "run_id", "throughput_rps", "p95_us", "average_us", "p99_us", "max_us",
            "healthy", "elapsed_seconds"
        ) if key in measurement}
        result["client"] = measurement.get("client", {})
        if include_diagnostics:
            result["telemetry"] = measurement.get("telemetry", {})
            result["query_evidence"] = measurement.get("query_evidence", {})
            result["plan_evidence"] = measurement.get("plan_evidence", {})
            result["database_state"] = measurement.get("database_state", {})
        context = measurement.get("experiment_context", {})
        result["measurement_protocol"] = {
            key: context.get(key) for key in ("duration", "warmup_seconds", "snapshot_settle_seconds", "terminals", "seed")
            if key in context
        }
        return result

    @staticmethod
    def _visible_incumbent(incumbent: IncumbentSnapshot) -> dict[str, Any]:
        """Keep the action context without duplicating all measurement evidence."""
        return {
            "config": incumbent.config,
            "config_hash": incumbent.config_hash,
            "verified_performance": incumbent.verified_performance,
            "guardrails": incumbent.guardrails,
            "workload_fingerprint_hash": incumbent.workload_fingerprint_hash,
            "accepted_experiment_id": incumbent.accepted_experiment_id,
            "evidence_pointer": {"label": incumbent.evidence.get("label"),
                                 "run_id": incumbent.evidence.get("run_id")},
        }

    @staticmethod
    def _compact_graph(snapshot, *, maximum_edges: int = 80) -> dict[str, Any]:
        """Expose graph decisions without recursively growing raw observations."""
        node_labels = {node.node_id: {"kind": node.kind, "label": node.label}
                       for node in snapshot.nodes}
        edges = []
        for edge in snapshot.edges[-maximum_edges:]:
            edges.append({
                "edge_id": edge.edge_id,
                "source": node_labels.get(edge.source, {"kind": "unknown", "label": edge.source}),
                "target": node_labels.get(edge.target, {"kind": "unknown", "label": edge.target}),
                "relation": edge.relation,
                "state": edge.state,
                "proposal_ids": edge.proposal_ids,
                "validation_experiment_ids": edge.validation_experiment_ids,
                "predictions": [{"metric_id": item.metric_id,
                                  "expected_direction": item.expected_direction,
                                  "minimum_change": item.minimum_change,
                                  "comparison_method": item.comparison_method}
                                 for item in edge.frozen_predictions],
            })
        return {"graph_kind": snapshot.graph_kind, "version": snapshot.version,
                "edge_count": len(snapshot.edges), "edges": edges,
                "truncated": len(snapshot.edges) > maximum_edges}

    @staticmethod
    def _diagnostic_alerts(measurement: dict[str, Any], incumbent: IncumbentSnapshot) -> list[dict[str, Any]]:
        """Derive high-signal facts deterministically before asking an expert to explain them."""
        alerts: list[dict[str, Any]] = []
        client = measurement.get("client", {})
        retry_ratio = client.get("server_retry_ratio")
        if isinstance(retry_ratio, (int, float)) and retry_ratio >= 0.05:
            alerts.append({
                "kind": "serialization_retry_storm",
                "severity": "high",
                "evidence": {"server_retry_ratio": retry_ratio,
                             "server_retry_transactions": client.get("server_retry_transactions"),
                             "retryable_database_aborts": client.get("retryable_database_aborts")},
                "interpretation": ("BenchBase internally retried PostgreSQL serialization failures; "
                                   "goodput excludes rejected attempts"),
            })
        bottleneck = client.get("bottleneck_summary", {})
        if bottleneck.get("detected"):
            alerts.append({
                "kind": "client_terminal_saturation",
                "severity": "high",
                "evidence": bottleneck,
                "interpretation": "one transaction class consumes most finite client terminal time and directly limits TPS",
            })
        if bottleneck.get("tail_masked_by_overall_p95"):
            alerts.append({
                "kind": "low_frequency_tail_masked_by_overall_percentile",
                "severity": "high",
                "evidence": {"critical_transaction": bottleneck.get("critical_transaction"),
                             "transaction_p50_us": bottleneck.get("transaction_p50_us"),
                             "overall_p95_us": bottleneck.get("overall_p95_us")},
                "interpretation": "overall p95 is dominated by frequent fast transactions and cannot guard this bottleneck",
            })
        telemetry = measurement.get("telemetry", {})
        wal = telemetry.get("wal_timing", {})
        if wal and not wal.get("timing_enabled"):
            alerts.append({"kind": "missing_wal_timing", "severity": "medium",
                           "interpretation": "WAL counts cannot distinguish fast writes from slow fsync without timing"})
        for anomaly in telemetry.get("anomalies", []):
            if anomaly in {"wal_sync_latency", "wal_wait", "host_io_pressure", "lock_contention", "temp_spill"}:
                alerts.append({"kind": anomaly, "severity": "high", "evidence": {
                    "wal_timing": wal, "host_io": telemetry.get("host_io", {}),
                    "wait_gauges": {name: telemetry.get("gauges", {}).get(name) for name in
                                    ("wal_waiting_backends", "io_waiting_backends", "lock_waiting_backends")}}})
        baseline = incumbent.verified_performance.get("median_throughput_rps") or incumbent.verified_performance.get("throughput_rps")
        current = measurement.get("throughput_rps")
        if baseline and current is not None:
            change = (float(current) / float(baseline) - 1.0) * 100.0
            if change <= -10.0:
                alerts.append({"kind": "same_config_throughput_collapse", "severity": "high",
                               "change_percent": change,
                               "interpretation": "diagnose the simultaneous transaction/WAL/I/O evidence before a planner-only action"})
        return alerts

    def _proposal_patch(self, proposal, round_number: int, base_version: int) -> GraphPatch:
        operations = [PatchOperation(operation_id=f"{proposal.proposal_id}:hypothesis-op",
            kind="add_hypothesis", subject_id=f"{proposal.proposal_id}:hypothesis",
            payload={"title": proposal.title, "anomaly": proposal.anomaly.model_dump(mode="json"),
                     "mechanism": proposal.mechanism, "alternatives": [x.model_dump(mode="json") for x in proposal.alternatives]})]
        if proposal.knob_changes:
            operations.append(PatchOperation(operation_id=f"{proposal.proposal_id}:intervention-op",
                kind="add_intervention", subject_id=f"{proposal.proposal_id}:intervention",
                references=[f"{proposal.proposal_id}:hypothesis"],
                payload={"knob_changes": [x.model_dump(mode="json") for x in proposal.knob_changes],
                         "reason": proposal.why_exceeds_incumbent}))
        else:
            operations.append(PatchOperation(operation_id=f"{proposal.proposal_id}:observation-op",
                kind="add_observation_request", subject_id=f"{proposal.proposal_id}:observation",
                references=[f"{proposal.proposal_id}:hypothesis"],
                payload={"metrics": [x.model_dump(mode="json") for x in proposal.intermediate_metrics],
                         "distinguishes": [x.model_dump(mode="json") for x in proposal.alternatives]}))
        operations.append(PatchOperation(operation_id=f"{proposal.proposal_id}:prediction-op",
            kind="add_prediction", subject_id=f"{proposal.proposal_id}:predictions",
            references=[f"{proposal.proposal_id}:hypothesis"],
            payload={"predictions": [x.model_dump(mode="json") for x in proposal.predictions]}))
        operations.append(PatchOperation(operation_id=f"{proposal.proposal_id}:challenge-op",
            kind="challenge", subject_id=f"{proposal.proposal_id}:challenge",
            references=[f"{proposal.proposal_id}:hypothesis"],
            payload={"competing_explanations": [x.model_dump(mode="json") for x in proposal.alternatives],
                     "resolution": "coordinator must select a discriminating observation or intervention"}))
        return GraphPatch(patch_id=f"round-{round_number:03d}-{proposal.expert}", episode_id=self.experiment_id,
            workload=self.target, author=proposal.expert, base_version=base_version,
            operations=operations, confidence=0.5)

    def _incumbent(self, config: dict[str, Any], context: dict[str, Any], measurements: list[dict[str, Any]], experiment_id: str, evidence: dict[str, Any]) -> IncumbentSnapshot:
        values = [float(x["throughput_rps"]) for x in measurements]
        return IncumbentSnapshot(config=config, config_hash=stable_hash(config),
            verified_performance={"throughput_rps": min(values), "median_throughput_rps": median(values), "replicate_throughput_rps": values},
            guardrails={"healthy": all(x.get("healthy", False) for x in measurements)},
            workload_fingerprint=context["workload_fingerprint"], workload_fingerprint_hash=stable_hash(context["workload_fingerprint"]),
            database_state=context, evidence=measurements[-1], accepted_experiment_id=experiment_id)

    def _summary(self, reason: str, rounds: int, unexplained: bool) -> dict[str, Any]:
        return {"stop_reason": reason, "completed_rounds": rounds, "workload_measurements": self.executor.measurement_count,
                "warmup_runs": self.executor.warmup_count, "unexplained_performance": unexplained,
                "episode_initial_tps": self.episode_initial_tps,
                "source_profile_measurements": self.source_profile_measurements}

    def _model_usage(self) -> dict[str, int]:
        calls = tokens = 0
        for path in (self.directory / "llm").glob("*_metadata.json"):
            try:
                usage = json.loads(path.read_text(encoding="utf-8")).get("usage", {})
            except (OSError, json.JSONDecodeError):
                continue
            calls += 1
            tokens += int(usage.get("total_tokens", 0) or 0)
        return {"model_calls": calls, "model_tokens": tokens}

    def _historical_baseline_check(self, context_hash: str, current: dict[str, Any]) -> dict[str, Any]:
        registry = self.root / "artifacts" / "multi_agent" / "baseline_registry.jsonl"
        records: list[dict[str, Any]] = []
        if registry.exists():
            for line in registry.read_text(encoding="utf-8").splitlines():
                try: records.append(json.loads(line))
                except json.JSONDecodeError: continue
        # Import earlier experiment artifacts once; this also audits runs made before the registry existed.
        for path in (self.root / "artifacts" / "multi_agent").glob("*/*/baseline_stability.json"):
            if self.directory in path.parents:
                continue
            manifest_path = path.parent / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                baseline = json.loads(path.read_text(encoding="utf-8"))
                records.append({"context_hash": stable_hash(manifest["context"]), "median": baseline.get("median"),
                                "experiment_id": manifest.get("experiment_id"), "source": "artifact_scan"})
            except (OSError, KeyError, json.JSONDecodeError):
                continue
        prior = [r for r in records if r.get("context_hash") == context_hash and isinstance(r.get("median"), (int, float))]
        current_median = current.get("median")
        ratios = [max(float(current_median), float(r["median"])) / max(min(float(current_median), float(r["median"])), 1e-12)
                  for r in prior] if isinstance(current_median, (int, float)) else []
        result = {"comparable": not ratios or max(ratios) < 2.0, "prior_count": len(prior),
                  "prior_medians": [r["median"] for r in prior], "maximum_ratio": max(ratios) if ratios else 1.0}
        _append(registry, {"at": utc_now(), "experiment_id": self.experiment_id, "direction": f"{self.source}_to_{self.target}",
                           "context_hash": context_hash, "median": current_median, "values": current.get("values", []),
                           "within_run_stable": current.get("stable"), "historical_comparable": result["comparable"]})
        return result

    @staticmethod
    def _invalid_outcome(experiment_id: str, proposal, error: Exception):
        from .models import ValidationOutcome
        return ValidationOutcome(experiment_id=experiment_id, validity="invalid",
            validity_reasons=[f"executor failure: {type(error).__name__}: {str(error)[:500]}"],
            performance_verdict="invalid", mechanism_verdict="inconclusive", accepted_as_incumbent=False,
            unexplained_performance=False,
            edge_results={edge.edge_id: {"state":"inconclusive", "reason":"experiment executor failed", "observations":[]} for edge in proposal.edges},
            actual_settings_match=False)
