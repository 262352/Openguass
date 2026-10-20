from __future__ import annotations

import json

from app.multi_agent.coordinator import Coordinator
from app.multi_agent.graph import GraphStore
from app.multi_agent.models import (
    AlternativeExplanation, CostEstimate, CrossReview, ExpertProposal, FrozenPrediction,
    GraphEdge, GraphNode, IncumbentSnapshot, InteractionDesign, KnobChange, MetricSpec, ProposedEdge, ProposedNode,
    ValidationOutcome,
)
from app.multi_agent.reporting import ReportBuilder
from app.multi_agent.runner import MultiAgentRunner
from app.multi_agent.validator import EvidenceValidator, _metric
from app.multi_agent.executor import stable_hash
from app.multi_agent.experts import DeepSeekExpertBackend, ExpertPanel
from app.phase2.space import FullKnobSpace


def proposal(pid="p1", expert="query_execution", *, mechanism="planner estimate quality", knob="random_page_cost",
             old=4.0, new=1.1, metric="idx_scan_per_txn", direction="increase", interaction=False):
    prefix = pid.replace(":", "_")
    nodes = [
        ProposedNode(node_id=f"{prefix}_i", kind="intervention", label=knob),
        ProposedNode(node_id=f"{prefix}_m", kind="mechanism", label=mechanism),
        ProposedNode(node_id=f"{prefix}_e", kind="evidence", label=metric),
        ProposedNode(node_id=f"{prefix}_o", kind="objective", label="TPS"),
    ]
    if interaction:
        nodes.insert(2, ProposedNode(node_id=f"{prefix}_x", kind="interaction", label="A x B"))
    path = [n.node_id for n in nodes]
    edges = []
    predictions = []
    for i, (source, target) in enumerate(zip(path, path[1:]), 1):
        eid, prid = f"{prefix}_edge{i}", f"{prefix}_pred{i}"
        edges.append(ProposedEdge(edge_id=eid, source=source, target=target, relation="affects", context_hash="cfg", prediction_ids=[prid]))
        predictions.append(FrozenPrediction(prediction_id=prid, edge_id=eid,
            metric_id="throughput_rps" if target == f"{prefix}_o" else metric,
            expected_direction="increase" if target == f"{prefix}_o" else direction,
            minimum_change=5, comparison_method="relative_percent", rationale="pre-registered"))
    changes = [KnobChange(knob=knob, old_value=old, new_value=new, numerical_basis="catalog range", mechanism_role="cause")]
    if interaction:
        changes.append(KnobChange(knob="seq_page_cost", old_value=1.0, new_value=.8, numerical_basis="paired factor", mechanism_role="factor B"))
    return ExpertProposal(proposal_id=pid, expert=expert, incumbent_config_hash="cfg", workload_fingerprint_hash="wf",
        title="test", anomaly=MetricSpec(metric_id=metric, source="telemetry", measurement_type="per_transaction",
            unit="count/txn", aggregation="window delta / TPS", sampling_window_seconds=30, observed_value=1),
        configuration_cause="configuration", mechanism=mechanism,
        intermediate_metrics=[MetricSpec(metric_id=metric, source="telemetry", measurement_type="per_transaction",
            unit="count/txn", aggregation="window delta / TPS", sampling_window_seconds=30, observed_value=1)],
        query_effect="observable query behavior", expected_tps_effect="increase", expected_latency_effect="decrease",
        knob_changes=changes, predictions=predictions,
        alternatives=[AlternativeExplanation(alternative_id=f"{prefix}_alt", statement="CPU pressure",
            discriminating_metric_id="wal_bytes_per_txn", expected_direction="unchanged")],
        why_exceeds_incumbent="removes observed bottleneck", cost=CostEstimate(restart_required=False, workload_measurements=4, estimated_seconds=120),
        nodes=nodes, edges=edges, interaction_factors=["A", "B"] if interaction else [],
        interaction_predictions={"baseline": 100, "a_only": 105, "b_only": 105, "a_plus_b": 120} if interaction else {},
        interaction_design=InteractionDesign(factor_a_knobs=[knob], factor_b_knobs=["seq_page_cost"],
            predicted_tps={"baseline":100,"a_only":105,"b_only":105,"a_plus_b":120}) if interaction else None)


