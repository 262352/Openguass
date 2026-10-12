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
from .models import IncumbentSnapshot, utc_now
from .reporting import ReportBuilder
from .validator import EvidenceValidator


def _append(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n")


class MultiAgentRunner:
    def __init__(self, root: Path, experiment_id: str, source: str, target: str, *,
                 max_rounds: int = 12, max_measurements: int = 60, duration: int = 30,
                 warmup_seconds: int = 30, terminals: int = 2, seed: int = 20261012,
                 baseline_repeats: int = 3, drift_percent: float = 25.0):
        self.root, self.experiment_id, self.source, self.target = root, experiment_id, source, target
        self.directory = root / "artifacts" / "multi_agent" / experiment_id / f"{source}_to_{target}"
        self.directory.mkdir(parents=True, exist_ok=False)
        self.max_rounds, self.max_measurements, self.baseline_repeats = max_rounds, max_measurements, baseline_repeats
        self.executor = ExperimentExecutor(self.directory, target, duration=duration, warmup_seconds=warmup_seconds,
                                           terminals=terminals, seed=seed)
        self.validator = EvidenceValidator(drift_percent=drift_percent, repeat_spread_percent=drift_percent)
        self.coordinator = Coordinator()
        self.panel: ExpertPanel | None = None
        self.reporter = ReportBuilder(self.directory)
        self.ledger = self.directory / "experiment_ledger.jsonl"

    def run(self) -> dict[str, Any]:
        start_config, hardware = self.executor.capture()
        source_file = self.root / "artifacts" / "source_configs" / f"{self.source}.json"
        if source_file.exists():
            start_config = json.loads(source_file.read_text(encoding="utf-8")).get("config", start_config)
        start_config = self.executor.space.reconcile(start_config)
        source_hash = stable_hash(start_config)
        context = self.executor.context(source_hash, hardware)
        context_hash = stable_hash(context)
        manifest = {"schema_version": "andromeda.multi-agent-experiment/v1", "experiment_id": self.experiment_id,
            "source": self.source, "target": self.target, "created_at": utc_now(), "context": context,
            "budgets": {"rounds": self.max_rounds, "workload_measurements": self.max_measurements},
            "policy": "No GP/EI/Bayesian component participates in online decisions."}
        (self.directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        graph = GraphStore(self.directory, source_hash)
        refuted: set[tuple[Any, ...]] = set()
        unexplained = False
        stop_reason = "budget_exhausted"
        try:
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
            (self.directory / "incumbent.json").write_text(incumbent.model_dump_json(indent=2), encoding="utf-8")
            self.panel = ExpertPanel(DeepSeekExpertBackend(self.directory / "llm"))
            for round_number in range(1, self.max_rounds + 1):
                remaining = self.max_measurements - self.executor.measurement_count
                if remaining < 4:
                    stop_reason = "measurement_budget_exhausted"; break
                payload = {"round": round_number, "current_incumbent": incumbent.model_dump(mode="json"),
                    "current_measurement": incumbent.evidence, "allowed_knobs": self.executor.space.describe(),
                    "graph_frontier": graph.snapshot("pending").model_dump(mode="json"),
                    "tested_summary": graph.snapshot("tested").model_dump(mode="json"),
                    "remaining_budget": {"rounds": self.max_rounds-round_number+1, "workload_measurements": remaining}}
                try:
                    proposals = self.panel.propose_parallel(payload)
                except Exception as error:
                    stop_reason = "model_unavailable"
                    _append(self.ledger, {"event": "expert_panel_failed", "round": round_number,
                        "error_type": type(error).__name__, "error": str(error)[:2000]})
                    break
                for proposal in proposals:
                    if proposal.incumbent_config_hash != incumbent.config_hash or proposal.workload_fingerprint_hash != incumbent.workload_fingerprint_hash:
                        raise RuntimeError("expert proposal context does not match current incumbent")
                try:
                    reviews = self.panel.review_parallel(round_number, incumbent.model_dump(mode="json"), proposals)
                except Exception as error:
                    stop_reason = "cross_review_unavailable"
                    _append(self.ledger, {"event": "cross_review_failed", "round": round_number,
                        "error_type": type(error).__name__, "error": str(error)[:2000]})
                    break
                canonical, merged = self.coordinator.merge_exact_duplicates(proposals)
                graph.record_hypothesis_pool(proposals)
                graph.add_proposals(canonical)
                for proposal in canonical:
                    graph.merge_provenance(proposal, merged.get(proposal.proposal_id, []))
                selection = self.coordinator.select(canonical, reviews, current_config=incumbent.config,
                    refuted_signatures=refuted, remaining_measurements=remaining)
                proposal_record = {"event": "proposal_frozen", "round": round_number,
                    "incumbent": incumbent.model_dump(mode="json"), "proposals": [p.model_dump(mode="json") for p in proposals],
                    "cross_reviews": [r.model_dump(mode="json") for r in reviews], "selection": selection.model_dump(mode="json")}
                _append(self.ledger, proposal_record)
                (self.directory / f"round_{round_number:03d}_decision.json").write_text(json.dumps(proposal_record, ensure_ascii=False, indent=2), encoding="utf-8")
                if not selection.selected_proposal_id:
                    stop_reason = "no_actionable_hypothesis"; break
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
                "warmup_runs": self.executor.warmup_count,
                "postgresql_restarts": self.executor.restart_count,
                "configuration_applications": self.executor.apply_count,
                **self._model_usage(), "final_rollback": rollback,
            })

    def _incumbent(self, config: dict[str, Any], context: dict[str, Any], measurements: list[dict[str, Any]], experiment_id: str, evidence: dict[str, Any]) -> IncumbentSnapshot:
        values = [float(x["throughput_rps"]) for x in measurements]
        return IncumbentSnapshot(config=config, config_hash=stable_hash(config),
            verified_performance={"throughput_rps": min(values), "median_throughput_rps": median(values), "replicate_throughput_rps": values},
            guardrails={"healthy": all(x.get("healthy", False) for x in measurements)},
            workload_fingerprint=context["workload_fingerprint"], workload_fingerprint_hash=stable_hash(context["workload_fingerprint"]),
            database_state=context, evidence=measurements[-1], accepted_experiment_id=experiment_id)

    def _summary(self, reason: str, rounds: int, unexplained: bool) -> dict[str, Any]:
        return {"stop_reason": reason, "completed_rounds": rounds, "workload_measurements": self.executor.measurement_count,
                "warmup_runs": self.executor.warmup_count, "unexplained_performance": unexplained}

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
