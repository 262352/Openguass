from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.phase2.space import FullKnobSpace, ROOT

KNOWLEDGE_PATH = ROOT / "knowledge/postgres_diagnostic_knob_relations.json"


def _normalized_retry(measurement: dict[str, Any]) -> float:
    value = float(measurement.get("client", {}).get("server_retry_ratio", 0) or 0)
    return value / 100.0 if value > 1.0 else value


def retrieve_diagnostic_bundles(anomalies: list[str], measurement: dict[str, Any],
                                space: FullKnobSpace,
                                experiment_counts: dict[str, int],
                                current_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Join diagnosed phenomena with literature relations and live safe knob metadata."""
    data = json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    phenomena = set(anomalies)
    # Normalize telemetry aliases into the mechanism vocabulary used by the
    # literature relation database. Preserve the raw names for auditability.
    if "wal_wait" in phenomena:
        phenomena.add("wal_sync_latency")
    if "host_io_pressure" in phenomena:
        phenomena.update({"checkpoint_write_pressure", "wal_sync_latency"})
    if _normalized_retry(measurement) > 0.10:
        phenomena.add("serializable_retry_storm")
    per_txn = measurement.get("telemetry", {}).get("per_transaction", {})
    if float(per_txn.get("n_dead_tup_per_txn", 0) or 0) > 0.25:
        phenomena.add("dead_tuple_pressure")

    dimensions = {item.name: item for item in space.dimensions}
    bundles = []
    mandatory_by_role: dict[str, dict[str, Any]] = {}
    priority = {
        "serializable_retry_storm": 0,
        "low_buffer_hit_ratio": 1,
        "wal_sync_latency": 2,
        "checkpoint_write_pressure": 3,
        "temp_spill": 4,
        "dead_tuple_pressure": 5,
    }
    candidates = sorted(
        (item for item in data["bundles"] if item["phenomenon"] in phenomena),
        key=lambda item: priority.get(item["phenomenon"], 99))
    for item in candidates:
        related = []
        for relation in item["related_knobs"]:
            name = relation["knob"]
            dimension = dimensions.get(name)
            if dimension is None:
                continue
            current = (current_config or {}).get(name, dimension.current)
            direction = relation["direction"]
            if dimension.choices:
                actionable = not (direction == "enable" and str(current).lower() == "on")
            else:
                current_number = float(current)
                actionable = not (
                    (direction == "increase" and current_number >= float(dimension.high))
                    or (direction == "decrease" and current_number <= float(dimension.low)))
            related.append({
                **relation,
                "current": current,
                "vartype": dimension.vartype,
                "execution_class": dimension.execution_class,
                "unit": dimension.unit,
                "safe_low": dimension.low,
                "safe_high": dimension.high,
                "choices": list(dimension.choices),
                "prior_experiment_count": int(experiment_counts.get(name, 0)),
                "actionable_from_current": actionable,
            })
        bundle = {
            **item,
            "related_knobs": related,
            "required_knobs": [value["knob"] for value in related
                               if value["actionable_from_current"]],
            "non_actionable_knobs": [value["knob"] for value in related
                                     if not value["actionable_from_current"]],
        }
        bundles.append(bundle)
        for role in item["roles"]:
            mandatory_by_role.setdefault(role, bundle)
    return {
        "schema_version": data["schema_version"],
        "active_phenomena": sorted(phenomena),
        "bundles": bundles,
        "mandatory_bundle_by_role": mandatory_by_role,
        "policy": (
            "A mechanism action tunes every actionable knob in its required_knobs as one coherent group. "
            "The expert chooses exact values from the live safe ranges and explains each knob's role."),
        "knowledge_source": str(KNOWLEDGE_PATH),
    }
