from app.schemas.models import VerificationResult


class ThresholdVerifier:
    def __init__(self, minimum_p95_improvement: float = 0.10):
        self.minimum_p95_improvement = minimum_p95_improvement

    def verify(self, before: dict, after: dict, expected_metric: str) -> VerificationResult:
        if before.get("errors", 0) > 0 or after.get("errors", 0) > 0:
            return VerificationResult(verdict="experiment_failed", criteria={}, explanation="benchmark reported errors")
        if min(before.get("transactions", 0), after.get("transactions", 0)) <= 0:
            return VerificationResult(verdict="experiment_failed", criteria={}, explanation="no valid transactions")
        p95_ok = after["p95_ms"] <= before["p95_ms"] * (1 - self.minimum_p95_improvement)
        errors_ok = after.get("error_rate", 0) <= before.get("error_rate", 0)
        metric_ok = after[expected_metric] < before[expected_metric]
        criteria = {"p95_improved": p95_ok, "errors_not_worse": errors_ok, "predicted_metric_improved": metric_ok}
        verdict = "supported" if all(criteria.values()) else "not_supported"
        return VerificationResult(verdict=verdict, criteria=criteria, explanation="all required criteria evaluated")
