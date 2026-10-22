from __future__ import annotations

import math

from app.continuous.health_scan import HealthCoveragePlanner
from app.multi_agent.coordinator import Coordinator, ExpertCreditScheduler
from app.multi_agent.models import ExpertProposal, KnobChange
from app.multi_agent.paired_decision import (
    SequentialPairedPolicy, is_three_sigma_outlier, log_gain,
)
from app.phase2.space import FullKnobSpace


def test_sequential_pair_early_stops_a_harmful_first_block():
    policy = SequentialPairedPolicy(delta_min=.03)
    decision = policy.decide([log_gain(100, 90)])
    assert decision.verdict == "early_stopped_worse"
    assert decision.should_continue is False


def test_sequential_pair_accepts_two_consistent_material_gains():
    policy = SequentialPairedPolicy(delta_min=.03)
    first = policy.decide([log_gain(100, 110)])
    final = policy.decide([log_gain(100, 110), log_gain(120, 134)])
    assert first.should_continue is True
    assert final.verdict == "accept"


def test_three_sigma_pair_is_excluded_and_two_outliers_stop():
    history = [99, 100, 101, 100, 99.5]
    assert is_three_sigma_outlier(150, history)
    policy = SequentialPairedPolicy(delta_min=.03)
    decision = policy.decide([], outlier_blocks=2)
    assert decision.verdict == "inconclusive"
    assert decision.valid_blocks == 0


def test_positive_ewma_increases_probability_and_three_losses_pause_with_wakeup():
    roles = ("query_execution", "memory_storage", "concurrency_maintenance")
    scheduler = ExpertCreditScheduler(roles, seed=7)
    for _ in range(3):
        scheduler.update("memory_storage", .08)
    probabilities = scheduler.probabilities(4)
    assert probabilities["memory_storage"] > probabilities["query_execution"]

    losing = ExpertCreditScheduler(roles, seed=7)
    for _ in range(3):
        losing.update("memory_storage", -.05)
    assert losing.credit["memory_storage"].state == "paused_low_yield"
    assert losing.probabilities(4)["memory_storage"] == 0
    assert losing.probabilities(5)["memory_storage"] == .05


def test_semantic_knob_substitution_is_rejected_before_measurement():
    proposal = ExpertProposal.model_construct(
        title="raise default_statistics_target for better estimates",
        configuration_cause="default_statistics_target is too low",
        mechanism="more statistics improve cardinality estimates",
        expected_tps_effect="increase", expected_latency_effect="decrease",
        knob_changes=[KnobChange(
            knob="seq_page_cost", old_value=1.0, new_value=.8,
            numerical_basis="bounded", mechanism_role="planner cost",
            recommended_min=.5, recommended_max=1.0, initial_step=.1)],
    )
    reason = Coordinator.semantic_consistency_error(proposal)
    assert reason and reason.startswith("semantic_mismatch")


def test_health_coverage_always_contains_shared_buffers_and_two_sided_planner_scan():
    space = FullKnobSpace()
    plan = HealthCoveragePlanner(space, memory_bytes=8 * 1024**3).compile(space.current_config())
    assert len(plan) >= 15
    assert len(plan) >= 10
    actions = [(item["knob"], item["new_value"]) for item in plan]
    assert any(name == "shared_buffers" for name, _ in actions)
    random_values = [float(value) for name, value in actions if name == "random_page_cost"]
    assert len(random_values) == 2 and min(random_values) < max(random_values)
