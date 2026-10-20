from __future__ import annotations

import json

from app.continuous.diagnosis_control import ActionConsistencyAuditor, HypothesisStateTable
from app.continuous.search_space import BoundedSearchPlanner, SearchSpaceCompiler
from app.multi_agent.models import (
    AlternativeExplanation, CostEstimate, ExpertProposal, FrozenPrediction,
    KnobChange, MetricSpec, ProposedEdge, ProposedNode, ValidationOutcome,
)
from app.phase2.space import FullKnobSpace


def proposal(pid: str, *, knob: str = "random_page_cost", old=4.0, new=2.0,
             low=1.0, high=4.0, step=.5) -> ExpertProposal:
    nodes = [ProposedNode(node_id=f"{pid}:i", kind="intervention", label=knob),
             ProposedNode(node_id=f"{pid}:m", kind="mechanism", label="mechanism"),
             ProposedNode(node_id=f"{pid}:o", kind="objective", label="TPS")]
    edges = [ProposedEdge(edge_id=f"{pid}:e1", source=nodes[0].node_id,
                          target=nodes[1].node_id, relation="changes", context_hash="cfg",
                          prediction_ids=[f"{pid}:p1"]),
             ProposedEdge(edge_id=f"{pid}:e2", source=nodes[1].node_id,
                          target=nodes[2].node_id, relation="improves", context_hash="cfg",
                          prediction_ids=[f"{pid}:p2"])]
    predictions = [FrozenPrediction(prediction_id=f"{pid}:p1", edge_id=edges[0].edge_id,
        metric_id="p95_us", expected_direction="decrease", minimum_change=1,
        comparison_method="relative_percent", rationale="mechanism"),
        FrozenPrediction(prediction_id=f"{pid}:p2", edge_id=edges[1].edge_id,
        metric_id="throughput_rps", expected_direction="increase", minimum_change=1,
        comparison_method="relative_percent", rationale="objective")]
    return ExpertProposal(proposal_id=pid, expert="query_execution",
        incumbent_config_hash="cfg", workload_fingerprint_hash="wf", title="test",
        anomaly=MetricSpec(metric_id="p95_us", source="test", measurement_type="state",
            unit="us", aggregation="median", sampling_window_seconds=5, observed_value=100),
        configuration_cause="configuration", mechanism="mechanism",
        intermediate_metrics=[MetricSpec(metric_id="p95_us", source="test",
            measurement_type="state", unit="us", aggregation="median",
            sampling_window_seconds=5, observed_value=100)],
        query_effect="query effect", expected_tps_effect="increase",
        expected_latency_effect="decrease",
        knob_changes=[KnobChange(knob=knob, old_value=old, new_value=new,
            recommended_min=low, recommended_max=high, initial_step=step,
            numerical_basis="expert bounded range", mechanism_role="cause")],
        predictions=predictions,
        alternatives=[AlternativeExplanation(alternative_id=f"{pid}:a",
            statement="noise", discriminating_metric_id="throughput_rps",
            expected_direction="unchanged")], why_exceeds_incumbent="evidence",
        cost=CostEstimate(restart_required=False, workload_measurements=4,
                          estimated_seconds=20), nodes=nodes, edges=edges)


def outcome(pid: str, performance: str, mechanism: str) -> ValidationOutcome:
    return ValidationOutcome(experiment_id=f"exp:{pid}", validity="valid", validity_reasons=[],
        performance_verdict=performance, mechanism_verdict=mechanism,
        accepted_as_incumbent=performance == "verified_improvement",
        unexplained_performance=False, edge_results={},
        verified_performance={"throughput_rps": 120}, actual_settings_match=True)


def test_action_fidelity_accepts_exact_expert_action_and_logs_it(tmp_path):
    space = FullKnobSpace(); current = space.current_config()
    p = proposal("p1", old=current["random_page_cost"])
    candidate, record = ActionConsistencyAuditor(tmp_path, space).freeze(1, p, current)
    assert candidate is not None and candidate["random_page_cost"] == 2.0
    assert record["verdict"] == "exact_match"
    assert "action_substitution" not in (tmp_path / "action_consistency.jsonl").read_text()


