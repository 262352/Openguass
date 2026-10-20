import json
import subprocess

import pytest

from app.phase2.llm import MetricNarrative, retrieve_catalog
from app.phase2.executor import PostgreSQLKnobExecutor
from app.phase2.runner import TransitionComparison
from app.phase2.space import FullKnobSpace, latin_hypercube


def test_full_space_contains_exact_catalog_and_decodes():
    space = FullKnobSpace()
    config = space.decode([0.5] * len(space))
    assert len(space.names) == len(set(space.names)) == len(config) == 62
    assert {"max_pred_locks_per_transaction", "max_pred_locks_per_relation",
            "max_pred_locks_per_page"} <= set(space.names)
    assert space.names[0] == "autovacuum_analyze_scale_factor"
    assert space.names[-1] == "work_mem"
    assert config["min_wal_size"] < config["max_wal_size"]
    assert config["huge_pages"] in {"off", "try"}


def test_full_lhs_is_deterministic_and_stratified():
    dimensions = len(FullKnobSpace())
    first = latin_hypercube(5, dimensions, 7)
    assert first == latin_hypercube(5, dimensions, 7)
    assert len(first) == 5 and all(len(row) == dimensions for row in first)
    for dimension in range(dimensions):
        assert sorted(int(row[dimension] * 5) for row in first) == list(range(5))


def test_catalog_retrieval_uses_diagnosed_metric_terms():
    narrative = MetricNarrative(
        observations=["temporary bytes increased"], workload_semantic_changes=[],
        bottleneck_hypotheses=[{"name": "temp spill and work memory pressure", "evidence_ids": ["target"], "confidence": 0.8}],
        retrieval_terms=["temp", "work_mem"],
    )
    names = [item["name"] for item in retrieve_catalog(narrative)]
    assert "work_mem" in names or "temp_buffers" in names


def test_tps_only_summary_and_thresholds():
    rows = [
        {"evaluation": 1, "valid": True, "tps": 110.0, "elapsed_seconds": 10.0, "apply": {"restart": False}},
        {"evaluation": 2, "valid": True, "tps": 125.0, "elapsed_seconds": 20.0, "apply": {"restart": True, "restart_seconds": 2.0}},
    ]
    summary = TransitionComparison._method_summary(rows, 100.0, 125.0)
    assert summary["best_tps"] == 125.0
    assert summary["improvement_pct"] == pytest.approx(25.0)
    assert summary["time_to_95pct_posthoc_best"]["evaluation"] == 2
    assert summary["restart_count"] == 1


def test_checkpoint_uses_its_own_long_timeout(tmp_path, monkeypatch):
    executor = PostgreSQLKnobExecutor(tmp_path, database="tpcc")
    calls = []

    def fake_psql(sql, *, timeout=None):
        calls.append((sql, timeout))
        return ""

    monkeypatch.setattr(executor, "_psql", fake_psql)
    monkeypatch.setattr(executor, "_bgwriter_snapshot", lambda: {"buffers_backend_fsync": 0.0})
    monkeypatch.setattr(executor, "_wait_for_storage_quiet", lambda: {"passed": True})
    monkeypatch.setenv("CHECKPOINT_TIMEOUT_SECONDS", "420")
    monkeypatch.setenv("SNAPSHOT_SETTLE_SECONDS", "0")
    result = executor.quiesce()
    assert calls == [("checkpoint", 420.0)]
    assert result["checkpoint_timeout_seconds"] == 420.0


def test_psql_timeout_reports_operation_and_configured_limit(tmp_path, monkeypatch):
    executor = PostgreSQLKnobExecutor(tmp_path, database="tpcc")

    def expire(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", expire)
    with pytest.raises(TimeoutError, match="after 75s: checkpoint"):
        executor._psql("checkpoint", timeout=75)
