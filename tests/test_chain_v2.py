import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.chain.agent import AgentDecision, SingleAgentChainDiagnoser, assert_no_online_leakage
from app.chain.analyzer import BottleneckFrontierAnalyzer
from app.chain.contracts import CounterfactualContract, InterventionGroup, KnobAction, Prediction
from app.chain.incumbent import IncumbentPolicy, IncumbentState, verified_performance
from app.chain.models import EvidenceGraph, GraphEdge, GraphNode, HypothesisRecord, ValidationFrontier
from app.chain.planner import ExperimentPlanner
from app.chain.session import SequentialGraphSession
from app.chain.source_config import load_source_config
from app.chain.validator import validate_contract
from app.chain.loop import ChainRunOptions, ChainValidationLoop
from app.phase2.space import FullKnobSpace


def action(name, current=1, proposed=2):
    return KnobAction(knob=name, current_value=current, proposed_value=proposed,
                      evidence_ids=("e",), knowledge_ids=(f"pg14:{name}",), mechanism="test", restart_required=False)


def contract(group):
    direct = Prediction(metric_id="locks_waiting", expected_direction="decrease", minimum_change=5,
                        unit="percent", target_edge_id="edge:direct", comparison_method="relative_percent")
    target = Prediction(metric_id="target_workload_throughput_rps", expected_direction="increase", minimum_change=5,
                        unit="percent", target_edge_id="edge:target", comparison_method="relative_percent")
    return CounterfactualContract(contract_id="c", hypothesis_id="h", frontier_id="f", baseline_state_id="s",
        target_edge_ids=("edge:direct", "edge:target"), intervention_group=group, baseline_config_hash="hash",
        control_variables={"target_workload":"twitter","snapshot_id":"twitter.dump","terminals":2,"duration":30,
                           "seed":1,"warmup_seconds":5,"postgresql_version":"14","resource_scope":"host","transaction_mix":"fixed"},
        direct_predictions=(direct,), intermediate_predictions=(), target_prediction=target,
        thresholds={"evidence":5,"tps":5}, warmup_seconds=5, measurement_seconds=30, seed=1,
        risk="low", restart_required=False, rollback_plan="restore", abort_conditions=("unhealthy",))


def group_fields(name="planner", knob="random_page_cost", value=1.1):
    return {
        "group_name": name,
        "coupling_rationale": "the settings jointly control one measured mechanism",
        "ranked_candidate_groups": [name],
        "knob_group": [{"knob": knob, "proposed_value": value,
                        "numerical_basis": "catalog-safe mechanism-specific value",
                        "mechanism_role": "controls the diagnosed mechanism",
                        "activation_check": f"SHOW {knob}"}],
    }


def test_planner_has_no_three_knob_limit_and_splits_independent():
    actions = tuple(action(f"k{i}") for i in range(5))
    plans = ExperimentPlanner().plan(contract(InterventionGroup(group_id="g", group_type="independent", knob_actions=actions)), {}, 5)
    assert len(plans) == 5
    assert all(len(p.changed_knobs) == 1 for p in plans)


def test_coupled_and_interaction_plans_are_identifiable():
    actions = (action("a"), action("b"))
    with pytest.raises(ValidationError):
        InterventionGroup(group_id="g", group_type="coupled", knob_actions=actions)
    coupled = InterventionGroup(group_id="g", group_type="coupled", knob_actions=actions, dependency_knowledge_ids=("dep",))
    assert len(ExperimentPlanner().plan(contract(coupled), {}, 1)) == 1
    interaction = InterventionGroup(group_id="g2", group_type="interaction", knob_actions=actions)
    assert [p.label for p in ExperimentPlanner().plan(contract(interaction), {}, 3)] == ["main:a", "main:b", "interaction:all"]
    with pytest.raises(RuntimeError, match="budget"):
        ExperimentPlanner().plan(contract(interaction), {}, 2)


def test_partial_frontier_resolution_keeps_other_candidate_edges(tmp_path):
    nodes = [GraphNode(node_id=x, type="mechanism", name=x, description=x, status="hypothesized") for x in ("a","b","c")]
    edges = [GraphEdge(edge_id="e1", cause_node_id="a", effect_node_id="b", relation="x", mechanism_text="x", confidence=.5),
             GraphEdge(edge_id="e2", cause_node_id="b", effect_node_id="c", relation="x", mechanism_text="x", confidence=.5)]
    session = SequentialGraphSession(tmp_path, "x", EvidenceGraph(graph_id="g", graph_kind="candidate", title="x", nodes=nodes, edges=[]))
    frontier = ValidationFrontier(frontier_id="f", hypothesis_id="h", selected_edge_ids=["e1"], reason_for_selection="local")
    session.propose_step("s1", [], edges, "c1", frontier=frontier)
    session.resolve_step("s1", {"e1":{"status":"supported","reason":"passed"}}, "x1", frontier)
    assert {e.edge_id for e in session.active_edges} == {"e2"}
    session.propose_step("s2", [], [], "c2")