def test_action_fidelity_rejects_implicit_dependency_substitution(tmp_path):
    space = FullKnobSpace(); current = space.current_config()
    p = proposal("p2", knob="max_parallel_workers", old=current["max_parallel_workers"],
                 new=0, low=0, high=16, step=2)
    candidate, record = ActionConsistencyAuditor(tmp_path, space).freeze(1, p, current)
    assert candidate is None and record["verdict"] == "action_substitution"
    assert any("unrequested knob max_parallel_workers_per_gather" in x for x in record["reasons"])


def test_exhaustion_requires_every_hypothesis_terminal_and_two_empty_rounds(tmp_path):
    table = HypothesisStateTable(tmp_path)
    p = proposal("p1")
    assert table.register_round(1, [p], "cfg") == 1
    assert not table.can_exhaust(2)
    table.transition("p1", "tested_supported", "measured")
    table.register_round(2, [], "cfg"); table.register_round(3, [], "cfg")
    assert table.can_exhaust(2)
    report = table.write_exhaustion_report(stop_reason="diagnosis_exhausted",
        consecutive_no_new=2, coordinator_summary="all graph frontier edges closed")
    assert report["trigger_satisfied"] and report["status_counts"] == {"tested_supported": 1}


def test_new_evidence_supersedes_siblings_before_experts_revise(tmp_path):
    table = HypothesisStateTable(tmp_path)
    first, second, third = proposal("p1"), proposal("p2", new=2.5), proposal("p3", new=3.0)
    assert table.register_round(1, [first, second, third], "cfg") == 3
    table.transition("p1", "testing", "selected graph frontier")
    table.transition("p1", "tested_supported", "measured")
    changed = table.supersede_pending(tested_proposal_id="p1",
        experiment_result={"experiment_id": "e1", "paired_tps_gain_percent": [10, 12]})
    assert set(changed) == {"p2", "p3"}
    assert table.records["p2"]["status"] == "parked"
    assert table.records["p3"]["superseding_experiment"]["experiment_id"] == "e1"
    assert table.proposed("cfg") == []


def test_parked_or_underpowered_action_can_be_revised_in_a_new_epoch(tmp_path):
    table = HypothesisStateTable(tmp_path)
    first = proposal("p1")
    assert table.register_round(1, [first], "cfg") == 1
    table.transition("p1", "underpowered", "regime mismatch")
    table.advance_epoch("experiment-1")
    revised = proposal("p2")
    assert table.register_round(2, [revised], "cfg") == 1
    assert table.records["p2"]["status"] == "proposed"


def test_compiler_freezes_supported_range_and_planner_stays_inside(tmp_path):
    space = FullKnobSpace(); current = space.current_config()
    table = HypothesisStateTable(tmp_path)
    p = proposal("p1", old=current["random_page_cost"], new=2.0, low=1.0, high=4.0)
    table.register_round(1, [p], "cfg")
    table.transition("p1", "tested_supported", "supported without TPS gain")
    definition = SearchSpaceCompiler(tmp_path, space).compile(table,
        [{"proposal": p, "outcome": outcome("p1", "no_improvement", "supported")}], current)
    entry = definition["parameters"][0]
    assert entry["compile_rule"].startswith("supported mechanism")
    assert entry["low"] == 2.0 and entry["high"] == 3.0
    planner = BoundedSearchPlanner(definition, space)
    candidate = planner.candidate(current, round_number=2, config_hash="cfg",
        workload_hash="wf", duration=5, workload="tpcc")
    assert candidate is not None
    config, _ = candidate
    assert entry["low"] <= config["random_page_cost"] <= entry["high"]


