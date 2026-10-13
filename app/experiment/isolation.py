from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ORACLE_KEY_FRAGMENTS = ("bo_reference", "target_reference", "oracle", "reference_tps", "gap_to_reference")


def assert_diagnosis_context_isolated(value: Any) -> None:
    """Fail closed when oracle/evaluation fields enter model-visible context."""
    def visit(item: Any, path: str = "root") -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                lowered = str(key).lower()
                if any(fragment in lowered for fragment in ORACLE_KEY_FRAGMENTS):
                    raise ValueError(f"oracle field is forbidden in diagnosis context: {path}.{key}")
                visit(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
    visit(value)


class OracleReferenceStore:
    """Offline evaluator-only BO reference reader."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, workload: str) -> Path:
        return self.root / workload / "reference.json"

    def load(self, workload: str) -> dict[str, Any]:
        value = json.loads(self.path(workload).read_text(encoding="utf-8"))
        if value.get("workload") != workload or "config" not in value or "metrics" not in value:
            raise ValueError(f"invalid BO reference for {workload}")
        return value

    def source_config_file(self, workload: str) -> Path:
        return self.path(workload)


def evaluate_episode(report: dict[str, Any], target_reference: dict[str, Any]) -> dict[str, Any]:
    initial = report.get("episode_initial_tps") or report["incumbent"]["verified_performance"].get("initial_throughput_rps")
    final = float(report["incumbent"]["verified_performance"]["throughput_rps"])
    reference = float(target_reference["metrics"]["throughput_rps_min"])
    if initial is None:
        initial = final
    initial = float(initial)
    initial_gap = reference - initial
    final_gap = reference - final
    return {"schema_version": "andromeda.oracle-evaluation/v1", "workload": target_reference["workload"],
            "episode_initial_tps": initial, "episode_final_tps": final,
            "bo_reference_verified_tps": reference,
            "absolute_improvement": final - initial,
            "relative_improvement_percent": (final / initial - 1) * 100 if initial else None,
            "initial_gap_to_bo_reference": initial_gap, "final_gap_to_bo_reference": final_gap,
            "gap_closed_percent": ((initial_gap - final_gap) / initial_gap * 100) if initial_gap else None,
            "visibility": "offline evaluator only; never supplied to diagnosis agents"}