def test_hypothesis_schema_keeps_revision_and_parent_history_fields():
    h = HypothesisRecord(hypothesis_id="h2", parent_hypothesis_id="h1", revision=2, title="locks",
                         statement="lock waits", status="revising", initial_state_id="s0", current_state_id="s1",
                         candidate_edge_ids=["e1"], supported_edge_ids=["e0"], revision_reason="counterfactual conflict")
    assert h.parent_hypothesis_id == "h1" and h.revision == 2 and h.supported_edge_ids == ["e0"]


def test_incumbent_requires_tps_evidence_health_and_application():
    validation = {"mechanism_path_supported": True}
    incumbent = IncumbentState({}, "h", verified_performance([{"throughput_rps": 100}]), "initial", "now", {})
    accepted = IncumbentPolicy().decide({"throughput_rps":100}, {"throughput_rps":107,"healthy":True}, validation,
        {"restart":False}, 5, incumbent=incumbent, retest_control={"throughput_rps":101},
        retest_treatment={"throughput_rps":106,"healthy":True})
    assert accepted["accepted"]
    unexplained = IncumbentPolicy().decide({"throughput_rps":100}, {"throughput_rps":107,"healthy":True},
        {"mechanism_path_supported":False}, {"restart":False}, 5, incumbent=incumbent,
        retest_control={"throughput_rps":100}, retest_treatment={"throughput_rps":106,"healthy":True})
    assert unexplained["accepted"] and unexplained["unexplained_performance"]
    upstream = IncumbentPolicy().decide({"throughput_rps":100}, {"throughput_rps":101,"healthy":True}, validation,
        {"restart":False}, 5, incumbent=incumbent, retest_control={"throughput_rps":100},
        retest_treatment={"throughput_rps":101,"healthy":True})
    assert not upstream["accepted"] and upstream["rollback_required"]


def test_single_lucky_run_never_replaces_incumbent():
    incumbent = IncumbentState({}, "h", verified_performance([{"throughput_rps": 100}]), "initial", "now", {})
    result = IncumbentPolicy().decide({"throughput_rps": 100}, {"throughput_rps": 150},
        {"mechanism_verdict": "supported"}, {}, 5, incumbent=incumbent,
        retest_control={"throughput_rps": 101}, retest_treatment={"throughput_rps": 99})
    assert not result["accepted"]
    assert result["candidate_verified_performance"]["throughput_rps"] == 99


def test_local_gain_below_incumbent_is_not_accepted():
    incumbent = IncumbentState({}, "h", verified_performance([{"throughput_rps": 120}]), "old", "now", {})
    result = IncumbentPolicy().decide({"throughput_rps": 90}, {"throughput_rps": 100},
        {"mechanism_verdict": "supported"}, {}, 5, incumbent=incumbent,
        retest_control={"throughput_rps": 91}, retest_treatment={"throughput_rps": 101})
    assert not result["accepted"] and result["candidate_vs_incumbent_percent"] < 0


def test_guardrail_blocks_tps_gain_and_mechanism_is_independent():
    incumbent = IncumbentState({}, "h", verified_performance([{"throughput_rps": 100}]), "old", "now", {})
    result = IncumbentPolicy().decide({"throughput_rps":100,"p95_us":10}, {"throughput_rps":110,"p95_us":30},
        {"mechanism_verdict":"refuted"}, {}, 5, incumbent=incumbent,
        retest_control={"throughput_rps":100,"p95_us":10}, retest_treatment={"throughput_rps":109,"p95_us":30})
    assert result["performance_verdict"] == "invalid" and result["mechanism_verdict"] == "refuted"


def test_bottleneck_analyzer_missing_unknown_and_directional_labels():
    a = BottleneckFrontierAnalyzer()
    assert a.classify("missing", "decrease", {}, {}, 5)["classification"] == "missing"
    control={"telemetry":{"gauges":{"locks_waiting":{"p95":10}}}}
    treatment={"telemetry":{"gauges":{"locks_waiting":{"p95":0}}}}
    assert a.classify("locks_waiting", "decrease", control, treatment, 5)["classification"] == "resolved"
    assert a.classify("locks_waiting", None, control, treatment, 5)["classification"] == "unknown"


def test_online_leakage_guard_rejects_historical_target_fields():
    for field in ("TPS_ref", "reference_config", "initial_gap", "coverage", "GP_label"):
        with pytest.raises(ValueError):
            assert_no_online_leakage({field: 1})
    assert_no_online_leakage({"current_target_measurement":{"throughput_rps":12}})


def test_contract_thresholds_are_frozen():
    value = contract(InterventionGroup(group_id="g", group_type="independent", knob_actions=(action("work_mem"),)))
    with pytest.raises(ValidationError):
        value.thresholds = {"tps":0}


def test_common_qualitative_model_confidence_is_normalized():
    value = AgentDecision(
        hypothesis_action="propose_next_bottleneck", hypothesis="h", mechanism="m",
        evidence_metric_id="xact_commit", knob="jit", proposed_value="off",
        knob_to_evidence_reason="direct test", evidence_to_tps_reason="commit rate limits TPS",
        confidence="medium", risk="low", why_exceeds_incumbent="repeatable gain", mechanism_measurement="normalized rate",
        activation_check="show jit", resource_guardrails=["latency"], alternative_explanations=["cache"],
        expected_tps_impact_percent=5, **group_fields("jit-execution", "jit", "off"),
    )
    assert value.confidence == 0.5