def test_compiler_keeps_inconclusive_expert_dimension_in_narrow_range(tmp_path):
    space = FullKnobSpace(); current = space.current_config()
    table = HypothesisStateTable(tmp_path)
    item = proposal("p1", knob="wal_writer_delay", old=current["wal_writer_delay"],
                    new=100, low=20, high=500, step=50)
    table.register_round(1, [item], "cfg")
    table.transition("p1", "tested_inconclusive", "unstable paired evidence")
    uncertain = outcome("p1", "inconclusive", "inconclusive")
    uncertain = uncertain.model_copy(update={"validity": "unstable", "accepted_as_incumbent": False})
    definition = SearchSpaceCompiler(tmp_path, space).compile(table,
        [{"proposal": item, "outcome": uncertain}], current)
    entry = next(value for value in definition["parameters"]
                 if value["knob"] == "wal_writer_delay")
    assert entry["evidence_confidence"] == "low"
    assert entry["compile_rule"].startswith("expert-rational")
    assert entry["low"] >= 50 and entry["high"] <= 150


def test_unstable_baseline_retries_once_then_uses_final_three_median(tmp_path, monkeypatch):
    import app.continuous.runner as runner_module

    class UnstableExecutor:
        def __init__(self, directory, workload, **kwargs):
            self.space = FullKnobSpace(); self.executor = self
            self.duration = kwargs["duration"]; self.seed = kwargs["seed"]
            self.measurement_count = self.warmup_count = 0
            self.restart_count = self.apply_count = 0
        def capture(self): return self.space.current_config(), {"host": "fake"}
        def capture_catalog(self):
            return [{"name": d.name, "boot_val": d.current, "setting": d.current,
                     "source": "default", "unit": d.unit, "context": d.execution_class,
                     "sourcefile": None, "pending_restart": False}
                    for d in self.space.dimensions]
        def context(self, source_hash, hardware):
            return {"source_config_hash": source_hash,
                    "workload_fingerprint": {"workload": "tpcc", "sha256": "wf"},
                    "snapshot": {"path_name": "tpcc.dump", "sha256": "snap"},
                    "hardware": hardware}
        def measure(self, config, label):
            values = (100.0, 220.0, 50.0)
            value = values[self.measurement_count % 3]
            self.measurement_count += 1
            return {"throughput_rps": value, "healthy": True}, {
                "observed": {}, "session_settings": {}}
        def restore(self): return {"restored": True}

    monkeypatch.setattr(runner_module, "ExperimentExecutor", UnstableExecutor)
    monkeypatch.setattr(runner_module, "DeepSeekExpertBackend",
                        lambda path: (_ for _ in ()).throw(AssertionError("experts must not start")))
    monkeypatch.setattr(runner_module.TwitterContinuousRunner,
                        "_workload_data_preflight", lambda self: None)
    runner = runner_module.TwitterContinuousRunner(tmp_path, "unstable", workload="tpcc",
        max_rounds=1, search_rounds=0, max_measurements=20, max_model_calls=3,
        duration=5, warmup_seconds=0, terminals=1, baseline_repeats=3,
        baseline_stability_retries=1)
    report = runner.run()
    out = tmp_path / "artifacts/tpcc_continuous/unstable"
    assert report["baseline_stable"] is False
    assert report["baseline_fallback_used"] is True
    assert report["workload_measurements"] == 6
    incumbent = json.loads((out / "incumbent.json").read_text())
    assert incumbent["verified_performance"]["throughput_rps"] == 100.0
    assert incumbent["verified_performance"]["median_throughput_rps"] == 100.0
    assert incumbent["verified_performance"]["score_method"] == \
        "median_of_final_three_unstable_baseline_fallback"
    attempts = json.loads((out / "baseline_stability_attempts.json").read_text())
    assert len(attempts["attempts"]) == 2 and attempts["hard_gate"] is False
    assert attempts["fallback_used"] is True


