from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import CounterfactualContract, Prediction


@dataclass(frozen=True)
class PredictionResult:
    metric_id: str
    target_edge_id: str
    control_value: float | None
    treatment_value: float | None
    observed_change: float | None
    status: str
    reason: str


def _lookup(measurement: dict[str, Any], metric_id: str) -> float | None:
    if metric_id in measurement and isinstance(measurement[metric_id], (int, float)):
        return float(measurement[metric_id])
    telemetry = measurement.get("telemetry", measurement)
    rates = telemetry.get("rates_per_second", {})
    if metric_id in rates:
        return float(rates[metric_id])
    normalized = telemetry.get("per_transaction", {})
    if metric_id in normalized:
        return float(normalized[metric_id])
    gauges = telemetry.get("gauges", {})
    if metric_id in gauges:
        value = gauges[metric_id]
        if isinstance(value, dict):
            return float(value.get("p95", value.get("max", value.get("median"))))
        return float(value)
    if metric_id in {"tps", "target_workload_throughput_rps"}:
        value = measurement.get("throughput_rps")
        return float(value) if value is not None else None
    plan = measurement.get("plan_evidence", {})
    if metric_id.startswith("plan_"):
        value = plan.get(metric_id.removeprefix("plan_"))
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def assess_prediction(prediction: Prediction, control: dict[str, Any], treatment: dict[str, Any]) -> PredictionResult:
    before, after = _lookup(control, prediction.metric_id), _lookup(treatment, prediction.metric_id)
    if before is None or after is None:
        return PredictionResult(prediction.metric_id, prediction.target_edge_id, before, after, None, "inconclusive", "required metric is missing")
    if prediction.comparison_method == "relative_percent":
        change = 0.0 if before == after == 0 else ((after - before) / abs(before) * 100 if before else float("inf"))
    else:
        change = after - before
    threshold = prediction.minimum_change
    if prediction.expected_direction == "increase":
        passed = change >= threshold
    elif prediction.expected_direction == "decrease":
        passed = change <= -threshold
    else:
        passed = abs(change) <= threshold
    return PredictionResult(
        prediction.metric_id, prediction.target_edge_id, before, after, change,
        "supported" if passed else "refuted",
        f"observed {change:.6g} by {prediction.comparison_method}; frozen threshold {threshold:g}",
    )


def validate_contract(contract: CounterfactualContract, control: dict[str, Any], treatment: dict[str, Any], experiment_failed: bool = False) -> dict[str, Any]:
    predictions = (*contract.direct_predictions, *contract.intermediate_predictions, contract.target_prediction)
    if experiment_failed:
        results = [PredictionResult(item.metric_id, item.target_edge_id, None, None, None, "inconclusive", "control or treatment experiment failed") for item in predictions]
    else:
        results = [assess_prediction(item, control, treatment) for item in predictions]
    edge_updates: dict[str, dict[str, Any]] = {}
    for result in results:
        previous = edge_updates.get(result.target_edge_id)
        status = result.status
        if previous and previous["status"] != status:
            status = "inconclusive"
        edge_updates[result.target_edge_id] = {"status": status, "reason": result.reason}
    action_knobs = {item.knob for item in contract.knob_actions}
    activation_status = "supported"
    activation_reason = "no special activation event required"
    rates = treatment.get("telemetry", {}).get("rates_per_second", {})
    if "default_statistics_target" in action_knobs and float(rates.get("analyze_count", 0)) <= 0:
        activation_status, activation_reason = "inconclusive", "statistics hypothesis had no observed ANALYZE event"
    if action_knobs & {"autovacuum_analyze_scale_factor", "autovacuum_naptime", "autovacuum_max_workers"} and float(rates.get("autoanalyze_count", 0)) <= 0:
        activation_status, activation_reason = "inconclusive", "autovacuum hypothesis had no observed autoanalyze event"
    circular = [item for item in results if item.metric_id in {"xact_commit", "idx_scan", "seq_scan"}]
    if circular:
        activation_status = "inconclusive"
        activation_reason = "raw per-second work counters cannot independently prove the mechanism; use normalized or plan evidence"
    if activation_status != "supported":
        for edge_id in list(edge_updates):
            if edge_id != contract.target_prediction.target_edge_id:
                edge_updates[edge_id] = {"status": "inconclusive", "reason": activation_reason}
    statuses = [item["status"] for item in edge_updates.values()]
    if activation_status != "supported":
        mechanism_verdict = activation_status
    elif statuses and all(item == "supported" for item in statuses):
        mechanism_verdict = "supported"
    elif "refuted" in statuses:
        mechanism_verdict = "refuted"
    else:
        mechanism_verdict = "inconclusive"
    return {
        "contract_id": contract.contract_id,
        "prediction_results": [item.__dict__ for item in results],
        "edge_updates": edge_updates,
        "activation_check": {"status": activation_status, "reason": activation_reason},
        "mechanism_verdict": mechanism_verdict,
        "mechanism_path_supported": mechanism_verdict == "supported",
    }
