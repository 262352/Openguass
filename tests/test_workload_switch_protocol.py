import json

import pytest

from app.experiment.isolation import OracleReferenceStore, assert_diagnosis_context_isolated, evaluate_episode
from app.multi_agent.graph_protocol import AgentGraph, EvidenceBlackboard, GraphPatch, PatchOperation
from app.phase2.bayesian_baseline import BayesianBaseline
from app.experiment.cli import workloads
from app.experiment.workflow import WorkflowOptions, WorkloadSwitchWorkflow
from app.phase2.space import FullKnobSpace


def patch(base_version=0, *, episode="e1", workload="twitter", reference=None):
    operations = [PatchOperation(operation_id=f"op-{base_version}", kind="add_hypothesis",
        subject_id=f"hypothesis-{base_version}", payload={"mechanism": "cache pressure"},
        references=[reference] if reference else [])]
    return GraphPatch(patch_id=f"patch-{base_version}", episode_id=episode, workload=workload,
        author="memory_storage", base_version=base_version, operations=operations, confidence=.6)


def test_bo_workload_selector_supports_twitter_only():
    assert workloads("twitter") == ("twitter",)
    assert workloads("tpcc,twitter") == ("tpcc", "twitter")
    with pytest.raises(Exception):
        workloads("ycsb")


def test_graph_patch_store_validates_version_references_and_provenance(tmp_path):
    store = EvidenceBlackboard(tmp_path, "e1", "twitter")
    assert store.commit(patch()) == 1
    with pytest.raises(ValueError, match="stale graph patch"):
        store.commit(patch())
    bad = patch(1, reference="missing")
    with pytest.raises(ValueError, match="unknown ids"):
        store.commit(bad)
    snapshot = json.loads((tmp_path / "evidence_graph.json").read_text())
    assert snapshot["version"] == 1
    assert snapshot["patches"][0]["author"] == "memory_storage"
    assert (tmp_path / "graph_patches.jsonl").exists()


def test_agent_graph_is_separate_and_bounded(tmp_path):
    graph = AgentGraph(tmp_path, "e1", ["query_execution", "memory_storage"],
        max_expert_calls_per_round=2, max_messages_per_round=2)
    graph.record_round(1, [{"agent": "query_execution"}, {"agent": "memory_storage"}],
                       [{"from": "query_execution", "to": "coordinator"}])
    value = json.loads((tmp_path / "agent_graph.json").read_text())
    assert value["topology"] == "blackboard" and "patches" not in value
    with pytest.raises(ValueError, match="exceeds"):
        graph.record_round(2, [{}, {}, {}], [])


def test_bo_references_are_workload_specific(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_BENCH_PASSWORD", "unit-test-only")
    source = tmp_path / "start.json"
    source.write_text(json.dumps({"config": {}}))
    tpcc = BayesianBaseline("same", 2, 1, 5, 1, 1, source, root=tmp_path / "bo", workload="tpcc")
    twitter = BayesianBaseline("same", 2, 1, 5, 1, 2, source, root=tmp_path / "bo", workload="twitter")
    assert tpcc.workload == "tpcc" and twitter.workload == "twitter"
    assert tpcc.dir != twitter.dir
    assert tpcc.adapter.name == "tpcc" and twitter.adapter.name == "twitter"


def test_serializable_lock_ranges_include_defaults_and_large_probe_region():
    dimensions = {item.name: item for item in FullKnobSpace().dimensions}
    assert dimensions["max_locks_per_transaction"].low == 64
    assert dimensions["max_pred_locks_per_transaction"].low == 64
    assert dimensions["max_pred_locks_per_transaction"].high >= 4096
    assert dimensions["max_pred_locks_per_transaction"].high == 16384


def test_baseline_retries_shared_lock_exhaustion_with_larger_tables(tmp_path):
    runner = BayesianBaseline.__new__(BayesianBaseline)
    runner.dir = tmp_path
    calls = []

    def measure(config, _space, _label):
        calls.append(config["max_pred_locks_per_transaction"])
        if len(calls) == 1:
            raise RuntimeError("states=['53200']; diagnostic=out of shared memory")
        return ({"throughput_rps": 100.0}, {"restart": True})

    runner.measure = measure
    space = FullKnobSpace()
    start = space.current_config()
    measurement, _applied, effective = runner.measure_baseline(start, space)
    assert calls == [64, 8192]
    assert effective["max_pred_locks_per_transaction"] == 8192
    assert len(measurement["baseline_safety_retries"]) == 1
    assert (tmp_path / "baseline_resource_retries.jsonl").is_file()


def test_reference_retest_rejects_failed_winner_and_falls_back_to_baseline(tmp_path):
    runner = BayesianBaseline.__new__(BayesianBaseline)
    runner.retest_repeats = 2
    runner.dir = tmp_path
    runner.experiment_id = "test"
    runner.workload = "twitter"
    runner.budget = 2
    runner.initial_points = 1
    runner.duration = 120
    runner.warmup_seconds = 30
    runner.terminals = 16
    runner.seed = 1
    calls = []

    def measure(config, _space, label):
        calls.append(label)
        if config["candidate"] == "winner":
            raise RuntimeError("SQLSTATE 53200: out of shared memory")
        return ({"throughput_rps": 100.0, "p95_us": 10.0}, {"restart": False})

    runner.measure = measure
    baseline = {"throughput_rps": 100.0, "config": {"candidate": "baseline"}}
    trials = [{"valid": True, "throughput_rps": 150.0, "evaluation": 1,
               "config": {"candidate": "winner"}}]
    reference = runner._build_reference(baseline, trials, object())
    assert reference["selection"] == {"kind": "baseline", "evaluation": 0}
    assert reference["retest_attempts"] == 3
    assert len(reference["rejected_reference_candidates"]) == 1
    assert "53200" in (tmp_path / "reference_candidate_rejections.jsonl").read_text()


def test_target_bo_oracle_is_rejected_from_online_context(tmp_path):
    root = tmp_path / "oracle"
    for workload, tps in (("tpcc", 111.0), ("twitter", 987654.0)):
        path = root / workload / "reference.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"workload": workload, "config": {"work_mem": int(tps)},
            "metrics": {"throughput_rps_min": tps}}))
    store = OracleReferenceStore(root)
    source = store.load("tpcc")
    target = store.load("twitter")
    visible = {"source_workload": "tpcc", "target_workload": "twitter",
               "episode_start_config": source["config"], "target_initial_tps": 100.0}
    assert_diagnosis_context_isolated(visible)
    with pytest.raises(ValueError, match="oracle field"):
        assert_diagnosis_context_isolated({**visible, "target_reference_tps": target["metrics"]["throughput_rps_min"]})
    serialized = json.dumps(visible)
    assert "987654" not in serialized