def test_two_stage_runner_smoke_executes_expert_action_without_substitution(tmp_path, monkeypatch):
    import app.continuous.runner as runner_module

    class FakeExecutor:
        def __init__(self, directory, workload, **kwargs):
            self.directory, self.workload = directory, workload
            self.space = FullKnobSpace(); self.executor = self
            self.duration = kwargs["duration"]; self.seed = kwargs["seed"]
            self.measurement_count = self.warmup_count = 0
            self.restart_count = self.apply_count = 0
        def capture(self): return self.space.current_config(), {"host": "fake"}
        def capture_catalog(self):
            return [{"name": d.name, "boot_val": d.current, "setting": d.current,
                     "source": "default", "unit": d.unit, "context": d.execution_class,
                     "sourcefile": None, "pending_restart": False}
                    for d in self.space.dimensions]
        def context(self, source_hash, hardware):
            self.expected_context = {"source_config_hash": source_hash,
                    "workload_fingerprint": {"workload": "tpcc", "sha256": "wf"},
                    "snapshot": {"path_name": "tpcc.dump", "sha256": "snap"},
                    "hardware": hardware}
            return self.expected_context
        def _psql(self, sql): return "5"
        @staticmethod
        def _measurement(tps, p95):
            return {"throughput_rps": tps, "p95_us": p95, "average_us": p95,
                    "p99_us": p95, "healthy": True, "client": {},
                    "telemetry": {"per_transaction": {}, "rates_per_second": {}, "gauges": {}},
                    "experiment_context": {"source_config_hash": "ignored"}}
        def measure(self, config, label):
            self.measurement_count += 1
            return self._measurement(100, 100), {"observed": {}, "session_settings": {}}
        def run_paired(self, incumbent, candidate, round_number):
            assert candidate["random_page_cost"] in {2.0, 3.0}
            self.measurement_count += 4
            controls = [self._measurement(100, 100), self._measurement(101, 100)]
            treatments = [self._measurement(120, 80), self._measurement(121, 80)]
            for item in controls + treatments:
                item["experiment_context"] = self.expected_context
            return {"order": ["control_1", "treatment_1", "control_2", "treatment_2"],
                    "controls": controls, "treatments": treatments, "applies": {},
                    "observed_candidate_settings": {
                        "random_page_cost": candidate["random_page_cost"]}}
        def apply(self, config, space): return {}
        def restore(self): return {"restored": True}

    class FakePanel:
        def __init__(self, backend, roles=None): pass
        def propose_parallel(self, payload):
            if payload["round"] == 1:
                assert payload["latest_experiment_result"] is None
                item = proposal("expert-p1",
                    old=payload["current_incumbent"]["config"]["random_page_cost"], new=2.0)
            else:
                shared = payload["latest_experiment_result"]
                assert shared["tested_proposal_id"] == "expert-p1"
                assert shared["accepted_as_incumbent"] is True
                assert shared["controls"][0]["throughput_rps"] == 100
                item = proposal("expert-p2",
                    old=payload["current_incumbent"]["config"]["random_page_cost"], new=3.0)
            data = item.model_dump(mode="json")
            data["incumbent_config_hash"] = payload["current_incumbent"]["config_hash"]
            data["workload_fingerprint_hash"] = payload["current_incumbent"]["workload_fingerprint_hash"]
            for edge in data["edges"]:
                edge["context_hash"] = data["incumbent_config_hash"]
            return [ExpertProposal.model_validate(data)]

    monkeypatch.setattr(runner_module, "ExperimentExecutor", FakeExecutor)
    monkeypatch.setattr(runner_module, "DeepSeekExpertBackend", lambda path: object())
    monkeypatch.setattr(runner_module, "ExpertPanel", FakePanel)
    monkeypatch.setattr(runner_module.TwitterContinuousRunner, "_workload_data_preflight", lambda self: None)
    run = runner_module.TwitterContinuousRunner(tmp_path, "smoke", workload="tpcc",
        max_rounds=2, search_rounds=0, max_measurements=11, max_model_calls=6,
        duration=5, warmup_seconds=0, terminals=1, baseline_repeats=3)
    run.run()
    out = tmp_path / "artifacts/tpcc_continuous/smoke"
    actions = [json.loads(line) for line in (out / "action_consistency.jsonl").read_text().splitlines()]
    assert actions and all(item["verdict"] == "exact_match" for item in actions)
    statuses = json.loads((out / "hypothesis_status.json").read_text())["hypotheses"]
    assert [item["status"] for item in statuses] == ["tested_supported", "tested_supported"]
    ledger = [json.loads(line) for line in (out / "experiment_ledger.jsonl").read_text().splitlines()]
    broadcasts = [item for item in ledger if item.get("event") == "evidence_broadcast_to_all_experts"]
    assert len(broadcasts) == 2
    assert (out / "FROZEN_SEARCH_SPACE.json").is_file()