def measurement(tps, metric=1.0, *, plan=None, healthy=True):
    return {"throughput_rps": tps, "p95_us": 100, "healthy": healthy,
            "telemetry": {"per_transaction": {"idx_scan_per_txn": metric, "wal_bytes_per_txn": metric},
                          "rates_per_second": {"idx_scan": metric * tps}, "gauges": {}},
            "plan_evidence": plan or {}, "client": {"failed": 0, "timeouts": 0, "unexpected_sql_errors": 0}}


def test_model_visible_measurement_keeps_diagnostics_without_full_context_duplication():
    value = measurement(123, plan={"critical": "plan"})
    value.update({"label": "baseline_1", "run_id": "run-1",
        "experiment_context": {"duration": 30, "warmup_seconds": 30, "terminals": 2,
                               "seed": 7, "hardware": {"large": "duplicate"}}})
    visible = MultiAgentRunner._visible_measurement(value, include_diagnostics=True)
    assert visible["telemetry"] == value["telemetry"]
    assert visible["plan_evidence"] == {"critical": "plan"}
    assert visible["measurement_protocol"] == {
        "duration": 30, "warmup_seconds": 30, "terminals": 2, "seed": 7}
    assert "experiment_context" not in visible and "hardware" not in visible

    baseline = MultiAgentRunner._visible_measurement(value, include_diagnostics=False)
    assert "telemetry" not in baseline and "plan_evidence" not in baseline


def validate(p, controls, treatments, observed=None):
    requested = {x.knob: x.new_value for x in p.knob_changes}
    observed = observed or requested
    return EvidenceValidator(repeat_spread_percent=25).validate("exp1", p, incumbent_tps=100,
        controls=controls, treatments=treatments, requested_settings=requested, observed_settings=observed,
        expected_context={"server": "a"}, actual_contexts=[{"server": "a"}] * 4)


def test_same_mechanism_merges_and_keeps_expert_provenance(tmp_path):
    a = proposal("p1", "query_execution")
    b = proposal("p2", "memory_storage")
    canonical, merged = Coordinator().merge_exact_duplicates([a, b])
    assert len(canonical) == 1 and merged["p1"] == [b]
    store = GraphStore(tmp_path, "cfg")
    store.add_proposals(canonical); store.merge_provenance(a, [b])
    assert set(store.edges["p1_edge1"].proposed_by) == {"query_execution", "memory_storage"}
    assert set(store.edges["p1_edge1"].proposal_ids) == {"p1", "p2"}


def test_competing_causes_remain_distinct_and_discriminating_review_wins():
    a = proposal("p1", mechanism="bad plan")
    b = proposal("p2", expert="memory_storage", mechanism="WAL pressure", knob="wal_writer_delay", old=200, new=50, metric="wal_bytes_per_txn", direction="decrease")
    canonical, merged = Coordinator().merge_exact_duplicates([a, b])
    assert len(canonical) == 2 and not merged
    review = CrossReview(review_id="r1", reviewer="concurrency_maintenance", proposal_id="p2", kind="competing_explanation",
        referenced_metric_ids=["wal_bytes_per_txn"], statement="distinguishes WAL from plan",
        discriminating_prediction=FrozenPrediction(prediction_id="rp", edge_id="p2_edge1", metric_id="wal_bytes_per_txn",
            expected_direction="decrease", minimum_change=5, comparison_method="relative_percent", rationale="different prediction"))
    config = {d.name: d.current for d in Coordinator().space.dimensions}; config.update({"random_page_cost": 4.0, "wal_writer_delay": 200})
    selected = Coordinator().select(canonical, [review], current_config=config, remaining_measurements=4)
    assert selected.selected_proposal_id == "p2"


