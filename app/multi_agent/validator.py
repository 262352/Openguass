from __future__ import annotations

import math
import statistics
from typing import Any

from .models import ExpertProposal, FrozenPrediction, ValidationOutcome
from .paired_decision import decide_paired


def _metric(measurement: dict[str, Any], metric_id: str) -> Any:
    if metric_id in {"tps", "throughput_rps", "target_workload_throughput_rps"}:
        return measurement.get("throughput_rps")
    if metric_id in {"p95_us", "average_us", "p99_us", "max_us"}:
        return measurement.get(metric_id)
    telemetry = measurement.get("telemetry", {})
    for group in ("per_transaction", "rates_per_second", "gauges"):
        value = telemetry.get(group, {}).get(metric_id)
        if isinstance(value, dict):
            return value.get("p95", value.get("median", value.get("max")))
        if value is not None:
            return value
    telemetry_path = metric_id.split(".")
    explicitly_telemetry = telemetry_path[0] == "telemetry"
    if explicitly_telemetry:
        telemetry_path = telemetry_path[1:]
    if telemetry_path and (explicitly_telemetry or telemetry_path[0] in {"wal_timing", "host_io"}):
        value: Any = telemetry
        for part in telemetry_path:
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value is not None:
            return value
    plan = measurement.get("plan_evidence", {})
    if metric_id.startswith("plan_"):
        return plan.get(metric_id.removeprefix("plan_"))
    client = measurement.get("client", {})
    path = metric_id.split(".")
    if path[0] == "client":
        path = path[1:]
    elif path[0] == "transaction_latency":
        path = ["transaction_latency", *path[1:]]
    else:
        path = []
    value: Any = client
    for part in path:
        if not isinstance(value, dict) or part not in value:
            value = None
            break
        value = value[part]
    if value is not None:
        return value
    return client.get(metric_id)


def _relative(after: float, before: float) -> float | None:
    if before == 0:
        return 0.0 if after == 0 else None
    return (after - before) / abs(before) * 100.0


def _finite_number(value: Any) -> float | None:
    """Return a comparable scalar, never coercing aggregate objects into numbers."""
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


