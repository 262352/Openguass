from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class PairedDecision:
    verdict: str
    reason: str
    effect_mean: float
    ci_low: float
    ci_high: float
    delta_min: float
    blocks: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def decide_paired(gains: list[float], *, delta_min: float = 0.03,
                  alpha: float = 0.10, samples: int = 10_000,
                  seed: int = 20261020) -> PairedDecision:
    """Three-state block-bootstrap decision over paired relative TPS gains."""
    if len(gains) < 2:
        return PairedDecision("unresolved", "at least two valid blocks are required",
                              statistics.mean(gains) if gains else 0.0,
                              float("-inf"), float("inf"), delta_min, len(gains))
    rng = random.Random(seed)
    estimates = sorted(statistics.mean(rng.choices(gains, k=len(gains)))
                       for _ in range(samples))
    lo_index = max(0, int((alpha / 2) * samples))
    hi_index = min(samples - 1, int((1 - alpha / 2) * samples) - 1)
    lo, hi = estimates[lo_index], estimates[hi_index]
    effect = statistics.mean(gains)
    if lo > delta_min:
        verdict, reason = "accept", "confidence interval is above the minimum worthwhile gain"
    elif hi < -delta_min:
        verdict, reason = "reject_harmful", "confidence interval is below the harmful-effect boundary"
    elif lo > -delta_min and hi < delta_min:
        verdict, reason = "reject_equivalent", "confidence interval lies inside the equivalence region"
    else:
        verdict, reason = "unresolved", "available blocks cannot distinguish benefit, harm, or equivalence"
    return PairedDecision(verdict, reason, effect, lo, hi, delta_min, len(gains))