def test_query_cost_group_with_effective_cache_size_is_executable():
    p = proposal()
    config = Coordinator().space.current_config()
    p = p.model_copy(update={"knob_changes": [
        KnobChange(knob="effective_cache_size", old_value=config["effective_cache_size"], new_value=786432,
                   numerical_basis="measured cache hypothesis", mechanism_role="capacity estimate"),
        KnobChange(knob="random_page_cost", old_value=config["random_page_cost"], new_value=2.0,
                   numerical_basis="measured plan hypothesis", mechanism_role="random I/O cost"),
        KnobChange(knob="seq_page_cost", old_value=config["seq_page_cost"], new_value=0.8,
                   numerical_basis="measured plan hypothesis", mechanism_role="sequential I/O cost"),
    ]})
    selected = Coordinator().select([p], [], current_config=config, remaining_measurements=4)
    assert selected.selected_proposal_id == p.proposal_id


def test_nested_transaction_metric_is_resolved_for_predictions():
    value = measurement(100)
    value["client"]["transaction_latency"] = {"InsertTweet": {"p50_us": 350_000}}
    assert _metric(value, "client.transaction_latency.InsertTweet.p50_us") == 350_000


def test_nested_wal_and_host_metrics_are_resolved_for_predictions():
    value = measurement(100)
    value["telemetry"].update({"wal_timing": {"sync_average_ms": 42.0},
                               "host_io": {"full_stall_fraction": .25}})
    assert _metric(value, "telemetry.wal_timing.sync_average_ms") == 42.0
    assert _metric(value, "host_io.full_stall_fraction") == .25


def test_nested_gauge_leaf_is_resolved_for_predictions():
    value = measurement(100)
    value["telemetry"]["gauges"]["wal_waiting_backends"] = {
        "median": 4.0, "p95": 5.0, "max": 5.0,
    }
    assert _metric(value, "telemetry.gauges.wal_waiting_backends.median") == 4.0
    assert _metric(value, "telemetry.gauges.wal_waiting_backends.p95") == 5.0


def test_aggregate_metric_prediction_is_inconclusive_instead_of_crashing():
    p = proposal(metric="telemetry.gauges.wal_waiting_backends", direction="decrease")
    controls = [measurement(100), measurement(101)]
    treatments = [measurement(110), measurement(111)]
    for item, median in zip(controls + treatments, (5.0, 4.0, 4.0, 3.0)):
        item["telemetry"]["gauges"]["wal_waiting_backends"] = {
            "median": median, "p95": 5.0, "max": 5.0,
        }
    outcome = validate(p, controls, treatments)
    observations = [observation for edge in outcome.edge_results.values()
                    for observation in edge["observations"]
                    if observation["prediction_id"].endswith("pred1")]
    assert observations[0]["state"] == "inconclusive"
    assert "not a finite scalar" in observations[0]["reason"]
    assert outcome.mechanism_verdict == "inconclusive"


def test_expert_proposal_rejects_known_aggregate_prediction_before_execution():
    value = measurement(100)
    value["telemetry"]["gauges"]["wal_waiting_backends"] = {
        "median": 4.0, "p95": 5.0, "max": 5.0,
    }
    aggregate = proposal(metric="telemetry.gauges.wal_waiting_backends")
    import pytest
    with pytest.raises(ValueError, match="resolves to a container"):
        DeepSeekExpertBackend._validate_prediction_metric_leaves(aggregate, value)

    leaf = proposal(metric="telemetry.gauges.wal_waiting_backends.median")
    DeepSeekExpertBackend._validate_prediction_metric_leaves(leaf, value)


def test_expert_panel_isolates_one_failed_expert():
    class Backend:
        def propose(self, role, payload):
            if role == "query_execution":
                raise ValueError("invalid_knob default_statistics_target: malformed value")
            return proposal(f"p-{role}", expert=role)

    panel = ExpertPanel(Backend())
    proposals = panel.propose_parallel({"round": 4})
    assert {item.expert for item in proposals} == {"memory_storage", "concurrency_maintenance"}
    assert panel.last_proposal_failures[0]["expert"] == "query_execution"
    assert panel.last_proposal_failures[0]["failure_kind"] == "invalid_knob"


