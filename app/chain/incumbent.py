from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any


def _tps(value: dict[str, Any]) -> float:
    return float(value.get("throughput_rps", 0.0))


def _percent(after: float, before: float) -> float:
    if before == after == 0:
        return 0.0
    return (after - before) / abs(before) * 100.0 if before else float("inf")


def verified_performance(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """Use a conservative repeat score so one lucky run cannot become incumbent."""
    values = [_tps(item) for item in measurements]
    if not values:
        raise ValueError("verified performance requires at least one measurement")
    return {"throughput_rps": min(values), "median_throughput_rps": median(values),
            "replicate_throughput_rps": values, "replicate_count": len(values),
            "score_method": "minimum_of_repeats"}


@dataclass(frozen=True)
class IncumbentState:
    config: dict[str, Any]
    config_hash: str
    verified_performance: dict[str, Any]
    accepted_experiment_id: str
    accepted_at: str
    evidence_summary: dict[str, Any]
    raw_run_records: list[dict[str, Any]] = field(default_factory=list)
    telemetry_snapshot: dict[str, Any] = field(default_factory=dict)
    effective_chain: dict[str, Any] = field(default_factory=dict)
    workload_fingerprint: dict[str, Any] = field(default_factory=dict)
    database_state: dict[str, Any] = field(default_factory=dict)
    experiment_conditions: dict[str, Any] = field(default_factory=dict)

    @property
    def tps(self) -> float:
        return float(self.verified_performance["throughput_rps"])

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class IncumbentPolicy:
    """Evaluate repeatable performance separately from the causal explanation."""

    def decide(self, control: dict[str, Any], treatment: dict[str, Any], validation: dict[str, Any],
               apply_result: dict[str, Any] | None, minimum_tps_percent: float, *,
               incumbent: IncumbentState | None = None,
               retest_control: dict[str, Any] | None = None,
               retest_treatment: dict[str, Any] | None = None,
               activation_confirmed: bool = True,
               max_p95_regression_percent: float = 10.0,
               max_rollback_ratio_increase_pp: float = 1.0) -> dict[str, Any]:
        independent = retest_control is not None and retest_treatment is not None and incumbent is not None
        controls = [control] + ([retest_control] if retest_control else [])
        treatments = [treatment] + ([retest_treatment] if retest_treatment else [])
        candidate = verified_performance(treatments)
        local_changes = [_percent(_tps(t), _tps(c)) for c, t in zip(controls, treatments)]
        incumbent_tps = incumbent.tps if incumbent else _tps(control)
        incumbent_gain = _percent(float(candidate["throughput_rps"]), incumbent_tps)
        applied = activation_confirmed and apply_result is not None and not any(item.get("apply_failed", False) for item in treatments)
        healthy = all(bool(item.get("healthy", True)) and not item.get("error") for item in controls + treatments)
        paired_positive = all(change > 0.0 for change in local_changes)

        latency_ok = True
        rollback_ok = True
        for c, t in zip(controls, treatments):
            before, after = float(c.get("p95_us", 0.0)), float(t.get("p95_us", 0.0))
            if before > 0 and _percent(after, before) > max_p95_regression_percent:
                latency_ok = False
            cb = float(c.get("telemetry", {}).get("rates_per_second", {}).get("rollback_ratio", 0.0))
            tb = float(t.get("telemetry", {}).get("rates_per_second", {}).get("rollback_ratio", 0.0))
            if (tb - cb) * 100.0 > max_rollback_ratio_increase_pp:
                rollback_ok = False
        guardrails = {"healthy": healthy, "p95_latency": latency_ok, "rollback_ratio": rollback_ok}

        if not independent:
            verdict, reason = "uncertain", "independent incumbent retest is missing"
        elif not applied or not all(guardrails.values()):
            verdict, reason = "invalid", "application, health, or safety guardrail failed"
        elif not paired_positive:
            verdict, reason = "no_improvement", "candidate was not positive against every paired local control"
        elif incumbent_gain < minimum_tps_percent:
            verdict = "uncertain" if incumbent_gain > 0 else "no_improvement"
            reason = "independent repeat did not exceed the verified incumbent threshold"
        else:
            verdict = "verified_improvement"
            reason = "both paired comparisons were positive and the conservative repeat score beat the verified incumbent"

        mechanism = validation.get("mechanism_verdict")
        if not mechanism:
            mechanism = "supported" if validation.get("mechanism_path_supported") else "refuted"
        accepted = verdict == "verified_improvement"
        return {"accepted": accepted, "reason": reason, "performance_verdict": verdict,
                "mechanism_verdict": mechanism,
                "unexplained_performance": accepted and mechanism != "supported",
                "local_change_percent": local_changes[0],
                "retest_local_change_percent": local_changes[1] if len(local_changes) > 1 else None,
                "tps_change_percent": local_changes[0], "candidate_verified_performance": candidate,
                "incumbent_before_tps": incumbent_tps,
                "candidate_vs_incumbent_percent": incumbent_gain, "guardrails": guardrails,
                "mechanism_path_supported": mechanism == "supported", "healthy": healthy,
                "applied": applied, "independent_retest_complete": independent,
                "rollback_required": not accepted}
