import json

import pytest

from app.phase2.llm import MetricNarrative, retrieve_catalog
from app.phase2.runner import TransitionComparison
from app.phase2.space import FullKnobSpace, latin_hypercube


def test_full_space_contains_exact_requested_60_and_decodes():
    space = FullKnobSpace()
    config = space.decode([0.5] * 60)
    assert len(space.names) == len(set(space.names)) == len(config) == 60
    assert space.names[0] == "autovacuum_analyze_scale_factor"
    assert space.names[-1] == "work_mem"
    assert config["min_wal_size"] < config["max_wal_size"]
    assert config["huge_pages"] in {"off", "try"}


def test_full_lhs_is_deterministic_and_stratified():
    first = latin_hypercube(5, 60, 7)
    assert first == latin_hypercube(5, 60, 7)
    assert len(first) == 5 and all(len(row) == 60 for row in first)
    for dimension in range(60):
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