def test_knob_prevalidation_rejects_unchanged_or_out_of_range_action():
    space = FullKnobSpace()
    current = space.current_config()
    payload = {"allowed_knobs": space.describe(), "current_incumbent": {"config": current}}
    unchanged = proposal(old=current["random_page_cost"], new=current["random_page_cost"])
    import pytest
    with pytest.raises(ValueError, match="new_value equals incumbent"):
        DeepSeekExpertBackend._validate_knob_changes(unchanged, payload)
    outside = proposal(old=current["random_page_cost"], new=99)
    with pytest.raises(ValueError, match="outside safe range"):
        DeepSeekExpertBackend._validate_knob_changes(outside, payload)


def test_raw_graph_reference_error_is_actionable():
    value = {
        "nodes": [{"node_id": "n1"}],
        "edges": [{"edge_id": "e1", "source": "default_statistics_target=300",
                   "target": "n1", "prediction_ids": ["p1"]}],
        "predictions": [{"prediction_id": "p1", "edge_id": "e1"}],
    }
    import pytest
    with pytest.raises(ValueError, match="not a declared node_id"):
        DeepSeekExpertBackend._validate_raw_graph_references(value)


def test_client_terminal_saturation_becomes_high_signal_alert():
    value = measurement(500)
    value["client"].update({"server_retry_ratio": .90,
                            "server_retry_transactions": 900,
                            "retryable_database_aborts": 900})
    value["client"]["bottleneck_summary"] = {
        "detected": True, "critical_transaction": "InsertTweet",
        "terminal_capacity_fraction": .92, "transaction_p50_us": 350_000,
        "overall_p95_us": 500, "tail_masked_by_overall_p95": True,
    }
    incumbent = IncumbentSnapshot(config={}, config_hash="cfg",
        verified_performance={"throughput_rps": 2500, "median_throughput_rps": 2700},
        guardrails={}, workload_fingerprint={}, workload_fingerprint_hash="wf",
        database_state={}, evidence=value, accepted_experiment_id="baseline")
    alerts = MultiAgentRunner._diagnostic_alerts(value, incumbent)
    kinds = {item["kind"] for item in alerts}
    assert {"client_terminal_saturation", "low_frequency_tail_masked_by_overall_percentile",
            "same_config_throughput_collapse", "serialization_retry_storm"} <= kinds


def test_transaction_tail_regression_is_diagnostic_only_for_tps_objective():
    p = proposal()
    controls = [measurement(100), measurement(101)]
    treatments = [measurement(120, .5), measurement(121, .5)]
    for item in controls:
        item["client"]["transaction_latency"] = {"InsertTweet": {"mean_us": 1000, "p50_us": 1000, "p99_us": 2000}}
    for item in treatments:
        item["client"]["transaction_latency"] = {"InsertTweet": {"mean_us": 2000, "p50_us": 2000, "p99_us": 4000}}
    outcome = validate(p, controls, treatments)
    assert outcome.performance_verdict == "verified_improvement"
    assert outcome.accepted_as_incumbent
    diagnostic = outcome.verified_performance["latency_diagnostic_only"]
    assert diagnostic["used_as_acceptance_gate"] is False
    assert max(diagnostic["maximum_regression_percent_by_pair"]) > 10


def test_tps_gain_with_retry_drop_is_flagged_for_semantic_review_not_rejected():
    p = proposal()
    controls = [measurement(100), measurement(101)]
    treatments = [measurement(120), measurement(121)]
    for item in controls:
        item["client"].update({"server_retry_ratio": .85,
            "successful_transactions": 12_000, "transaction_attempts": 80_000,
            "server_retry_transactions": 68_000, "final_failures": 0})
    for item in treatments:
        item["client"].update({"server_retry_ratio": .05,
            "successful_transactions": 14_400, "transaction_attempts": 15_200,
            "server_retry_transactions": 800, "final_failures": 0})
    outcome = validate(p, controls, treatments)
    assert outcome.accepted_as_incumbent
    semantics = outcome.verified_performance["transaction_semantics"]
    assert all(item["classification"] == "semantic_improvement_review" for item in semantics)
    assert all(item["acceptance_effect"] == "diagnostic_only" for item in semantics)


