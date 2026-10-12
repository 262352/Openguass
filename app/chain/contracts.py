from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class KnobAction(BaseModel):
    model_config = ConfigDict(frozen=True)
    knob: str
    current_value: str | int | float | bool
    proposed_value: str | int | float | bool
    evidence_ids: tuple[str, ...]
    knowledge_ids: tuple[str, ...]
    mechanism: str
    mechanism_role: str = ""
    numerical_basis: str = ""
    restart_required: bool
    activation_check: str = "verify SHOW value and relevant runtime event"
    resource_guardrails: tuple[str, ...] = ()


class Prediction(BaseModel):
    model_config = ConfigDict(frozen=True)
    metric_id: str
    expected_direction: Literal["increase", "decrease", "unchanged"]
    minimum_change: float = Field(ge=0)
    unit: str
    target_edge_id: str
    comparison_method: Literal["relative_percent", "absolute_delta", "percentage_points"]


class InterventionGroup(BaseModel):
    model_config = ConfigDict(frozen=True)
    group_id: str
    group_type: Literal["independent", "coupled", "interaction"]
    knob_actions: tuple[KnobAction, ...]
    dependency_knowledge_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def identifiable(self):
        if not self.knob_actions:
            raise ValueError("intervention group requires at least one knob action")
        if self.group_type == "coupled" and not self.dependency_knowledge_ids:
            raise ValueError("coupled intervention requires catalog dependency knowledge")
        if self.group_type == "interaction" and len(self.knob_actions) < 2:
            raise ValueError("interaction intervention requires at least two knob actions")
        return self


class CounterfactualContract(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: str = "andromeda.counterfactual-contract/v1"
    contract_id: str
    hypothesis_id: str
    frontier_id: str = "frontier:legacy"
    baseline_state_id: str = "state:legacy"
    target_edge_ids: tuple[str, ...]
    intervention_group: InterventionGroup
    baseline_config_hash: str
    control_variables: dict[str, Any]
    direct_predictions: tuple[Prediction, ...]
    intermediate_predictions: tuple[Prediction, ...]
    target_prediction: Prediction
    thresholds: dict[str, float]
    warmup_seconds: int = Field(ge=0)
    measurement_seconds: int = Field(ge=5)
    seed: int
    risk: str = Field(validation_alias=AliasChoices("risk", "risk_assessment"))
    restart_required: bool
    rollback_plan: str
    abort_conditions: tuple[str, ...]
    why_exceeds_incumbent: str = ""
    mechanism_measurement: str = ""
    alternative_explanations: tuple[str, ...] = ()

    @property
    def knob_actions(self) -> tuple[KnobAction, ...]:
        return self.intervention_group.knob_actions

    @model_validator(mode="after")
    def predictions_bind_declared_edges(self):
        known = set(self.target_edge_ids)
        predictions = (*self.direct_predictions, *self.intermediate_predictions, self.target_prediction)
        unknown = [item.target_edge_id for item in predictions if item.target_edge_id not in known]
        if unknown:
            raise ValueError(f"predictions reference undeclared target edges: {unknown}")
        required_controls = {"target_workload", "snapshot_id", "terminals", "duration", "seed", "warmup_seconds", "postgresql_version", "resource_scope", "transaction_mix"}
        missing = sorted(required_controls - set(self.control_variables))
        if missing:
            raise ValueError(f"missing frozen control variables: {missing}")
        return self
