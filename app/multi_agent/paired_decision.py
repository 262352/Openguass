from __future__ import annotations

import math
import random
import statistics
from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True)
class PairedDecision:
    verdict: str
    reason: str
    effect_mean: float
    ci_low: float
    ci_high: float
    delta_min: float
    blocks: int
    valid_blocks: int | None = None
    outlier_blocks: int = 0
    should_continue: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def log_gain(champion_tps: float, candidate_tps: float) -> float:
    if champion_tps <= 0 or candidate_tps <= 0:
        raise ValueError("paired TPS values must be positive")
    return math.log(candidate_tps / champion_tps)


def is_three_sigma_outlier(value: float, history: Iterable[float], *, sigma: float = 3.0) -> bool:
    values = [float(item) for item in history if math.isfinite(float(item))]
    if len(values) < 3:
        return False
    deviation = statistics.stdev(values)
    return deviation > 0 and abs(float(value) - statistics.mean(values)) > sigma * deviation


class SequentialPairedPolicy:
    """At most three synchronous champion/candidate blocks with early loss stopping."""

    def __init__(self, *, delta_min: float = 0.03, max_blocks: int = 3):
        if delta_min <= 0:
            raise ValueError("delta_min must be positive")
        if max_blocks not in {2, 3}:
            raise ValueError("max_blocks must be two or three")
        self.delta_min = float(delta_min)
        self.max_blocks = max_blocks

    def decide(self, gains: list[float], *, outlier_blocks: int = 0,
               high_noise: bool = False) -> PairedDecision:
        valid = [float(value) for value in gains if math.isfinite(float(value))]
        effect = statistics.mean(valid) if valid else 0.0
        base = dict(effect_mean=effect, ci_low=float("-inf"), ci_high=float("inf"),
                    delta_min=self.delta_min, blocks=len(valid) + outlier_blocks,
                    valid_blocks=len(valid), outlier_blocks=outlier_blocks)
        if outlier_blocks >= 2:
            return PairedDecision("inconclusive", "two consecutive paired blocks were outliers",
                                  should_continue=False, **base)
        if not valid:
            return PairedDecision("continue", "no valid paired block is available",
                                  should_continue=outlier_blocks < 2, **base)
        if len(valid) == 1:
            gain = valid[0]
            if gain < -self.delta_min and not high_noise:
                return PairedDecision("early_stopped_worse",
                    "first paired log gain is below the harmful boundary",
                    should_continue=False, **base)
            return PairedDecision("continue",
                "a second paired block is required" + (" for the one-block comeback check" if high_noise else ""),
                should_continue=True, **base)
        same_positive = all(value > 0 for value in valid)
        same_negative = all(value < 0 for value in valid)
        if effect > self.delta_min and same_positive:
            return PairedDecision("accept",
                "mean paired log gain exceeds delta_min and all valid blocks agree",
                should_continue=False, **base)
        if effect < -self.delta_min and same_negative:
            return PairedDecision("reject_harmful",
                "mean paired log gain is below the harmful boundary and all valid blocks agree",
                should_continue=False, **base)
        crosses_zero = min(valid) <= 0 <= max(valid)
        if len(valid) < self.max_blocks and crosses_zero:
            return PairedDecision("continue", "paired effects cross zero; run one final block",
                                  should_continue=True, **base)
        return PairedDecision("inconclusive",
            "valid paired blocks do not show a consistent effect above delta_min",
            should_continue=False, **base)


def decide_paired(gains: list[float], *, delta_min: float = 0.03,
                  alpha: float = 0.10, samples: int = 10_000,
                  seed: int = 20261020) -> PairedDecision:
    """Three-state block-bootstrap decision over paired relative TPS gains."""
    if len(gains) < 2:
        return PairedDecision("unresolved", "at least two valid blocks are required",
                              statistics.mean(gains) if gains else 0.0,
                              float("-inf"), float("inf"), delta_min, len(gains), len(gains))
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
    return PairedDecision(verdict, reason, effect, lo, hi, delta_min, len(gains), len(gains))