def test_equivalent_tps_is_inconclusive_not_mechanism_refutation():
    p = proposal()
    outcome = validate(p, [measurement(100), measurement(100)],
                       [measurement(101), measurement(99)])
    assert outcome.verified_performance["paired_decision"]["verdict"] == "reject_equivalent"
    assert outcome.performance_verdict == "inconclusive"
    assert outcome.mechanism_verdict == "inconclusive"
    assert all(value["state"] == "inconclusive" for edge_id, value in outcome.edge_results.items()
               if not EvidenceValidator._objective_edge(p, edge_id))


def test_two_stable_positive_pairs_are_accepted_even_below_old_gain_threshold():
    p = proposal()
    outcome = validate(p, [measurement(100), measurement(100)],
                       [measurement(101), measurement(102)])
    decision = outcome.verified_performance["paired_decision"]
    assert decision["statistical_verdict"] == "reject_equivalent"
    assert decision["verdict"] == "accept"
    assert decision["both_stable_pairs_positive"] is True
    assert outcome.accepted_as_incumbent


def test_only_material_tps_decline_refutes_tested_direction():
    p = proposal()
    outcome = validate(p, [measurement(100), measurement(100)],
                       [measurement(80), measurement(80)])
    assert outcome.verified_performance["paired_decision"]["verdict"] == "reject_harmful"
    assert outcome.performance_verdict == "no_improvement"
    assert outcome.mechanism_verdict == "refuted"


def test_independent_roots_to_tps_do_not_create_interaction(tmp_path):
    store = GraphStore(tmp_path, "cfg")
    store.add_proposals([proposal("p1"), proposal("p2", expert="memory_storage", mechanism="WAL pressure",
        knob="wal_writer_delay", old=200, new=50, metric="wal_bytes_per_txn", direction="decrease")])
    assert not any(n.kind == "interaction" for n in store.nodes.values())


def test_joint_success_without_factorial_controls_cannot_support_interaction():
    p = proposal("px", interaction=True)
    outcome = validate(p, [measurement(100), measurement(101)], [measurement(120, 1.2), measurement(121, 1.2)])
    interaction_node = next(n.node_id for n in p.nodes if n.kind == "interaction")
    interaction_edges = [e.edge_id for e in p.edges if interaction_node in {e.source, e.target}]
    assert all(outcome.edge_results[e]["state"] == "inconclusive" for e in interaction_edges)
    assert not EvidenceValidator.interaction_is_identified(p, {"baseline":[100,100], "a_plus_b":[120,120]})


def test_complete_factorial_can_support_interaction_only_when_nonadditive():
    p = proposal("px", interaction=True)
    factorial = {"baseline":[measurement(100),measurement(100)],
        "a_only":[measurement(105),measurement(105)], "b_only":[measurement(105),measurement(105)],
        "a_plus_b":[measurement(120,1.2),measurement(121,1.2)]}
    requested={x.knob:x.new_value for x in p.knob_changes}
    outcome=EvidenceValidator(repeat_spread_percent=25).validate("exp-factorial",p,incumbent_tps=100,
        controls=factorial["baseline"],treatments=factorial["a_plus_b"],requested_settings=requested,
        observed_settings=requested,expected_context={"server":"a"},actual_contexts=[{"server":"a"}]*8,
        factorial_measurements=factorial)
    interaction_node=next(n.node_id for n in p.nodes if n.kind=="interaction")
    interaction_edges=[e.edge_id for e in p.edges if interaction_node in {e.source,e.target}]
    assert interaction_edges and all(outcome.edge_results[e]["state"]=="tested_supported" for e in interaction_edges)