class EvidenceValidator:
    """Deterministic validity, performance and frozen-prediction validation."""

    def __init__(self, *, drift_percent: float = 25.0, repeat_spread_percent: float = 25.0,
                 minimum_tps_gain_percent: float = 5.0, max_p95_regression_percent: float = 10.0):
        self.drift_percent = drift_percent
        self.repeat_spread_percent = repeat_spread_percent
        self.minimum_tps_gain_percent = minimum_tps_gain_percent
        self.max_p95_regression_percent = max_p95_regression_percent

    def baseline_stability(self, measurements: list[dict[str, Any]]) -> dict[str, Any]:
        values = [float(m["throughput_rps"]) for m in measurements if m.get("healthy", True) and m.get("throughput_rps") is not None]
        if len(values) < 3:
            return {"stable": False, "reason": "at least three unchanged-configuration measurements are required", "values": values}
        median = statistics.median(values)
        spread = (max(values) - min(values)) / max(abs(median), 1e-12) * 100
        ratio = max(values) / max(min(values), 1e-12)
        stable = spread <= self.repeat_spread_percent and ratio < 2.0
        return {"stable": stable, "values": values, "median": median, "spread_percent": spread, "max_min_ratio": ratio,
                "reason": "stable" if stable else "unchanged configuration has excessive drift"}

    def validate(self, experiment_id: str, proposal: ExpertProposal, *, incumbent_tps: float,
                 controls: list[dict[str, Any]], treatments: list[dict[str, Any]],
                 requested_settings: dict[str, Any], observed_settings: dict[str, Any],
                 expected_context: dict[str, Any], actual_contexts: list[dict[str, Any]],
                 factorial_measurements: dict[str, list[dict[str, Any]]] | None = None) -> ValidationOutcome:
        reasons: list[str] = []
        settings_match = self._settings_match(requested_settings, observed_settings)
        if not settings_match:
            reasons.append("one or more requested settings did not become active")
        if not controls or len(controls) != len(treatments):
            reasons.append("paired control/treatment measurements are incomplete")
        if any(c != expected_context for c in actual_contexts):
            reasons.append("database, workload, snapshot, hardware or source context changed")
        all_runs = controls + treatments
        if any(not m.get("healthy", True) or m.get("error") for m in all_runs):
            reasons.append("benchmark or database health check failed")
        if any(self._client_failed(m) for m in all_runs):
            reasons.append("workload client reported failures, timeouts or unexpected rollbacks")
        control_stability = self._spread(controls)
        treatment_stability = self._spread(treatments)
        regime_pairs = [(c.get("client", {}).get("regime", "unknown"),
                         t.get("client", {}).get("regime", "unknown"))
                        for c, t in zip(controls, treatments)]
        invalid_regime_blocks = [index + 1 for index, pair in enumerate(regime_pairs)
                                 if pair[0] != pair[1]]
        unstable = bool(invalid_regime_blocks)
        if invalid_regime_blocks:
            reasons.append(
                f"control/treatment regime mismatch in blocks {invalid_regime_blocks}: {regime_pairs}")
        if reasons:
            validity = "unstable" if unstable and settings_match else "invalid"
        else:
            validity = "valid"

        edge_results = self._validate_edges(proposal, controls, treatments, validity, factorial_measurements)
        mechanism_states = [v["state"] for k, v in edge_results.items() if not self._objective_edge(proposal, k)]
        if validity != "valid" or not mechanism_states or "inconclusive" in mechanism_states:
            mechanism = "inconclusive"
        elif "tested_refuted" in mechanism_states:
            mechanism = "refuted"
        elif all(x == "tested_supported" for x in mechanism_states):
            mechanism = "supported"
        else:
            mechanism = "inconclusive"

        verified = None
        performance = "invalid" if validity == "invalid" else "inconclusive"
        accepted = False
        if validity == "valid":
            tps = [float(m["throughput_rps"]) for m in treatments]
            paired = [_relative(float(t["throughput_rps"]), float(c["throughput_rps"]))
                      for c, t in zip(controls, treatments)]
            latency_regressions = [self._latency_regression(c, t)
                                   for c, t in zip(controls, treatments)]
            transaction_semantics = [self._transaction_semantics(c, t)
                                     for c, t in zip(controls, treatments)]
            verified = {"throughput_rps": min(tps), "median_throughput_rps": statistics.median(tps),
                        "replicate_throughput_rps": tps,
                        "score_method": "minimum_of_comparable_repeats",
                        "paired_tps_gain_percent": paired,
                        "incumbent_gain_percent": _relative(min(tps), incumbent_tps),
                        "latency_diagnostic_only": {
                            "maximum_regression_percent_by_pair": latency_regressions,
                            "former_guardrail_percent": self.max_p95_regression_percent,
                            "used_as_acceptance_gate": False,
                        },
                        "transaction_semantics": transaction_semantics}
            decision = decide_paired(
                [float(x) / 100.0 for x in paired if x is not None],
                delta_min=max(0.0, self.minimum_tps_gain_percent) / 100.0)
            both_positive = len(paired) >= 2 and all(
                value is not None and float(value) > 0 for value in paired)
            decision_record = decision.as_dict()
            decision_record["statistical_verdict"] = decision.verdict
            decision_record["both_stable_pairs_positive"] = both_positive
            decision_record["acceptance_rule"] = (
                "accept when every one of at least two valid paired blocks has treatment TPS above control TPS")
            if both_positive:
                decision_record.update(
                    verdict="accept",
                    reason="every valid paired treatment TPS is above its paired control TPS")
            verified["paired_decision"] = decision_record
            verified["regime_pairs"] = regime_pairs
            # TPS remains the sole optimization objective. Latency, retries,
            # and mechanism metrics remain diagnostic rather than vetoes.
            effective_verdict = decision_record["verdict"]
            if effective_verdict == "accept":
                performance, accepted = "verified_improvement", True
            elif effective_verdict == "reject_harmful":
                performance = "no_improvement"
                mechanism = "refuted"
                edge_results = self._override_nonobjective_edges(
                    proposal, edge_results, "tested_refuted",
                    "paired TPS confidence interval shows a materially harmful direction")
            elif effective_verdict == "reject_equivalent":
                performance = "inconclusive"
                mechanism = "inconclusive"
                edge_results = self._override_nonobjective_edges(
                    proposal, edge_results, "inconclusive",
                    "equivalent TPS does not refute the mechanism; increase action amplitude")
            else:
                performance = "inconclusive"
        return ValidationOutcome(
            experiment_id=experiment_id, validity=validity, validity_reasons=reasons,
            performance_verdict=performance, mechanism_verdict=mechanism,
            accepted_as_incumbent=accepted, unexplained_performance=accepted and mechanism != "supported",
            edge_results=edge_results, verified_performance=verified, actual_settings_match=settings_match,
        )

    @staticmethod
    def _override_nonobjective_edges(proposal: ExpertProposal,
                                     edge_results: dict[str, dict[str, Any]],
                                     state: str, reason: str) -> dict[str, dict[str, Any]]:
        result = {name: dict(value) for name, value in edge_results.items()}
        for edge_id in result:
            if not EvidenceValidator._objective_edge(proposal, edge_id):
                result[edge_id]["state"] = state
                result[edge_id]["reason"] = reason
        return result

    @staticmethod
    def _transaction_semantics(control: dict[str, Any], treatment: dict[str, Any]) -> dict[str, Any]:
        """Keep success/retry/failure semantics beside latency diagnostics."""
        before, after = control.get("client", {}), treatment.get("client", {})
        before_retry = _finite_number(before.get("server_retry_ratio"))
        after_retry = _finite_number(after.get("server_retry_ratio"))
        retry_change = None
        if before_retry is not None and after_retry is not None:
            retry_change = after_retry - before_retry
        tps_gain = _relative(float(treatment["throughput_rps"]), float(control["throughput_rps"]))
        latency_regression = EvidenceValidator._latency_regression(control, treatment)
        semantic_improvement = bool(
            tps_gain is not None and tps_gain > 0
            and retry_change is not None and retry_change < -0.05
        )
        return {
            "control": {key: before.get(key) for key in (
                "successful_transactions", "transaction_attempts", "server_retry_transactions",
                "reconnect_retry_transactions", "aborted_transactions", "final_failures",
                "successful_transaction_tps", "attempted_transaction_tps", "server_retry_ratio")},
            "treatment": {key: after.get(key) for key in (
                "successful_transactions", "transaction_attempts", "server_retry_transactions",
                "reconnect_retry_transactions", "aborted_transactions", "final_failures",
                "successful_transaction_tps", "attempted_transaction_tps", "server_retry_ratio")},
            "tps_gain_percent": tps_gain,
            "server_retry_ratio_change": retry_change,
            "maximum_success_latency_regression_percent": latency_regression,
            "classification": (
                "semantic_improvement_review" if semantic_improvement
                else "ordinary_success_latency_diagnostic"),
            "acceptance_effect": "diagnostic_only",
        }

    def _validate_edges(self, proposal: ExpertProposal, controls: list[dict[str, Any]], treatments: list[dict[str, Any]], validity: str,
                        factorial_measurements: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for edge in proposal.edges:
            observations = []
            if validity != "valid":
                result[edge.edge_id] = {"state": "inconclusive", "reason": f"experiment validity is {validity}", "observations": []}
                continue
            for prediction in [p for p in proposal.predictions if p.edge_id == edge.edge_id]:
                observations.append(self._assess_prediction(prediction, controls, treatments))
            states = [o["state"] for o in observations]
            state = "tested_supported" if states and all(s == "tested_supported" for s in states) else (
                "tested_refuted" if "tested_refuted" in states else "inconclusive")
            result[edge.edge_id] = {"state": state, "reason": "; ".join(o["reason"] for o in observations), "observations": observations}
        # Joint success alone never supports interaction.
        factorial_tps = {key: [float(x["throughput_rps"]) for x in values]
                         for key, values in (factorial_measurements or {}).items()}
        threshold = proposal.interaction_design.minimum_nonadditivity_percent if proposal.interaction_design else 5.0
        if proposal.interaction_factors and not self.interaction_is_identified(proposal, factorial_tps, threshold):
            for edge in proposal.edges:
                if any(n.node_id in {edge.source, edge.target} and n.kind == "interaction" for n in proposal.nodes):
                    result[edge.edge_id] = {"state": "inconclusive", "reason": "A-only/B-only/A+B factorial observations are incomplete", "observations": []}
        return result

    def _assess_prediction(self, prediction: FrozenPrediction, controls: list[dict[str, Any]], treatments: list[dict[str, Any]]) -> dict[str, Any]:
        pairs = [(_metric(c, prediction.metric_id), _metric(t, prediction.metric_id)) for c, t in zip(controls, treatments)]
        if any(a is None or b is None for a, b in pairs):
            return {"prediction_id": prediction.prediction_id, "state": "inconclusive", "reason": "required metric was not observed", "pairs": pairs}
        if prediction.metric_id in {"idx_scan", "seq_scan"}:
            return {"prediction_id": prediction.prediction_id, "state": "inconclusive",
                    "reason": "per-second scan counters are workload-volume coupled; use per-transaction or plan evidence", "pairs": pairs}
        changes = []
        for before, after in pairs:
            if prediction.comparison_method == "plan_changed":
                changes.append(100.0 if before != after else 0.0)
                continue
            before_number, after_number = _finite_number(before), _finite_number(after)
            if before_number is None or after_number is None:
                return {
                    "prediction_id": prediction.prediction_id,
                    "state": "inconclusive",
                    "reason": (
                        f"metric {prediction.metric_id} is not a finite scalar; "
                        "use a leaf metric such as .median, .p95 or .max"
                    ),
                    "pairs": pairs,
                }
            if prediction.comparison_method == "relative_percent":
                value = _relative(after_number, before_number)
                if value is None:
                    return {"prediction_id": prediction.prediction_id, "state": "inconclusive", "reason": "zero baseline prevents relative comparison", "pairs": pairs}
                changes.append(value)
            else:
                changes.append(after_number - before_number)
        threshold = prediction.minimum_change
        if prediction.expected_direction == "increase": passed = all(x >= threshold for x in changes)
        elif prediction.expected_direction == "decrease": passed = all(x <= -threshold for x in changes)
        else: passed = all(abs(x) <= threshold for x in changes)
        return {"prediction_id": prediction.prediction_id, "state": "tested_supported" if passed else "tested_refuted",
                "reason": f"frozen direction={prediction.expected_direction}, threshold={threshold}, observed={changes}", "pairs": pairs}

    @staticmethod
    def interaction_is_identified(proposal: ExpertProposal, factorial_observations: dict[str, list[float]], tolerance_percent: float = 5.0) -> bool:
        if not proposal.interaction_factors or set(factorial_observations) != {"baseline", "a_only", "b_only", "a_plus_b"}:
            return False
        if any(len(v) < 2 for v in factorial_observations.values()):
            return False
        means = {k: statistics.mean(v) for k, v in factorial_observations.items()}
        expected_additive = means["a_only"] + means["b_only"] - means["baseline"]
        difference = abs(means["a_plus_b"] - expected_additive) / max(abs(means["baseline"]), 1e-12) * 100
        return difference >= tolerance_percent

    @staticmethod
    def _settings_match(requested: dict[str, Any], observed: dict[str, Any]) -> bool:
        def norm(value: Any) -> str:
            if isinstance(value, dict): value = value.get("setting")
            try: return f"{float(value):.8g}"
            except (TypeError, ValueError): return str(value).lower()
        return all(name in observed and norm(value) == norm(observed[name]) for name, value in requested.items())

    @staticmethod
    def _client_failed(measurement: dict[str, Any]) -> bool:
        client = measurement.get("client", {})
        return any(float(client.get(k, 0) or 0) > 0 for k in ("failed", "timeouts", "unexpected_sql_errors"))

    @staticmethod
    def _spread(measurements: list[dict[str, Any]]) -> float:
        values = [float(m["throughput_rps"]) for m in measurements if m.get("throughput_rps") is not None]
        if len(values) < 2: return math.inf
        return (max(values) - min(values)) / max(abs(statistics.median(values)), 1e-12) * 100

    @staticmethod
    def _latency_regression(control: dict[str, Any], treatment: dict[str, Any]) -> float:
        """Guard overall and per-transaction tails; aggregate p95 can hide rare stalls."""
        regressions: list[float] = []
        for name in ("p95_us", "average_us", "p99_us"):
            before, after = control.get(name), treatment.get(name)
            if before is not None and after is not None and float(before) > 0:
                regressions.append(_relative(float(after), float(before)) or 0.0)
        before_tx = control.get("client", {}).get("transaction_latency", {})
        after_tx = treatment.get("client", {}).get("transaction_latency", {})
        for transaction in set(before_tx) & set(after_tx):
            for name in ("mean_us", "p50_us", "p99_us"):
                before, after = before_tx[transaction].get(name), after_tx[transaction].get(name)
                if before is not None and after is not None and float(before) > 0:
                    regressions.append(_relative(float(after), float(before)) or 0.0)
        return max(regressions, default=0.0)

    @staticmethod
    def _objective_edge(proposal: ExpertProposal, edge_id: str) -> bool:
        objective_ids = {n.node_id for n in proposal.nodes if n.kind == "objective"}
        return any(e.edge_id == edge_id and e.target in objective_ids for e in proposal.edges)
