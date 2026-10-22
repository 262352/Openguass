from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.phase2.space import FullKnobSpace


# These knobs affect evidence collection or log availability.  They remain
# fixed for the whole episode so a candidate cannot win by disabling evidence.
OBSERVATION_KNOBS = frozenset({
    "log_checkpoints", "log_connections", "log_disconnections",
    "log_min_duration_statement", "log_rotation_size", "log_temp_files",
    "logging_collector", "track_activity_query_size",
})

# Scale-100 Twitter at SERIALIZABLE is not runnable with PostgreSQL's boot
# defaults (64/64) on the target environment.  These are declared experiment
# infrastructure exceptions, not tuned candidates.
SAFETY_OVERRIDES: dict[str, Any] = {
    "max_locks_per_transaction": 512,
    "max_pred_locks_per_transaction": 8192,
}


@dataclass(frozen=True)
class DefaultBaselinePlan:
    config: dict[str, Any]
    classifications: dict[str, str]
    exceptions: dict[str, dict[str, Any]]
    tunable_knobs: tuple[str, ...]


def build_default_baseline(catalog: list[dict[str, Any]], space: FullKnobSpace,
                           current: dict[str, Any], workload: str = "twitter") -> DefaultBaselinePlan:
    """Build a reproducible boot-default baseline with declared exceptions."""
    by_name = {row["name"]: row for row in catalog}
    config: dict[str, Any] = {}
    classifications: dict[str, str] = {}
    exceptions: dict[str, dict[str, Any]] = {}
    tunable: list[str] = []
    for name in space.names:
        if workload == "twitter" and name in SAFETY_OVERRIDES:
            raw = SAFETY_OVERRIDES[name]
            classifications[name] = "fixed_environment_safety"
            exceptions[name] = {
                "boot_default": by_name[name].get("boot_val"),
                "effective_baseline": raw,
                "reason": "required for scale-100 Twitter under TRANSACTION_SERIALIZABLE",
            }
        elif name in OBSERVATION_KNOBS:
            raw = current[name]
            classifications[name] = "fixed_observation"
            exceptions[name] = {
                "boot_default": by_name[name].get("boot_val"),
                "effective_baseline": raw,
                "reason": "held constant so evidence/logging cannot be disabled for performance",
            }
        else:
            raw = by_name[name].get("boot_val", current[name])
            classifications[name] = "tuning_space_boot_default"
            tunable.append(name)
        config[name] = space.clamp(name, raw)
    config = space.reconcile(config)
    return DefaultBaselinePlan(config, classifications, exceptions, tuple(tunable))