def test_performance_can_advance_while_failed_mechanism_stays_out_of_effective_graph(tmp_path):
    p = proposal()
    outcome = validate(p, [measurement(100, 1), measurement(101, 1)], [measurement(120, .5), measurement(121, .5)])
    assert outcome.accepted_as_incumbent and outcome.mechanism_verdict == "refuted" and outcome.unexplained_performance
    store = GraphStore(tmp_path, "cfg"); store.add_proposals([p]); store.apply_validation(p, outcome)
    assert store.snapshot("effective").edges == []


def test_raw_idx_scan_rate_following_tps_does_not_support_plan_edge():
    p = proposal(metric="idx_scan", direction="increase")
    outcome = validate(p, [measurement(100), measurement(101)], [measurement(120, 1.2), measurement(121, 1.2)])
    assert outcome.mechanism_verdict == "inconclusive"
    assert any("workload-volume coupled" in x["reason"] for edge in outcome.edge_results.values() for x in edge["observations"])


def test_arm_spread_is_a_tripwire_not_a_candidate_rejection_rule():
    verdict = EvidenceValidator().baseline_stability([measurement(400), measurement(1000), measurement(3000)])
    assert not verdict["stable"] and verdict["max_min_ratio"] > 2
    p = proposal()
    outcome = validate(p, [measurement(100), measurement(300)], [measurement(130), measurement(400)])
    assert outcome.validity == "valid" and outcome.accepted_as_incumbent
    assert outcome.verified_performance["paired_decision"]["verdict"] == "accept"


def test_wrong_applied_value_or_context_is_invalid():
    p = proposal()
    outcome = EvidenceValidator().validate("exp", p, incumbent_tps=100,
        controls=[measurement(100), measurement(101)], treatments=[measurement(120), measurement(121)],
        requested_settings={"random_page_cost": 1.1}, observed_settings={"random_page_cost": 4.0},
        expected_context={"server":"a"}, actual_contexts=[{"server":"b"}] * 4)
    assert outcome.validity == "invalid" and not outcome.accepted_as_incumbent


def test_effective_edge_traces_to_prediction_experiment_and_observation(tmp_path):
    p = proposal(direction="increase")
    outcome = validate(p, [measurement(100, 1), measurement(101, 1)], [measurement(120, 1.2), measurement(121, 1.2)])
    assert outcome.mechanism_verdict == "supported"
    store = GraphStore(tmp_path, "cfg"); store.add_proposals([p]); store.apply_validation(p, outcome)
    assert store.snapshot("effective").edges
    trace = store.trace_edge("p1_edge1")
    assert trace["frozen_predictions"] and trace["experiments"] == ["exp1"] and trace["observations"]


def test_budget_exhaustion_report_keeps_real_uncertainty(tmp_path):
    graph = GraphStore(tmp_path / "graph", "cfg")
    incumbent = IncumbentSnapshot(config={}, config_hash="cfg", verified_performance={"throughput_rps":100}, guardrails={},
        workload_fingerprint={}, workload_fingerprint_hash="wf", database_state={}, evidence={}, accepted_experiment_id="baseline")
    report = ReportBuilder(tmp_path / "report").build(incumbent, graph,
        {"stop_reason":"measurement_budget_exhausted", "completed_rounds":0, "unexplained_performance":True})
    assert report["stop_reason"] == "measurement_budget_exhausted"
    assert report["effective_path_count"] == 0 and report["unexplained_performance"] is True
    assert "没有形成" in (tmp_path / "report" / "REPORT.md").read_text()


