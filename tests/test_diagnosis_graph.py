import json

import pytest
from pydantic import ValidationError

from app.chain.models import EvidenceGraph, GraphEdge, GraphNode
from app.chain.contracts import CounterfactualContract, InterventionGroup, KnobAction, Prediction
from app.chain.session import SequentialGraphSession
from app.chain.validator import validate_contract
from app.chain.visualize import render_graph


def graph_fixture():
    nodes = [
        GraphNode(node_id="context:switch", type="workload_context", name="TPC-C → Twitter", description="switch", status="observed"),
        GraphNode(node_id="mechanism:lock", type="mechanism", name="锁排队", description="candidate mechanism", status="hypothesized"),
        GraphNode(
            node_id="evidence:locks", type="evidence_metric", name="locks_waiting", description="measured lock waits",
            source_ids=["telemetry.json"], status="observed", metric_id="locks_waiting",
            metric_source="pg_locks", sampling_window={"seconds": 30}, baseline_value=0,
            current_value=2, aggregation="max", direction="increased", unit="count",
        ),
        GraphNode(node_id="target:tps", type="target_metric", name="Twitter TPS", description="objective", status="observed"),
    ]
    edges = [
        GraphEdge(edge_id="edge:a", cause_node_id="context:switch", effect_node_id="mechanism:lock", relation="triggers", mechanism_text="x", confidence=.6),
        GraphEdge(edge_id="edge:b", cause_node_id="mechanism:lock", effect_node_id="evidence:locks", relation="raises", mechanism_text="x", confidence=.7, status="supported"),
        GraphEdge(edge_id="edge:c", cause_node_id="evidence:locks", effect_node_id="target:tps", relation="reduces", mechanism_text="x", confidence=.5, status="refuted"),
    ]
    return EvidenceGraph(graph_id="test", graph_kind="combined", title="诊断图", nodes=nodes, edges=edges)


def test_evidence_graph_validates_references_and_real_metric_fields():
    graph = graph_fixture()
    assert len(graph.nodes) == 4
    with pytest.raises(ValidationError):
        GraphNode(node_id="evidence:bad", type="evidence_metric", name="invented", description="bad", status="observed")
    with pytest.raises(ValidationError):
        EvidenceGraph(
            graph_id="bad", graph_kind="candidate", title="bad", nodes=graph.nodes,
            edges=[GraphEdge(edge_id="edge:bad", cause_node_id="missing", effect_node_id="target:tps", relation="x", mechanism_text="x", confidence=.5)],
        )


def test_effective_graph_contains_only_supported_edges():
    effective = graph_fixture().effective()
    assert effective.graph_kind == "effective"
    # A supported fragment disconnected from an intervention-to-target path is
    # not promoted to the effective causal chain.
    assert [edge.edge_id for edge in effective.edges] == []
    assert effective.nodes == []


def test_visual_report_contains_hypothesis_evidence_and_status_colors(tmp_path):
    paths = render_graph(graph_fixture(), tmp_path)
    svg = (tmp_path / "diagnosis_graph.svg").read_text(encoding="utf-8")
    markdown = (tmp_path / "diagnosis_graph.md").read_text(encoding="utf-8")
    raw = json.loads((tmp_path / "diagnosis_graph.json").read_text(encoding="utf-8"))
    assert set(paths) == {"json", "markdown", "svg"}
    assert "锁排队" in svg and "locks_waiting" in svg
    assert "#16a34a" in svg and "#dc2626" in svg
    assert "```mermaid" in markdown and "candidate" in markdown
    assert raw["schema_version"] == "andromeda.evidence-graph/v1"


def contract_fixture():
    action = KnobAction(
        knob="work_mem", current_value=4096, proposed_value=8192,
        evidence_ids=("evidence:locks",), knowledge_ids=("pg14:work_mem",),
        mechanism="reduce spill", restart_required=False,
    )
    evidence = Prediction(
        metric_id="locks_waiting", expected_direction="decrease", minimum_change=1,
        unit="count", target_edge_id="edge:b", comparison_method="absolute_delta",
    )
    target = Prediction(
        metric_id="target_workload_throughput_rps", expected_direction="increase", minimum_change=5,
        unit="percent", target_edge_id="edge:c", comparison_method="relative_percent",
    )
    return CounterfactualContract(
        contract_id="contract:1", hypothesis_id="hypothesis:1", target_edge_ids=("edge:b", "edge:c"),
        intervention_group=InterventionGroup(group_id="group:1", group_type="independent", knob_actions=(action,)),
        baseline_config_hash="abc", control_variables={
            "target_workload":"twitter", "snapshot_id":"twitter.dump", "terminals":2,
            "duration":30, "seed":7, "warmup_seconds":5, "postgresql_version":"14",
            "resource_scope":"test", "transaction_mix":"fixed",
        }, direct_predictions=(evidence,), intermediate_predictions=(), target_prediction=target,
        thresholds={"evidence":1, "tps_percent":5}, warmup_seconds=5, measurement_seconds=30,
        seed=7, risk_assessment="low", restart_required=False, rollback_plan="restore",
        abort_conditions=("database unhealthy",),
    )


def test_counterfactual_contract_is_frozen_and_validates_each_edge():
    contract = contract_fixture()
    with pytest.raises(ValidationError):
        contract.measurement_seconds = 60
    result = validate_contract(
        contract,
        {"throughput_rps":100, "telemetry":{"gauges":{"locks_waiting":{"p95":2}}}},
        {"throughput_rps":110, "telemetry":{"gauges":{"locks_waiting":{"p95":0}}}},
    )
    assert result["mechanism_path_supported"] is True
    assert {item["status"] for item in result["edge_updates"].values()} == {"supported"}


def test_validator_separates_evidence_and_tps_edges():
    result = validate_contract(
        contract_fixture(),
        {"throughput_rps":100, "telemetry":{"gauges":{"locks_waiting":{"p95":2}}}},
        {"throughput_rps":101, "telemetry":{"gauges":{"locks_waiting":{"p95":0}}}},
    )
    assert result["edge_updates"]["edge:b"]["status"] == "supported"
    assert result["edge_updates"]["edge:c"]["status"] == "refuted"
    assert result["mechanism_path_supported"] is False


def test_sequential_session_rejects_next_hypothesis_until_active_chain_resolves(tmp_path):
    graph = graph_fixture()
    graph.edges = []
    session = SequentialGraphSession(tmp_path, "sequential", graph)
    first_edges = [
        GraphEdge(edge_id="edge:step1a", cause_node_id="context:switch", effect_node_id="mechanism:lock", relation="trigger", mechanism_text="x", confidence=.5),
        GraphEdge(edge_id="edge:step1b", cause_node_id="mechanism:lock", effect_node_id="evidence:locks", relation="measure", mechanism_text="x", confidence=.5),
        GraphEdge(edge_id="edge:step1c", cause_node_id="evidence:locks", effect_node_id="target:tps", relation="affect", mechanism_text="x", confidence=.5),
    ]
    session.propose_step("step_001", [], first_edges, "contract:step1")
    with pytest.raises(RuntimeError, match="finish the active"):
        session.propose_step("step_002", [], [], "contract:step2")
    session.resolve_step("step_001", {
        edge.edge_id:{"status":"supported", "reason":"directional threshold reached"}
        for edge in first_edges
    }, "experiment:1")
    assert not session.active_edges
    session.propose_step("step_002", [], [], "contract:step2")
    assert (tmp_path / "steps" / "step_001.svg").is_file()
    assert "step_resolved" in (tmp_path / "graph_events.jsonl").read_text()
