from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.multi_agent.runner import MultiAgentRunner
from app.phase2.bayesian_baseline import BayesianBaseline, atomic_json
from app.phase2.executor import PostgreSQLKnobExecutor
from app.phase2.space import FullKnobSpace

from .isolation import OracleReferenceStore, assert_diagnosis_context_isolated, evaluate_episode


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkflowOptions:
    suite_id: str
    tpcc_bo_budget: int = 100
    twitter_bo_budget: int = 100
    bo_initial_points: int = 10
    bo_retest_repeats: int = 3
    bo_workloads: tuple[str, ...] = ("tpcc", "twitter")
    directions: tuple[tuple[str, str], ...] = (("tpcc", "twitter"), ("twitter", "tpcc"))
    repetitions: int = 1
    diagnosis_rounds: int = 12
    diagnosis_measurements: int = 60
    duration: int = 120
    warmup_seconds: int = 30
    tpcc_terminals: int = 10
    twitter_terminals: int = 8
    baseline_repeats: int = 3
    source_profile_repeats: int = 1
    max_observation_rounds: int = 2
    seed: int = 20261012
    drift_percent: float = 25.0
    communication_topology: str = "blackboard"
    expert_roles: tuple[str, ...] = ("query_execution", "memory_storage", "concurrency_maintenance")

    def terminals_for(self, workload: str) -> int:
        if workload == "tpcc":
            return self.tpcc_terminals
        if workload == "twitter":
            return self.twitter_terminals
        raise ValueError(f"unsupported workflow workload: {workload}")