def test_small_end_to_end_runner_with_fake_database_and_experts(tmp_path, monkeypatch):
    import app.multi_agent.runner as runner_module

    class FakeExecutor:
        def __init__(self, directory, workload, **kwargs):
            self.directory, self.workload = directory, workload
            self.space = FullKnobSpace(); self.measurement_count = 0; self.warmup_count = 0
            self.restart_count = 0; self.apply_count = 0
            self.executor = self
        def capture(self):
            return self.space.current_config(), {"host":"fake", "postgresql_version":"14"}
        def context(self, source_hash, hardware):
            return {"source_config_hash":source_hash, "workload_fingerprint":{"workload":"twitter","sha256":"wf"},
                    "snapshot":{"path_name":"twitter.dump","sha256":"snap"}, "hardware":hardware,
                    "duration":5, "warmup_seconds":0, "terminals":1, "seed":1}
        def measure(self, config, label):
            values = [100, 101, 100]
            value = measurement(values[self.measurement_count], 1)
            self.measurement_count += 1
            return value, {"observed":{"random_page_cost":{"setting":config["random_page_cost"]}}, "session_settings":{}}
        def run_paired(self, incumbent, candidate, round_number):
            self.measurement_count += 4
            controls=[measurement(100,1), measurement(101,1)]
            treatments=[measurement(120,1.2), measurement(121,1.2)]
            return {"order":["control_1","treatment_1","control_2","treatment_2"], "controls":controls,
                    "treatments":treatments, "applies":{}, "observed_candidate_settings":{"random_page_cost":1.1}}
        def apply(self, config, space): return {}
        def restore(self): self.restart_count += 1; return {"restored":True}

    class FakePanel:
        def __init__(self, backend, roles=None): pass
        def propose_parallel(self, payload):
            base = proposal()
            data = base.model_dump(mode="json")
            data["incumbent_config_hash"] = payload["current_incumbent"]["config_hash"]
            data["workload_fingerprint_hash"] = payload["current_incumbent"]["workload_fingerprint_hash"]
            for edge in data["edges"]: edge["context_hash"] = data["incumbent_config_hash"]
            values=[]
            for role, suffix in (("query_execution","q"),("memory_storage","m"),("concurrency_maintenance","c")):
                item=json.loads(json.dumps(data)); item["proposal_id"] += suffix; item["expert"] = role
                # identical semantic proposals can have distinct local graph ids.
                mapping={n["node_id"]:n["node_id"]+suffix for n in item["nodes"]}
                for n in item["nodes"]: n["node_id"] = mapping[n["node_id"]]
                for e in item["edges"]:
                    e["source"],e["target"] = mapping[e["source"]],mapping[e["target"]]
                    old=e["edge_id"]; e["edge_id"] += suffix
                    for pred in item["predictions"]:
                        if pred["edge_id"] == old: pred["edge_id"] = e["edge_id"]
                values.append(ExpertProposal.model_validate(item))
            return values
        def review_parallel(self, *args): return []

    monkeypatch.setattr(runner_module, "ExperimentExecutor", FakeExecutor)
    monkeypatch.setattr(runner_module, "DeepSeekExpertBackend", lambda path: object())
    monkeypatch.setattr(runner_module, "ExpertPanel", FakePanel)
    run = runner_module.MultiAgentRunner(tmp_path, "smoke", "tpcc", "twitter", max_rounds=1,
        max_measurements=7, duration=5, warmup_seconds=0, terminals=1, baseline_repeats=3)
    report = run.run()
    out = tmp_path / "artifacts/multi_agent/smoke/tpcc_to_twitter"
    assert report["completed_rounds"] == 1
    assert (out / "experiment_ledger.jsonl").exists()
    assert (out / "pending_graph.json").exists() and (out / "tested_graph.json").exists()
    assert (out / "CAUSAL_GRAPH.md").exists() and json.loads((out / "final_rollback.json").read_text())["restored"]


def test_incomplete_interaction_is_demoted_without_inventing_factorial_evidence():
    value = {"interaction_factors":["memory may constrain planner"], "interaction_predictions":{"combined":10},
             "cross_domain_requirements":[], "knob_changes":[], "mechanism":"unknown", "nodes":[]}
    DeepSeekExpertBackend._normalize_nonclaims(value)
    assert value["interaction_factors"] == [] and value["interaction_predictions"] == {}
    assert "unverified interaction note" in value["cross_domain_requirements"][0]
    assert {n["kind"] for n in value["nodes"]} == {"mechanism", "objective"}


