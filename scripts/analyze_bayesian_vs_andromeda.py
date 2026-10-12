#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from app.phase2.space import FullKnobSpace


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def semantic_config(config: dict[str, Any]) -> dict[str, Any]:
    return FullKnobSpace(config).current_config()


def config_hash(config: dict[str, Any]) -> str:
    value = semantic_config(config)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def percent(value: float, baseline: float) -> float:
    return (value / baseline - 1.0) * 100.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bayesian-dir", type=Path, required=True)
    parser.add_argument("--andromeda-dir", type=Path, required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()

    bo_dir, a_dir = args.bayesian_dir, args.andromeda_dir
    manifest = read_json(bo_dir / "manifest.json")
    baseline = read_json(bo_dir / "baseline.json")
    bo_rows = [json.loads(line) for line in (bo_dir / "trials.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    a_rows = list(csv.DictReader((a_dir / "performance.csv").open(encoding="utf-8")))
    initial_1 = read_json(a_dir / "initial_control.json")["measurement"]["throughput_rps"]
    initial_2 = read_json(a_dir / "initial_control_verification.json")["measurement"]["throughput_rps"]
    a_baseline = min(float(initial_1), float(initial_2))
    bo_baseline = float(baseline["throughput_rps"])
    budget = int(manifest["budget"])
    if len(bo_rows) != budget and not args.allow_incomplete:
        raise SystemExit(f"Bayesian experiment incomplete: {len(bo_rows)}/{budget}")

    a_best, a_curve = a_baseline, []
    for index, row in enumerate(a_rows, 1):
        if row["accepted"] in {"是", "true", "True"}:
            a_best = max(a_best, float(row["candidate_verified_tps"]))
        a_curve.append({"evaluation": index, "best_tps": a_best,
                        "improvement_percent": percent(a_best, a_baseline)})

    bo_best, bo_curve, best_row = bo_baseline, [], None
    invalid = 0
    for row in bo_rows:
        if row.get("valid"):
            if float(row["throughput_rps"]) > bo_best:
                bo_best, best_row = float(row["throughput_rps"]), row
        else:
            invalid += 1
        bo_curve.append({"evaluation": row["evaluation"], "phase": row["phase"],
                         "best_tps": bo_best, "improvement_percent": percent(bo_best, bo_baseline)})

    common = min(len(a_rows), len(bo_rows))
    a_common = a_curve[common - 1] if common else None
    bo_common = bo_curve[common - 1] if common else None
    first_beat = next((item for item in bo_curve if item["best_tps"] >= a_best), None)
    a_source = read_json(a_dir / "source_config.json")["config"]
    bo_source = baseline["config"]
    source_equal = semantic_config(a_source) == semantic_config(bo_source)

    result = {
        "schema_version": "andromeda.bo-comparison/v1",
        "state": "completed" if len(bo_rows) == budget else "interim",
        "fairness": {
            "workload": "twitter", "objective": "maximize throughput_rps", "knob_count": 60,
            "duration_seconds": manifest["duration"], "terminals": manifest["terminals"],
            "same_source_config": source_equal,
            "andromeda_source_config_hash": config_hash(a_source),
            "bayesian_source_config_hash": config_hash(bo_source),
            "same_snapshot_name": "twitter.dump",
            "baseline_measurements_not_comparable_warning": (
                "The two runs used the same semantic source configuration but their initial TPS values differ materially; "
                "report both absolute TPS and within-run normalized improvement."
            ),
        },
        "andromeda": {
            "budget": len(a_rows), "baseline_tps_samples": [initial_1, initial_2],
            "conservative_baseline_tps": a_baseline, "final_best_tps": a_best,
            "final_improvement_percent": percent(a_best, a_baseline), "best_so_far_curve": a_curve,
        },
        "bayesian": {
            "planned_budget": budget, "completed": len(bo_rows), "valid": len(bo_rows) - invalid,
            "invalid": invalid, "initial_lhs": manifest["initial_lhs"],
            "gp_ei_iterations_completed": sum(row["phase"] == "gp" for row in bo_rows),
            "baseline_tps": bo_baseline, "final_best_tps": bo_best,
            "final_improvement_percent": percent(bo_best, bo_baseline),
            "best_evaluation": best_row["evaluation"] if best_row else 0,
            "best_config": best_row["config"] if best_row else baseline["config"],
            "best_so_far_curve": bo_curve,
        },
        "common_budget_comparison": {
            "evaluations": common,
            "andromeda_best_tps": a_common["best_tps"] if a_common else None,
            "bayesian_best_tps": bo_common["best_tps"] if bo_common else None,
            "absolute_tps_difference_bayesian_minus_andromeda": (
                bo_common["best_tps"] - a_common["best_tps"] if common else None),
            "andromeda_within_run_improvement_percent": a_common["improvement_percent"] if a_common else None,
            "bayesian_within_run_improvement_percent": bo_common["improvement_percent"] if bo_common else None,
            "absolute_tps_winner": ("bayesian" if bo_common["best_tps"] > a_common["best_tps"] else
                                    "andromeda" if bo_common["best_tps"] < a_common["best_tps"] else "tie") if common else None,
        },
        "bayesian_vs_andromeda_final": {
            "bayesian_final_best_tps": bo_best, "andromeda_final_best_tps": a_best,
            "absolute_tps_difference_bayesian_minus_andromeda": bo_best - a_best,
            "first_bayesian_evaluation_reaching_andromeda_final_best": first_beat,
        },
    }
    output = bo_dir / ("final_comparison.json" if result["state"] == "completed" else "interim_comparison.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    common_result = result["common_budget_comparison"]
    markdown = ["# Twitter：Andromeda 与 60 旋钮贝叶斯优化对比", "",
        f"- 状态：`{result['state']}`", f"- 共同预算：`{common}` 次",
        f"- Andromeda 共同预算最好 TPS：`{common_result['andromeda_best_tps']}`",
        f"- BO 共同预算最好 TPS：`{common_result['bayesian_best_tps']}`",
        f"- 共同预算绝对 TPS 胜者：`{common_result['absolute_tps_winner']}`",
        f"- BO 当前/最终最好 TPS：`{bo_best}`（第 `{result['bayesian']['best_evaluation']}` 轮）",
        f"- Andromeda 最终最好 TPS：`{a_best}`", "",
        "两次运行使用相同的规范化 60 旋钮源配置和 Twitter 快照名称，但初始 TPS 差异很大。",
        "因此同时报告绝对 TPS 与各自运行内改善率，不能只依据改善百分比判断方法优劣。", "",
        f"机器可读报告：`{output.name}`"]
    (bo_dir / ("FINAL_COMPARISON.md" if result["state"] == "completed" else "INTERIM_COMPARISON.md")).write_text("\n".join(markdown) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "state": result["state"],
                      "common_budget": common_result, "final": result["bayesian_vs_andromeda_final"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
