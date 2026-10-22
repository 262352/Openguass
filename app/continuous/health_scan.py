from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.phase2.space import FullKnobSpace


@dataclass(frozen=True)
class CoverageAction:
    subsystem: str
    knob: str
    multiplier: float
    reason: str


class HealthCoveragePlanner:
    """Mandatory representative perturbations independent of expert ranking."""

    ACTIONS = (
        CoverageAction("memory_cache", "shared_buffers", 4.0, "at least four times the default, capped near 25% RAM"),
        CoverageAction("wal_durability", "wal_buffers", 4.0, "four times the default"),
        CoverageAction("query_planning", "random_page_cost", .5, "half the default"),
        CoverageAction("query_planning", "random_page_cost", 2.0, "twice the default"),
        CoverageAction("concurrency_maintenance", "work_mem", 2.0, "twice the default"),
        CoverageAction("concurrency_maintenance", "work_mem", 4.0, "four times the default"),
        CoverageAction("memory_cache", "effective_cache_size", 2.0, "expert-protocol reserve: planner cache estimate"),
        CoverageAction("memory_cache", "effective_io_concurrency", 2.0, "expert-protocol reserve: prefetch depth"),
        CoverageAction("wal_durability", "wal_writer_delay", .5, "expert-protocol reserve: writer cadence"),
        CoverageAction("concurrency_maintenance", "max_pred_locks_per_transaction", 2.0,
                       "expert-protocol reserve: predicate-lock capacity"),
        CoverageAction("query_planning", "seq_page_cost", .5,
                       "coverage reserve: cheaper sequential access"),
        CoverageAction("query_planning", "seq_page_cost", 2.0,
                       "coverage reserve: more expensive sequential access"),
        CoverageAction("memory_cache", "temp_buffers", 2.0,
                       "coverage reserve: session-local temporary buffers"),
        CoverageAction("wal_durability", "checkpoint_completion_target", 1.25,
                       "coverage reserve: spread checkpoint writes"),
        CoverageAction("wal_durability", "bgwriter_lru_maxpages", 2.0,
                       "coverage reserve: background writer capacity"),
    )

    def __init__(self, space: FullKnobSpace, *, memory_bytes: int | None = None):
        self.space, self.memory_bytes = space, memory_bytes

    def compile(self, baseline: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for action in self.ACTIONS:
            if action.knob not in baseline or action.knob not in self.space.names:
                continue
            current = baseline[action.knob]
            try:
                target = float(current) * action.multiplier
                if action.knob == "shared_buffers" and self.memory_bytes:
                    # PostgreSQL reports this knob in 8KiB blocks in this catalog.
                    target = max(target, (self.memory_bytes * .25) / 8192)
                if str(current).lstrip("-").isdigit():
                    target = int(round(target))
                target = self.space.clamp(action.knob, target)
            except (TypeError, ValueError):
                continue
            result.append({"subsystem": action.subsystem, "knob": action.knob,
                           "old_value": current, "new_value": target,
                           "reason": action.reason, "duration_seconds": 30,
                           "minimum_paired_blocks": 2})
        return result
