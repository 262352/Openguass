from __future__ import annotations

import pytest

from app.continuous.knowledge_retrieval import retrieve_diagnostic_bundles
from app.multi_agent.experts import DeepSeekExpertBackend
from app.phase2.space import FullKnobSpace

from test_multi_agent import proposal


def test_low_cache_diagnosis_retrieves_complete_actionable_knob_bundle():
    space = FullKnobSpace()
    result = retrieve_diagnostic_bundles(
        ["low_buffer_hit_ratio"], {"client": {}, "telemetry": {}}, space, {})
    bundle = next(item for item in result["bundles"]
                  if item["bundle_id"] == "buffer_cache_capacity")
    assert set(bundle["required_knobs"]) == {
        "shared_buffers", "effective_cache_size", "effective_io_concurrency",
        "random_page_cost", "seq_page_cost",
    }
    assert result["mandatory_bundle_by_role"]["memory_storage"] == bundle


def test_executable_expert_action_cannot_shrink_mandatory_bundle_to_one_knob():
    space = FullKnobSpace()
    current = space.current_config()
    knowledge = retrieve_diagnostic_bundles(
        ["low_buffer_hit_ratio"], {"client": {}, "telemetry": {}}, space, {})
    item = proposal("cache", expert="memory_storage", knob="shared_buffers",
                    old=current["shared_buffers"], new=32768)
    payload = {"diagnostic_knowledge": knowledge}
    with pytest.raises(ValueError, match="invalid_knob_group.*effective_cache_size"):
        DeepSeekExpertBackend._validate_knowledge_bundle(item, payload)


def test_equivalent_result_requires_larger_same_direction_step():
    space = FullKnobSpace()
    current = space.current_config()
    previous_new = float(current["random_page_cost"]) - 0.5
    payload = {
        "current_incumbent": {"config": current},
        "latest_experiment_result": {
            "tested_expert": "query_execution",
            "adaptation_policy": "increase_amplitude_in_same_direction",
            "tested_changes": {"random_page_cost": {
                "old_value": current["random_page_cost"], "new_value": previous_new}},
        },
    }
    too_small = proposal("small", old=current["random_page_cost"],
                         new=float(current["random_page_cost"]) - 0.25)
    with pytest.raises(ValueError, match="invalid_adaptation"):
        DeepSeekExpertBackend._validate_adaptation_policy(too_small, payload)
    larger = proposal("larger", old=current["random_page_cost"],
                      new=float(current["random_page_cost"]) - 1.0)
    DeepSeekExpertBackend._validate_adaptation_policy(larger, payload)
