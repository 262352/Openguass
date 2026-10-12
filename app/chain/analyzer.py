from __future__ import annotations

from typing import Any

from .validator import _lookup


class BottleneckFrontierAnalyzer:
    LABELS = {"resolved", "improved", "persistent", "worsened", "emerged", "unchanged", "missing", "unknown"}

    def classify(self, metric_id: str, expected_direction: str | None, control: dict[str, Any], treatment: dict[str, Any], threshold: float) -> dict[str, Any]:
        before, after = _lookup(control, metric_id), _lookup(treatment, metric_id)
        if before is None or after is None:
            return {"classification": "missing", "metric_id": metric_id, "before": before, "after": after}
        if expected_direction not in {"increase", "decrease", "unchanged"}:
            return {"classification": "unknown", "metric_id": metric_id, "before": before, "after": after}
        delta = after - before
        scale = abs(before) if before else 1.0
        change = delta / scale * 100.0
        favorable = change if expected_direction == "increase" else -change if expected_direction == "decrease" else -abs(change)
        if before == 0 and after != 0 and expected_direction == "decrease":
            label = "emerged"
        elif after == 0 and before != 0 and expected_direction == "decrease":
            label = "resolved"
        elif favorable >= threshold:
            label = "improved"
        elif favorable <= -threshold:
            label = "worsened"
        elif abs(change) < max(threshold, 1e-12):
            label = "unchanged"
        else:
            label = "persistent"
        return {"classification": label, "metric_id": metric_id, "before": before, "after": after, "relative_change_percent": change}

    def analyze(self, predictions: tuple, control: dict[str, Any], treatment: dict[str, Any]) -> list[dict[str, Any]]:
        return [self.classify(p.metric_id, p.expected_direction, control, treatment, p.minimum_change) for p in predictions]

    def analyze_measurements(self, control: dict[str, Any], treatment: dict[str, Any], predictions: tuple = ()) -> list[dict[str, Any]]:
        configured = {p.metric_id: (p.expected_direction, p.minimum_change) for p in predictions}
        bad_when_higher = {"xact_rollback", "rollback_ratio", "blks_read", "temp_files", "temp_bytes", "deadlocks",
                           "blk_read_time", "blk_write_time", "checkpoint_write_time", "checkpoint_sync_time",
                           "waiting_backends", "idle_in_transaction", "locks_waiting"}
        good_when_higher = {"xact_commit", "buffer_hit_ratio"}
        telemetry_keys: set[str] = set()
        for measurement in (control, treatment):
            telemetry = measurement.get("telemetry", {})
            telemetry_keys.update(telemetry.get("rates_per_second", {}))
            telemetry_keys.update(telemetry.get("gauges", {}))
        result = []
        for metric_id in sorted(telemetry_keys | set(configured)):
            if metric_id in configured:
                direction, threshold = configured[metric_id]
            elif metric_id in bad_when_higher:
                direction, threshold = "decrease", 5.0
            elif metric_id in good_when_higher:
                direction, threshold = "increase", 5.0
            else:
                direction, threshold = None, 5.0
            result.append(self.classify(metric_id, direction, control, treatment, threshold))
        return result
