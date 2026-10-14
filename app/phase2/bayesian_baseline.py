from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import xml.etree.ElementTree as ET
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.lab.gp import propose
from app.lab.workloads import PostgresBenchBaseAdapter
from app.phase2.executor import PostgreSQLKnobExecutor
from app.phase2.space import FullKnobSpace, latin_hypercube


P95 = "95th Percentile Latency (microseconds)"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def load_source_config(path: Path, space: FullKnobSpace) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    config = raw.get("config", raw)
    missing = sorted(set(space.names) - set(config))
    if missing:
        raise ValueError(f"source configuration is missing {len(missing)} knobs: {missing}")
    return space.reconcile({name: space.clamp(name, config[name]) for name in space.names})


def load_trials(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def andromeda_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    performance = path / "performance.csv"
    if not performance.exists():
        return None
    rows = list(csv.DictReader(performance.open(encoding="utf-8")))
    if not rows:
        return {"artifact_dir": str(path), "completed_experiments": 0}
    initial = float(rows[0]["incumbent_before_tps"])
    best = initial
    curve = []
    for index, row in enumerate(rows, 1):
        if row["accepted"] in {"是", "True", "true"}:
            best = max(best, float(row["candidate_verified_tps"]))
        curve.append({"evaluation": index, "best_tps": best})
    return {
        "artifact_dir": str(path), "completed_experiments": len(rows),
        "initial_tps": initial, "best_tps": best,
        "improvement_percent": (best / initial - 1) * 100 if initial else None,
        "best_so_far_curve": curve,
    }


class BayesianBaseline:
    def __init__(self, experiment_id: str, budget: int, initial_points: int, duration: int,
                 terminals: int, seed: int, source_config: Path,
                 comparison_dir: Path | None = None, root: Path = Path("artifacts/bayesian"),
                 workload: str = "twitter", retest_repeats: int = 3, warmup_seconds: int = 30):
        if budget < 2 or not 1 <= initial_points < budget:
            raise ValueError("require 1 <= initial_points < budget")
        if workload not in {"tpcc", "twitter"}:
            raise ValueError("Bayesian reference workload must be tpcc or twitter")
        if retest_repeats < 1:
            raise ValueError("retest_repeats must be positive")
        self.experiment_id = experiment_id
        self.workload = workload
        self.retest_repeats = retest_repeats
        if warmup_seconds not in {0} and warmup_seconds < 5:
            raise ValueError("warmup_seconds must be zero or at least 5")
        self.warmup_seconds = warmup_seconds
        self.budget = budget
        self.initial_points = initial_points
        self.duration = duration
        self.terminals = terminals
        self.seed = seed
        self.source_config_path = source_config
        self.comparison_dir = comparison_dir
        self.dir = root / experiment_id / workload
        self.dir.mkdir(parents=True, exist_ok=True)
        self.system_dir = self.dir / "system"
        self.trials_path = self.dir / "trials.jsonl"
        self.progress_path = self.dir / "progress.csv"
        self.status_path = self.dir / "status.json"
        self.stop_path = self.dir.parent / "STOP"
        self.executor = PostgreSQLKnobExecutor(self.system_dir)
        self.adapter = PostgresBenchBaseAdapter(workload)
        self.started = time.monotonic()
        self.interrupted = False

    def stop(self, signum: int, _frame: Any) -> None:
        self.interrupted = True

    def measure(self, config: dict[str, Any], space: FullKnobSpace, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
        self.adapter.restore_snapshot()
        applied = self.executor.apply(config, space)
        if self.warmup_seconds:
            self.adapter.runner.run(self.workload, self.warmup_seconds, self.terminals, False,
                                    applied["session_settings"], verbose=False, random_seed=self.seed)
        quiescence = self.executor.quiesce()
        raw = self.adapter.runner.run(self.workload, self.duration, self.terminals, False,
                                      applied["session_settings"], verbose=False, random_seed=self.seed)
        return ({"throughput_rps": float(raw["throughput_rps"]),
                 "goodput_rps": float(raw.get("goodput_rps") or raw["throughput_rps"]),
                 "benchbase_reported_throughput_rps": float(raw.get("benchbase_reported_throughput_rps") or raw["throughput_rps"]),
                 "sql_error_policy": raw.get("sql_error_policy", {}),
                 "p95_us": float(raw["latency"][P95]),
                 "average_us": float(raw["latency"].get("Average Latency (microseconds)", 0) or 0),
                 "p99_us": float(raw["latency"].get("99th Percentile Latency (microseconds)", 0) or 0),
                 "transaction_latency": raw.get("transaction_latency", {}),
                 "pre_measurement_quiescence": quiescence,
                 "client_status": raw.get("status", "unknown"),
                 "artifact": str(Path(raw["summary_file"]).parent), "label": label}, applied)

    def measure_baseline(self, start_config: dict[str, Any], space: FullKnobSpace) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Establish a baseline, expanding SERIALIZABLE lock tables after SQLSTATE 53200."""
        start_config = space.reconcile({name: space.clamp(name, start_config[name]) for name in space.names})
        candidates = [start_config]
        for pred_locks, regular_locks in ((8192, 512), (16384, 1024)):
            candidate = dict(start_config)
            candidate["max_pred_locks_per_transaction"] = space.clamp(
                "max_pred_locks_per_transaction", pred_locks)
            candidate["max_locks_per_transaction"] = space.clamp(
                "max_locks_per_transaction", regular_locks)
            candidate = space.reconcile(candidate)
            if candidate not in candidates:
                candidates.append(candidate)
        failures = []
        for attempt, candidate in enumerate(candidates, 1):
            try:
                measurement, applied = self.measure(candidate, space, f"baseline_attempt_{attempt}")
                measurement["baseline_safety_retries"] = failures
                return measurement, applied, candidate
            except RuntimeError as error:
                message = str(error)
                if "states=['53200']" not in message or "out of shared memory" not in message:
                    raise
                failure = {"attempt": attempt,
                           "max_pred_locks_per_transaction": candidate["max_pred_locks_per_transaction"],
                           "max_locks_per_transaction": candidate["max_locks_per_transaction"],
                           "error": message, "at": now()}
                failures.append(failure)
                append_jsonl(self.dir / "baseline_resource_retries.jsonl", failure)
        raise RuntimeError(
            "Twitter baseline exhausted shared lock memory even at the maximum safe lock-table settings; "
            f"inspect {self.dir / 'baseline_resource_retries.jsonl'}")

    def benchmark_profile(self) -> dict[str, Any]:
        fingerprint = self.adapter.fingerprint()
        path = Path(__file__).resolve().parents[2] / fingerprint["config"]
        root = ET.parse(path).getroot()
        work = root.find("./works/work")
        return {**fingerprint, "scale_factor": root.findtext("scalefactor"),
                "isolation": root.findtext("isolation"), "batch_size": root.findtext("batchsize"),
                "rate": work.findtext("rate") if work is not None else None,
                "weights": work.findtext("weights") if work is not None else None,
                "transaction_types": [node.findtext("name") for node in root.findall("./transactiontypes/transactiontype")]}

    def write_progress(self, trials: list[dict[str, Any]], baseline: dict[str, Any]) -> None:
        valid = [trial for trial in trials if trial.get("valid")]
        best_trial = max(valid, key=lambda item: item["throughput_rps"], default=None)
        best_tps = max(float(baseline["throughput_rps"]),
                       float(best_trial["throughput_rps"]) if best_trial else float("-inf"))
        completed = len(trials)
        lhs_done = sum(item.get("phase") == "lhs" for item in trials)
        gp_done = sum(item.get("phase") == "gp" for item in trials)
        status = {
            "experiment_id": self.experiment_id,
            "state": "stopping" if self.interrupted or self.stop_path.exists() else "running",
            "workload": self.workload, "knob_count": 60, "objective": "maximize throughput_rps",
            "budget": self.budget, "completed": completed, "remaining": self.budget - completed,
            "lhs_completed": lhs_done, "gp_completed": gp_done,
            "baseline_tps": baseline["throughput_rps"], "best_tps": best_tps,
            "improvement_percent": (best_tps / baseline["throughput_rps"] - 1) * 100,
            "best_trial": best_trial["evaluation"] if best_trial and best_tps > baseline["throughput_rps"] else 0,
            "elapsed_seconds": round(time.monotonic() - self.started, 3), "updated_at": now(),
        }
        atomic_json(self.status_path, status)
        comparison = {"generated_at": now(), "bayesian": status,
                      "andromeda": andromeda_summary(self.comparison_dir)}
        common = min(completed, (comparison["andromeda"] or {}).get("completed_experiments", 0))
        if common:
            a_curve = comparison["andromeda"]["best_so_far_curve"]
            bo_best = max([baseline["throughput_rps"],
                           *[item["throughput_rps"] for item in valid if item["evaluation"] <= common]])
            comparison["common_budget"] = {
                "evaluations": common, "andromeda_best_tps": a_curve[common - 1]["best_tps"],
                "bayesian_best_tps": bo_best,
            }
        atomic_json(self.dir / "comparison_live.json", comparison)
        lines = [f"# {self.workload} 60旋钮贝叶斯优化", "",
                 f"- 状态：`{status['state']}`", f"- 已完成：`{completed}/{self.budget}`",
                 f"- 初始化/GP：`{lhs_done}/{gp_done}`", f"- 基线 TPS：`{baseline['throughput_rps']}`",
                 f"- 当前最好 TPS：`{best_tps}`", f"- 改善：`{status['improvement_percent']:.3f}%`",
                 f"- 最好轮次：`{status['best_trial']}`", f"- 更新时间：`{status['updated_at']}`"]
        (self.dir / "LIVE_PROGRESS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def append_progress_row(self, trial: dict[str, Any], best_tps: float) -> None:
        fields = ["evaluation", "phase", "valid", "throughput_rps", "p95_us", "best_tps",
                  "improvement_percent", "expected_improvement", "restart", "restart_seconds",
                  "elapsed_seconds", "error"]
        new = not self.progress_path.exists()
        with self.progress_path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            if new:
                writer.writeheader()
            writer.writerow({name: trial.get(name, "") for name in fields} | {
                "best_tps": best_tps,
                "improvement_percent": trial.get("improvement_percent", ""),
            })

    def run(self) -> dict[str, Any]:
        old = {sig: signal.signal(sig, self.stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        captured = self.executor.capture()
        instrumentation = self.executor.enable_diagnostic_timing()
        space = FullKnobSpace(captured)
        if len(space.names) != 60:
            raise RuntimeError("Bayesian baseline requires exactly 60 knobs")
        start_config = load_source_config(self.source_config_path, space)
        atomic_json(self.dir / "manifest.json", {
            "experiment_id": self.experiment_id, "workload": self.workload, "knob_count": 60,
            "budget": self.budget, "initial_lhs": self.initial_points,
            "gp_ei_iterations": self.budget - self.initial_points,
            "duration": self.duration, "warmup_seconds": self.warmup_seconds,
            "snapshot_settle_seconds": max(0.0, float(os.getenv("SNAPSHOT_SETTLE_SECONDS", "5"))),
            "terminals": self.terminals, "seed": self.seed,
            "source_config": str(self.source_config_path), "comparison_dir": str(self.comparison_dir or ""),
            "objective": {"metric": "throughput_rps", "direction": "maximize",
                          "definition": "successful completed transactions per second (BenchBase Goodput)"},
            "started_at": now(), "knobs": space.describe(),
            "algorithm": {"initial_design": "latin_hypercube", "surrogate": "gaussian_process",
                          "acquisition": "expected_improvement"},
            "environment": {"hostname": platform.node(), "machine": platform.machine(),
                            "kernel": platform.release(),
                            "postgresql_version": self.executor._psql("show server_version"),
                            "workload_profile": self.benchmark_profile(),
                            "snapshot": self.adapter.snapshot.name,
                            "diagnostic_instrumentation": instrumentation},
        })
        baseline_path = self.dir / "baseline.json"
        try:
            if baseline_path.exists():
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            else:
                measurement, applied, start_config = self.measure_baseline(start_config, space)
                baseline = {**measurement, "apply": applied, "config": start_config}
                atomic_json(baseline_path, baseline)
            trials = load_trials(self.trials_path)
            xs = [space.encode(start_config)] + [item["point"] for item in trials]
            ys = [baseline["throughput_rps"]] + [
                item["throughput_rps"] if item.get("valid") else item.get("surrogate_tps", 0.0)
                for item in trials
            ]
            design = latin_hypercube(self.initial_points, 60, self.seed)
            self.write_progress(trials, baseline)
            for index in range(len(trials) + 1, self.budget + 1):
                if self.interrupted or self.stop_path.exists():
                    break
                phase = "lhs" if index <= self.initial_points else "gp"
                if phase == "lhs":
                    point, ei = design[index - 1], None
                else:
                    point, ei = propose(xs, ys, self.seed + index, pool=4096)
                config = space.decode(point)
                trial: dict[str, Any] = {"evaluation": index, "phase": phase, "point": point,
                    "config": config, "expected_improvement": ei, "started_at": now(), "valid": False}
                try:
                    measurement, applied = self.measure(config, space, f"evaluation_{index:03d}")
                    trial.update(measurement)
                    trial.update({"valid": True, "apply": applied,
                                  "restart": bool(applied.get("restart")),
                                  "restart_seconds": applied.get("restart_seconds", 0)})
                    xs.append(point)
                    ys.append(measurement["throughput_rps"])
                except Exception as error:
                    trial["error"] = f"{type(error).__name__}: {error}"
                    # Failed configurations are infeasible observations. Giving
                    # the surrogate a zero-throughput penalty teaches it to avoid
                    # the failed region while keeping the trial out of best-TPS
                    # selection and reference construction.
                    trial["surrogate_tps"] = 0.0
                    xs.append(point)
                    ys.append(0.0)
                trial["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
                best_tps = max(ys)
                trial["improvement_percent"] = (best_tps / baseline["throughput_rps"] - 1) * 100
                append_jsonl(self.trials_path, trial)
                trials.append(trial)
                self.append_progress_row(trial, best_tps)
                self.write_progress(trials, baseline)
            final_state = "interrupted" if len(trials) < self.budget else "completed"
            summary = json.loads(self.status_path.read_text(encoding="utf-8"))
            summary.update({"state": final_state, "completed_at": now()})
            atomic_json(self.status_path, summary)
            reference_runs = 0
            if final_state == "completed":
                reference = self._build_reference(baseline, trials, space)
                summary["reference_file"] = str(self.dir / "reference.json")
                summary["reference_retests"] = self.retest_repeats
                reference_runs = reference["retest_attempts"]
            else:
                summary["reference_file"] = None
                summary["reference_retests"] = 0
            measured_runs = 1 + len(trials) + reference_runs
            summary["benchmark_measurements"] = measured_runs
            summary["warmup_runs"] = measured_runs if self.warmup_seconds else 0
            summary["total_benchmark_executions"] = measured_runs * (2 if self.warmup_seconds else 1)
            atomic_json(self.dir / "summary.json", summary)
            return summary
        finally:
            try:
                atomic_json(self.system_dir / "final_rollback.json", self.executor.restore())
            finally:
                for sig, handler in old.items():
                    signal.signal(sig, handler)

    def _build_reference(self, baseline: dict[str, Any], trials: list[dict[str, Any]],
                         space: FullKnobSpace) -> dict[str, Any]:
        valid = sorted((item for item in trials if item.get("valid")),
                       key=lambda item: float(item["throughput_rps"]), reverse=True)
        candidates = [
            (item["config"], {"kind": "trial", "evaluation": item["evaluation"]})
            for item in valid if float(item["throughput_rps"]) > float(baseline["throughput_rps"])
        ]
        candidates.append((baseline["config"], {"kind": "baseline", "evaluation": 0}))
        attempts, rejected = 0, []
        repeats: list[dict[str, Any]] = []
        config: dict[str, Any] = {}
        origin: dict[str, Any] = {}
        for candidate_config, candidate_origin in candidates:
            candidate_repeats = []
            try:
                for index in range(1, self.retest_repeats + 1):
                    attempts += 1
                    measurement, applied = self.measure(
                        candidate_config, space,
                        f"reference_{candidate_origin['kind']}_{candidate_origin['evaluation']}_retest_{index}")
                    candidate_repeats.append({**measurement, "apply": applied})
            except Exception as error:
                rejection = {"selection": candidate_origin,
                             "completed_retests": len(candidate_repeats),
                             "error": f"{type(error).__name__}: {error}", "at": now()}
                rejected.append(rejection)
                append_jsonl(self.dir / "reference_candidate_rejections.jsonl", rejection)
                continue
            config, origin, repeats = candidate_config, candidate_origin, candidate_repeats
            break
        if not repeats:
            raise RuntimeError("no BO candidate, including baseline, passed all reference retests")
        tps = [float(item["throughput_rps"]) for item in repeats]
        p95 = [float(item["p95_us"]) for item in repeats]
        reference = {
            "schema_version": "andromeda.bo-reference/v1", "workload": self.workload,
            "definition": "best measured configuration within the declared search space and budget",
            "experiment_id": self.experiment_id, "config": config, "selection": origin,
            "retest_attempts": attempts, "rejected_reference_candidates": rejected,
            "search_budget": {"total": self.budget, "lhs": self.initial_points,
                              "gp_ei": self.budget - self.initial_points},
            "measurement_protocol": {"duration": self.duration, "warmup_seconds": self.warmup_seconds,
                                     "terminals": self.terminals,
                                     "seed": self.seed, "repeats": self.retest_repeats,
                                     "snapshot_restored_before_each_run": True},
            "retests": repeats,
            "metrics": {"throughput_rps_values": tps, "throughput_rps_min": min(tps),
                        "throughput_rps_median": sorted(tps)[len(tps)//2],
                        "p95_us_values": p95, "p95_us_max": max(p95)},
            "created_at": now(),
        }
        atomic_json(self.dir / "reference.json", reference)
        return reference


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable 60-knob workload-specific Bayesian optimization reference")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--budget", type=int, default=100)
    parser.add_argument("--initial-points", type=int, default=8)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--terminals", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261009)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--comparison-dir", type=Path)
    parser.add_argument("--workload", choices=("tpcc", "twitter"), default="twitter")
    parser.add_argument("--retest-repeats", type=int, default=3)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    args = parser.parse_args()
    result = BayesianBaseline(args.experiment_id, args.budget, args.initial_points, args.duration,
                              args.terminals, args.seed, args.source_config, args.comparison_dir,
                              workload=args.workload, retest_repeats=args.retest_repeats,
                              warmup_seconds=args.warmup_seconds).run()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