class WorkloadSwitchWorkflow:
    def __init__(self, root: Path, options: WorkflowOptions):
        self.root = root
        self.options = options
        self.directory = root / "artifacts" / "workload_switch" / options.suite_id
        self.oracle_root = self.directory / "oracle" / "bo_references"
        self.reference_run_root = self.oracle_root.parent / "runs"
        self.store = OracleReferenceStore(self.oracle_root)
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_json(self.directory / "suite_manifest.json", {
            "schema_version": "andromeda.workload-switch-suite/v1", "suite_id": options.suite_id,
            "created_at": now(), "workloads": ["tpcc", "twitter"],
            "directions": [f"{a}_to_{b}" for a, b in options.directions],
            "budgets": {"bo": {"tpcc": options.tpcc_bo_budget, "twitter": options.twitter_bo_budget},
                        "diagnosis_rounds": options.diagnosis_rounds,
                        "diagnosis_measurements": options.diagnosis_measurements},
            "measurement": {"duration": options.duration, "warmup_seconds": options.warmup_seconds,
                            "terminals_by_workload": {"tpcc": options.tpcc_terminals,
                                                      "twitter": options.twitter_terminals},
                            "rate": "unlimited", "seed": options.seed},
            "oracle_isolation": "target BO references are stored under oracle/ and never loaded by MultiAgentRunner",
            "environment": {"hostname": platform.node(), "machine": platform.machine(),
                            "kernel": platform.release(), "code_revision": self._revision()},
        })

    def _revision(self) -> str | None:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, text=True, capture_output=True)
        return result.stdout.strip() if result.returncode == 0 else None

    def _initial_config(self) -> Path:
        path = self.directory / "bo_initial_config.json"
        if path.exists():
            return path
        executor = PostgreSQLKnobExecutor(self.directory / "bo_initial_capture")
        raw = executor.capture()
        space = FullKnobSpace(raw)
        atomic_json(path, {"config": space.current_config(), "captured_at": now(),
                           "purpose": "common BO initialization only"})
        return path

    def run_bo(self) -> dict[str, Any]:
        initial = self._initial_config()
        summary_path = self.directory / "bo_stage_summary.json"
        results = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        workload_seed_offsets = {"tpcc": 0, "twitter": 1}
        for workload in self.options.bo_workloads:
            budget = self.options.tpcc_bo_budget if workload == "tpcc" else self.options.twitter_bo_budget
            run_id = f"{self.options.suite_id}-{workload}"
            runner = BayesianBaseline(run_id, budget, min(self.options.bo_initial_points, budget - 1),
                self.options.duration, self.options.terminals_for(workload),
                self.options.seed + workload_seed_offsets[workload], initial,
                root=self.reference_run_root, workload=workload, retest_repeats=self.options.bo_retest_repeats,
                warmup_seconds=self.options.warmup_seconds)
            results[workload] = runner.run()
            source = self.reference_run_root / run_id / workload / "reference.json"
            target = self.oracle_root / workload / "reference.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        atomic_json(summary_path, results)
        missing = [workload for workload in ("tpcc", "twitter") if not self.store.path(workload).is_file()]
        if missing:
            raise FileNotFoundError(
                f"BO stage completed requested workloads but references are still missing: {missing}; "
                "run the missing workload before diagnosis")
        self._write_reference_standard()
        return results

    def run_diagnosis(self) -> dict[str, Any]:
        for workload in ("tpcc", "twitter"):
            if not self.store.path(workload).is_file():
                raise FileNotFoundError(f"missing BO reference for {workload}: {self.store.path(workload)}")
        results: list[dict[str, Any]] = []
        for repeat in range(1, self.options.repetitions + 1):
            for direction_index, (source, target) in enumerate(self.options.directions):
                episode_id = f"{self.options.suite_id}-r{repeat:02d}-{source}-to-{target}"
                episode_root = self.directory / "episodes" / f"repeat_{repeat:02d}"
                runner = MultiAgentRunner(self.root, episode_id, source, target,
                    max_rounds=self.options.diagnosis_rounds,
                    max_measurements=self.options.diagnosis_measurements,
                    duration=self.options.duration, warmup_seconds=self.options.warmup_seconds,
                    terminals=self.options.terminals_for(target),
                    source_profile_terminals=self.options.terminals_for(source),
                    seed=self.options.seed + repeat * 100 + direction_index,
                    baseline_repeats=self.options.baseline_repeats, drift_percent=self.options.drift_percent,
                    source_config=self.store.source_config_file(source),
                    source_profile_repeats=self.options.source_profile_repeats,
                    max_observation_rounds=self.options.max_observation_rounds,
                    communication_topology=self.options.communication_topology,
                    expert_roles=self.options.expert_roles, output_root=episode_root)
                report = runner.run()
                episode_dir = episode_root / f"{source}_to_{target}"
                visible = json.loads((episode_dir / "diagnosis_visible_context.json").read_text(encoding="utf-8"))
                assert_diagnosis_context_isolated(visible)
                evaluation = evaluate_episode(report, self.store.load(target))
                evaluation["episode_id"] = episode_id
                evaluation_path = self.directory / "oracle" / "evaluations" / f"repeat_{repeat:02d}_{source}_to_{target}.json"
                atomic_json(evaluation_path, evaluation)
                results.append({"episode_id": episode_id, "direction": f"{source}_to_{target}",
                                "report": str(episode_dir / "report.json"),
                                "oracle_evaluation": str(evaluation_path),
                                "stop_reason": report.get("stop_reason")})
        summary = {"schema_version": "andromeda.workload-switch-diagnosis-summary/v1",
                   "suite_id": self.options.suite_id, "episodes": results, "completed_at": now()}
        atomic_json(self.directory / "diagnosis_stage_summary.json", summary)
        self._write_suite_results(summary)
        return summary

    def _write_reference_standard(self) -> None:
        references = {workload: self.store.load(workload) for workload in ("tpcc", "twitter")}
        payload = {
            "schema_version": "andromeda.bo-reference-standard/v1",
            "suite_id": self.options.suite_id,
            "created_at": now(),
            "objective": "maximize throughput_rps",
            "references": {
                workload: {
                    "verified_tps_min": value["metrics"]["throughput_rps_min"],
                    "verified_tps_median": value["metrics"]["throughput_rps_median"],
                    "selected_config": value["config"],
                    "selection": value["selection"],
                    "search_budget": value["search_budget"],
                    "reference_file": str(self.store.path(workload)),
                }
                for workload, value in references.items()
            },
            "usage": "TPC-C reference supplies the source configuration; Twitter reference is evaluation-only and never enters agent context.",
        }
        atomic_json(self.directory / "REFERENCE_STANDARD.json", payload)
        twitter = payload["references"]["twitter"]
        tpcc = payload["references"]["tpcc"]
        lines = ["# Bayesian optimization reference standard", "",
                 f"- Suite: `{self.options.suite_id}`",
                 f"- TPC-C verified TPS (minimum/median): `{tpcc['verified_tps_min']}` / `{tpcc['verified_tps_median']}`",
                 f"- Twitter verified TPS (minimum/median): `{twitter['verified_tps_min']}` / `{twitter['verified_tps_median']}`",
                 f"- Twitter selected point: `{twitter['selection']}`",
                 f"- Search budget per workload: `{twitter['search_budget']['total']}`", "",
                 "The TPC-C reference configuration is the source configuration for the workload switch. ",
                 "The Twitter value is an offline comparison standard and is not supplied to diagnosis agents.", ""]
        (self.directory / "REFERENCE_STANDARD.md").write_text("\n".join(lines), encoding="utf-8")

    def _write_suite_results(self, diagnosis_summary: dict[str, Any]) -> None:
        rows = []
        for episode in diagnosis_summary["episodes"]:
            evaluation = json.loads(Path(episode["oracle_evaluation"]).read_text(encoding="utf-8"))
            rows.append({**episode, "evaluation": evaluation})
        payload = {"schema_version": "andromeda.workload-switch-final-comparison/v1",
                   "suite_id": self.options.suite_id, "completed_at": now(), "episodes": rows}
        atomic_json(self.directory / "FINAL_COMPARISON.json", payload)
        lines = ["# BO reference versus evidence-graph diagnosis", "",
                 "| Direction | Initial TPS | Final TPS | Target BO reference TPS | Improvement | Gap closed | Stop reason |",
                 "|---|---:|---:|---:|---:|---:|---|"]
        for row in rows:
            value = row["evaluation"]
            lines.append(
                f"| {row['direction']} | {value['episode_initial_tps']:.3f} | "
                f"{value['episode_final_tps']:.3f} | {value['bo_reference_verified_tps']:.3f} | "
                f"{value['relative_improvement_percent'] if value['relative_improvement_percent'] is not None else 'N/A'}% | "
                f"{value['gap_closed_percent'] if value['gap_closed_percent'] is not None else 'N/A'} | "
                f"{row['stop_reason']} |")
        lines.extend(["", "BO target references are read only after diagnosis for offline evaluation.", ""])
        (self.directory / "FINAL_COMPARISON.md").write_text("\n".join(lines), encoding="utf-8")