def test_non_executable_audit_fields_accept_model_json_shapes():
    value = AgentDecision(
        hypothesis_action="propose_next_bottleneck", hypothesis="h", mechanism="m",
        evidence_metric_id="seq_scan", evidence_expected_direction="decrease", knob="random_page_cost",
        proposed_value=1.1, knob_to_evidence_reason="planner test", evidence_to_tps_reason="scan cost",
        confidence=.4, risk="low", stop_decision=False, stop_reason="",
        why_exceeds_incumbent="lower plan cost", mechanism_measurement="EXPLAIN",
        activation_check="show random_page_cost", resource_guardrails=["latency"], alternative_explanations=["cache"],
        expected_tps_impact_percent=5, **group_fields(),
        graph_patch={"nodes": []}, frontier=["seq_scan"], next_contract="audit summary",
    )
    assert value.stop_decision is False and value.frontier == ["seq_scan"]


def test_model_scalar_list_fields_are_normalized():
    value = AgentDecision(
        hypothesis_action="propose_next_bottleneck", hypothesis="h", mechanism="m",
        evidence_metric_id="blks_hit_per_txn", knob="random_page_cost", proposed_value=1.1,
        knob_to_evidence_reason="planner path", evidence_to_tps_reason="less work per txn",
        confidence=.4, risk="low", why_exceeds_incumbent="less CPU per transaction",
        mechanism_measurement="normalized buffer hits", activation_check="show random_page_cost",
        resource_guardrails="p95 must not regress", alternative_explanations="cache warming",
        expected_tps_impact_percent=5, **group_fields(),
    )
    assert value.resource_guardrails == ["p95 must not regress"]
    assert value.alternative_explanations == ["cache warming"]


def test_selected_group_must_lead_impact_ranking():
    with pytest.raises(ValidationError, match="first in the expected-impact ranking"):
        AgentDecision(
            hypothesis_action="propose_next_bottleneck", hypothesis="h", mechanism="m",
            evidence_metric_id="blks_hit_per_txn", knob="random_page_cost", proposed_value=1.1,
            knob_to_evidence_reason="planner path", evidence_to_tps_reason="less work",
            confidence=.4, risk="low", why_exceeds_incumbent="less CPU",
            mechanism_measurement="normalized buffer hits", activation_check="show setting",
            resource_guardrails=["latency"], alternative_explanations=["cache"],
            group_name="planner", coupling_rationale="planner mechanism",
            ranked_candidate_groups=["memory", "planner"],
            knob_group=group_fields()["knob_group"], expected_tps_impact_percent=5,
        )


def test_statistics_mechanism_requires_analyze_event():
    value = contract(InterventionGroup(group_id="g", group_type="independent",
        knob_actions=(action("default_statistics_target"),)))
    control = {"throughput_rps": 100, "telemetry": {"gauges": {"locks_waiting": {"p95": 10}}, "rates_per_second": {}}}
    treatment = {"throughput_rps": 110, "telemetry": {"gauges": {"locks_waiting": {"p95": 0}}, "rates_per_second": {"analyze_count": 0}}}
    result = validate_contract(value, control, treatment)
    assert result["mechanism_verdict"] == "inconclusive"
    assert result["activation_check"]["status"] == "inconclusive"


def test_source_config_is_loaded_and_audited(tmp_path):
    directory = tmp_path / "configs"
    directory.mkdir()
    (directory / "tpcc.json").write_text(json.dumps({"config": {"work_mem": 2048}}))
    value, audit = load_source_config(tmp_path, "tpcc", {"work_mem": 1024}, str(directory))
    assert value["work_mem"] == 2048 and audit["explicit"] is True


def test_raw_idx_scan_rate_is_not_mechanism_proof():
    value = contract(InterventionGroup(group_id="g", group_type="independent", knob_actions=(action("work_mem"),)))
    value_data = value.model_dump()
    value_data["direct_predictions"][0]["metric_id"] = "idx_scan"
    value = CounterfactualContract.model_validate(value_data)
    control = {"throughput_rps": 100, "telemetry": {"rates_per_second": {"idx_scan": 100}}}
    treatment = {"throughput_rps": 110, "telemetry": {"rates_per_second": {"idx_scan": 110}}}
    result = validate_contract(value, control, treatment)
    assert result["mechanism_verdict"] == "inconclusive"
    assert result["edge_updates"]["edge:direct"]["status"] == "inconclusive"


def test_patience_alone_does_not_stop_independent_bottlenecks(tmp_path):
    loop = ChainValidationLoop("x", "tpcc", "twitter", ChainRunOptions(mode="quick", duration=5, warmup=0), root=tmp_path)
    loop.no_improvement = 999
    assert loop._budget_stop() is None