def test_oracle_evaluation_keeps_episode_and_reference_metrics_separate():
    report = {"episode_initial_tps": 100.0,
              "incumbent": {"verified_performance": {"throughput_rps": 120.0}}}
    reference = {"workload": "twitter", "metrics": {"throughput_rps_min": 150.0}}
    result = evaluate_episode(report, reference)
    assert result["absolute_improvement"] == 20.0
    assert result["initial_gap_to_bo_reference"] == 50.0
    assert result["final_gap_to_bo_reference"] == 30.0
    assert result["gap_closed_percent"] == 40.0


def test_two_direction_workflow_passes_only_source_reference_to_runner(tmp_path, monkeypatch):
    options = WorkflowOptions(suite_id="suite", directions=(("tpcc", "twitter"), ("twitter", "tpcc")),
                              repetitions=1, diagnosis_rounds=1, diagnosis_measurements=4)
    workflow = WorkloadSwitchWorkflow(tmp_path, options)
    for workload, value in (("tpcc", 111), ("twitter", 999)):
        target = workflow.oracle_root / workload / "reference.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"workload": workload, "config": {"work_mem": value},
            "metrics": {"throughput_rps_min": float(value)}}))
    calls = []

    class FakeRunner:
        def __init__(self, root, episode_id, source, target, **kwargs):
            calls.append({"source": source, "target": target, "source_config": kwargs["source_config"],
                          "terminals": kwargs["terminals"],
                          "source_profile_terminals": kwargs["source_profile_terminals"]})
            self.out = kwargs["output_root"] / f"{source}_to_{target}"
            self.out.mkdir(parents=True)
            visible = {"source_workload": source, "target_workload": target,
                       "episode_start_config": {"work_mem": 111 if source == "tpcc" else 999},
                       "online_context_only": True}
            (self.out / "diagnosis_visible_context.json").write_text(json.dumps(visible))
        def run(self):
            return {"episode_initial_tps": 100.0,
                    "incumbent": {"verified_performance": {"throughput_rps": 110.0}},
                    "stop_reason": "budget_exhausted"}

    monkeypatch.setattr("app.experiment.workflow.MultiAgentRunner", FakeRunner)
    result = workflow.run_diagnosis()
    assert len(result["episodes"]) == 2
    assert calls[0]["source_config"] == workflow.store.path("tpcc")
    assert calls[1]["source_config"] == workflow.store.path("twitter")
    assert calls[0]["terminals"] == 5 and calls[0]["source_profile_terminals"] == 3
    assert calls[1]["terminals"] == 3 and calls[1]["source_profile_terminals"] == 5
    first_visible = (workflow.directory / "episodes/repeat_01/tpcc_to_twitter/diagnosis_visible_context.json").read_text()
    assert "999" not in first_visible
    evaluations = sorted((workflow.directory / "oracle/evaluations").glob("*.json"))
    assert len(evaluations) == 2 and "bo_reference_verified_tps" in evaluations[0].read_text()
    assert (workflow.directory / "FINAL_COMPARISON.json").is_file()
    assert "tpcc_to_twitter" in (workflow.directory / "FINAL_COMPARISON.md").read_text()


def test_bo_reference_standard_records_twitter_tps_and_config(tmp_path):
    workflow = WorkloadSwitchWorkflow(tmp_path, WorkflowOptions(suite_id="suite"))
    for workload, value in (("tpcc", 111), ("twitter", 999)):
        target = workflow.store.path(workload)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"workload": workload, "config": {"work_mem": value},
            "selection": {"kind": "trial", "evaluation": 7},
            "search_budget": {"total": 100, "lhs": 10, "gp_ei": 90},
            "metrics": {"throughput_rps_min": float(value),
                        "throughput_rps_median": float(value + 1)}}))
    workflow._write_reference_standard()
    standard = json.loads((workflow.directory / "REFERENCE_STANDARD.json").read_text())
    assert standard["references"]["twitter"]["verified_tps_min"] == 999
    assert standard["references"]["twitter"]["selected_config"] == {"work_mem": 999}
    assert "Twitter verified TPS" in (workflow.directory / "REFERENCE_STANDARD.md").read_text()
