from __future__ import annotations

import argparse
import csv
import json
import os
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
                 comparison_dir: Path | None = None, root: Path = Path("artifacts/bayesian")):
        if budget < 2 or not 1 <= initial_points < budget:
            raise ValueError("require 1 <= initial_points < budget")
        self.experiment_id = experiment_id
        self.budget = budget
        self.initial_points = initial_points
        self.duration = duration
        self.terminals = terminals
        self.seed = seed
        self.source_config_path = source_config
        self.comparison_dir = comparison_dir
        self.dir = root / experiment_id / "twitter"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.system_dir = self.dir / "system"
        self.trials_path = self.dir / "trials.jsonl"
        self.progress_path = self.dir / "progress.csv"
        self.status_path = self.dir / "status.json"
        self.stop_path = self.dir.parent / "STOP"
        self.executor = PostgreSQLKnobExecutor(self.system_dir)
        self.adapter = PostgresBenchBaseAdapter("twitter")
        self.started = time.monotonic()
        self.interrupted = False

    def stop(self, signum: int, _frame: Any) -> None:
        self.interrupted = True

    def measure(self, config: dict[str, Any], space: FullKnobSpace, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
        self.adapter.restore_snapshot()
        applied = self.executor.apply(config, space)
        raw = self.adapter.runner.run("twitter", self.duration, self.terminals, False,
                                      applied["session_settings"], verbose=False)
        return ({"throughput_rps": float(raw["throughput_rps"]),
                 "p95_us": float(raw["latency"][P95]),
                 "artifact": str(Path(raw["summary_file"]).parent), "label": label}, applied)

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
            "workload": "twitter", "knob_count": 60, "objective": "maximize throughput_rps",
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
        lines = ["# Twitter 60旋钮贝叶斯优化", "",
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
        space = FullKnobSpace(captured)
        if len(space.names) != 60:
            raise RuntimeError("Bayesian baseline requires exactly 60 knobs")
        start_config = load_source_config(self.source_config_path, space)
        atomic_json(self.dir / "manifest.json", {
            "experiment_id": self.experiment_id, "workload": "twitter", "knob_count": 60,
            "budget": self.budget, "initial_lhs": self.initial_points,
            "gp_ei_iterations": self.budget - self.initial_points,
            "duration": self.duration, "terminals": self.terminals, "seed": self.seed,
            "source_config": str(self.source_config_path), "comparison_dir": str(self.comparison_dir or ""),
            "objective": {"metric": "throughput_rps", "direction": "maximize"},
            "started_at": now(), "knobs": space.describe(),
        })
        baseline_path = self.dir / "baseline.json"
        try:
            if baseline_path.exists():
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            else:
                measurement, applied = self.measure(start_config, space, "baseline")
                baseline = {**measurement, "apply": applied, "config": start_config}
                atomic_json(baseline_path, baseline)
            trials = load_trials(self.trials_path)
            xs = [space.encode(start_config)] + [item["point"] for item in trials if item.get("valid")]
            ys = [baseline["throughput_rps"]] + [item["throughput_rps"] for item in trials if item.get("valid")]
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
            atomic_json(self.dir / "summary.json", summary)
            return summary
        finally:
            try:
                atomic_json(self.system_dir / "final_rollback.json", self.executor.restore())
            finally:
                for sig, handler in old.items():
                    signal.signal(sig, handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable 60-knob Twitter Bayesian optimization baseline")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--budget", type=int, default=100)
    parser.add_argument("--initial-points", type=int, default=8)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--terminals", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261009)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--comparison-dir", type=Path)
    args = parser.parse_args()
    result = BayesianBaseline(args.experiment_id, args.budget, args.initial_points, args.duration,
                              args.terminals, args.seed, args.source_config, args.comparison_dir).run()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
