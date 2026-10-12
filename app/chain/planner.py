from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import CounterfactualContract


@dataclass(frozen=True)
class PlannedExperiment:
    label: str
    changed_knobs: tuple[str, ...]
    config: dict[str, Any]


class ExperimentPlanner:
    """Expand an immutable contract into identifiable treatment configurations."""

    def plan(self, contract: CounterfactualContract, baseline: dict[str, Any], remaining_budget: int) -> list[PlannedExperiment]:
        group = contract.intervention_group
        actions = group.knob_actions
        if group.group_type == "independent":
            plans = [self._one(f"single:{a.knob}", baseline, (a,)) for a in actions]
        elif group.group_type == "coupled":
            if not group.dependency_knowledge_ids:
                raise ValueError("coupled actions require a documented dependency")
            plans = [self._one("coupled", baseline, actions)]
        else:
            # A compact factorial: each main effect and the combined interaction.
            plans = [self._one(f"main:{a.knob}", baseline, (a,)) for a in actions]
            plans.append(self._one("interaction:all", baseline, actions))
        if len(plans) > remaining_budget:
            raise RuntimeError(f"experiment budget requires {len(plans)} treatments but only {remaining_budget} remain")
        return plans

    @staticmethod
    def _one(label: str, baseline: dict[str, Any], actions: tuple) -> PlannedExperiment:
        config = dict(baseline)
        for action in actions:
            config[action.knob] = action.proposed_value
        return PlannedExperiment(label, tuple(a.knob for a in actions), config)