def test_same_context_historical_baseline_regime_shift_is_rejected(tmp_path):
    from app.multi_agent.runner import MultiAgentRunner
    context={"source_config_hash":"cfg","snapshot":"snap","hardware":"host","workload":"twitter"}
    old=tmp_path/"artifacts/multi_agent/old/tpcc_to_twitter"; old.mkdir(parents=True)
    (old/"manifest.json").write_text(json.dumps({"experiment_id":"old","context":context}))
    (old/"baseline_stability.json").write_text(json.dumps({"median":1000,"values":[990,1000,1010],"stable":True}))
    runner=MultiAgentRunner.__new__(MultiAgentRunner)
    runner.root=tmp_path; runner.directory=tmp_path/"artifacts/multi_agent/new/tpcc_to_twitter"
    runner.experiment_id="new"; runner.source="tpcc"; runner.target="twitter"
    result=runner._historical_baseline_check(stable_hash(context),{"median":100,"values":[99,100,101],"stable":True})
    assert not result["comparable"] and result["maximum_ratio"] == 10
    assert (tmp_path/"artifacts/multi_agent/baseline_registry.jsonl").exists()


def test_observation_only_frontier_collects_evidence_without_peer_review(tmp_path, monkeypatch):
    import app.multi_agent.runner as runner_module

    class FakeExecutor:
        def __init__(self, directory, workload, **kwargs):
            self.directory, self.workload = directory, workload
            self.space = FullKnobSpace(); self.measurement_count = self.warmup_count = 0
            self.restart_count = self.apply_count = 0
            self.duration = kwargs["duration"]; self.warmup_seconds = kwargs["warmup_seconds"]
            self.terminals = kwargs["terminals"]; self.seed = kwargs["seed"]; self.executor = self
        def capture(self): return self.space.current_config(), {"host":"fake"}
        def context(self, source_hash, hardware):
            return {"source_config_hash":source_hash,"workload_fingerprint":{"workload":self.workload,"sha256":"wf"},
                    "snapshot":{"path_name":self.workload+".dump","sha256":"snap"},"hardware":hardware}
        def measure(self, config, label):
            self.measurement_count += 1; self.apply_count += 1
            return measurement(100 + self.measurement_count), {"observed":{},"session_settings":{}}
        def restore(self): return {"restored":True}
        def apply(self, config, space): return {}

    class ObservationPanel:
        review_called = False
        def __init__(self, backend, roles=None): self.round = 0
        def propose_parallel(self, payload):
            self.round += 1; values=[]
            for role, suffix in (("query_execution","q"),("memory_storage","m"),("concurrency_maintenance","c")):
                item = proposal(f"r{self.round}{suffix}", role).model_dump(mode="json")
                item["knob_changes"] = []
                item["incumbent_config_hash"] = payload["current_incumbent"]["config_hash"]
                item["workload_fingerprint_hash"] = payload["current_incumbent"]["workload_fingerprint_hash"]
                for edge in item["edges"]: edge["context_hash"] = item["incumbent_config_hash"]
                values.append(ExpertProposal.model_validate(item))
            return values
        def review_parallel(self, *args):
            ObservationPanel.review_called = True
            raise AssertionError("legacy peer review must not be called")

    monkeypatch.setattr(runner_module, "ExperimentExecutor", FakeExecutor)
    monkeypatch.setattr(runner_module, "DeepSeekExpertBackend", lambda path: object())
    monkeypatch.setattr(runner_module, "ExpertPanel", ObservationPanel)
    runner = runner_module.MultiAgentRunner(tmp_path, "observation", "tpcc", "twitter",
        max_rounds=2, max_measurements=8, duration=5, warmup_seconds=0, terminals=1,
        baseline_repeats=3, max_observation_rounds=1)
    report = runner.run()
    out = tmp_path/"artifacts/multi_agent/observation/tpcc_to_twitter"
    assert report["stop_reason"] == "no_executable_or_discriminating_followup"
    assert report["workload_measurements"] == 4
    assert not ObservationPanel.review_called
    assert len(json.loads((out/"agent_graph.json").read_text())["rounds"]) == 2
    events = (out/"experiment_ledger.jsonl").read_text()
    assert "observation_completed" in events
