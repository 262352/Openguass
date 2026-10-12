import json

from app.chain.agent import AgentDecision, SingleAgentChainDiagnoser
from app.chain.loop import ChainRunOptions, ChainValidationLoop


class FakeExecutor:
    def __init__(self, directory):
        self.directory = directory
    def capture(self):
        return {}
    def apply(self, config, space):
        dimensions = {item.name: item for item in space.dimensions}
        session = {name: str(value) for name, value in config.items() if dimensions[name].execution_class == "session"}
        observed = {name: {"setting": str(value), "pending_restart": "f"} for name, value in config.items()
                    if dimensions[name].execution_class != "session"}
        return {"applied": True, "restart": False, "session_settings": session, "observed": observed}
    def restore(self):
        return {"restored": True}


class FakeAdapter:
    target_values = iter([100.0, 101.0, 100.0, 110.0, 100.0, 109.0])
    def __init__(self, name):
        self.name = name
    def restore_snapshot(self):
        return {"restored": True}
    def warmup(self, *args, **kwargs):
        return None
    def run(self, *args, **kwargs):
        value = 80.0 if self.name == "tpcc" else next(self.target_values)
        return {"throughput_rps": value, "p95_us": 1000.0}
    def parse_result(self, raw):
        return raw
    def fingerprint(self):
        return {"workload": self.name, "sha256": self.name}


class FakeSampler:
    def __init__(self, database):
        pass
    def start(self):
        return self
    def stop(self):
        return []


class FakeAgent:
    _metric_values = staticmethod(SingleAgentChainDiagnoser._metric_values)
    def __init__(self):
        self.calls = 0
    def decide(self, *args, **kwargs):
        self.calls += 1
        if self.calls > 1:
            return AgentDecision(hypothesis_action="stop", stop_reason="no_actionable_hypothesis")
        return AgentDecision(hypothesis_action="propose_next_bottleneck", hypothesis="lock waits limit TPS",
            mechanism="lock contention", evidence_metric_id="locks_waiting", evidence_expected_direction="decrease",
            knob="work_mem", proposed_value=8192, knob_to_evidence_reason="reduce spill and lock duration",
            evidence_to_tps_reason="shorter waits increase TPS", confidence=.6, risk="low",
            why_exceeds_incumbent="paired repeats will test it", mechanism_measurement="locks_waiting p95",
            activation_check="SHOW work_mem", resource_guardrails=["p95 latency"],
            alternative_explanations=["cache state"], group_name="per_session_memory",
            coupling_rationale="work and temporary buffers jointly control spill duration",
            ranked_candidate_groups=["per_session_memory"], expected_tps_impact_percent=8,
            knob_group=[
                {"knob":"work_mem", "proposed_value":8192, "numerical_basis":"double current memory",
                 "mechanism_role":"reduce sort spill", "activation_check":"SHOW work_mem"},
                {"knob":"temp_buffers", "proposed_value":2048, "numerical_basis":"increase temporary buffers",
                 "mechanism_role":"reduce temporary I/O", "activation_check":"SHOW temp_buffers"},
            ])
    def materialize(self, *args, **kwargs):
        return SingleAgentChainDiagnoser.materialize(self, *args, **kwargs)


def test_short_end_to_end_promotes_only_retested_candidate(tmp_path, monkeypatch):
    telemetry = {"sample_count": 2, "rates_per_second": {"rollback_ratio": 0.0},
                 "gauges": {"locks_waiting": {"median": 1, "p95": 1, "max": 1}}, "anomalies": []}
    monkeypatch.setattr("app.chain.loop.DiagnosticSampler", FakeSampler)
    monkeypatch.setattr("app.chain.loop.summarize", lambda samples: telemetry)
    monkeypatch.setattr("app.chain.loop.collect_plan_evidence", lambda workload: {"status": "observed", "uses_index": True})
    FakeAdapter.target_values = iter([100.0, 101.0, 100.0, 110.0, 100.0, 109.0])
    report = ChainValidationLoop("e2e", "tpcc", "twitter",
        ChainRunOptions(mode="quick", duration=5, warmup=0, max_experiments=2, min_tps_percent=5,
                        min_screening_groups=1),
        root=tmp_path, agent=FakeAgent(), adapter_factory=FakeAdapter,
        executor_factory=FakeExecutor).run()
    assert report["failure"] is None
    assert report["experiments"] == 1
    assert report["search_protocol"]["tested_groups"] == ["per_session_memory"]
    effects = (tmp_path / "artifacts/v3_v4/e2e/tpcc_to_twitter/knob_group_effects.csv").read_text()
    assert effects.count("per_session_memory") == 1
    assert "work_mem" in effects and "temp_buffers" in effects
    assert report["current_incumbent"]["verified_performance"]["throughput_rps"] == 109.0
    result = json.loads((tmp_path / "artifacts/v3_v4/e2e/tpcc_to_twitter/experiments/experiment_001_01/result.json").read_text())
    assert result["incumbent_decision"]["accepted"] is True
    assert result["retest_treatment"]["throughput_rps"] == 109.0
    assert set(result["plan"]["changed_knobs"]) == {"work_mem", "temp_buffers"}
