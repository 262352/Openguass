from __future__ import annotations

import csv
import json
import signal
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.lab.gp import propose
from app.lab.workloads import PostgresBenchBaseAdapter
from app.phase2.executor import PostgreSQLKnobExecutor
from app.phase2.llm import DiagnosticLLM, retrieve_catalog
from app.phase2.space import FullKnobSpace, latin_hypercube
from app.phase2.telemetry import DiagnosticSampler, summarize


P95 = "95th Percentile Latency (microseconds)"


def _atomic(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class TransitionComparison:
    def __init__(self, experiment_id: str, source: str, target: str, budget: int, duration: int, terminals: int, seed: int = 20260921):
        if source == target or source not in {"tpcc", "twitter", "ycsb"} or target not in {"tpcc", "twitter", "ycsb"}:
            raise ValueError(f"invalid transition {source}->{target}")
        if budget < 2:
            raise ValueError("budget must be at least 2 evaluations per method")
        self.id, self.source, self.target = experiment_id, source, target
        self.budget, self.duration, self.terminals, self.seed = budget, duration, terminals, seed
        self.dir = Path("artifacts/phase2") / experiment_id / f"{source}_to_{target}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.executor = PostgreSQLKnobExecutor(self.dir / "system")
        self.source_adapter = PostgresBenchBaseAdapter(source)
        self.target_adapter = PostgresBenchBaseAdapter(target)
        self.progress_csv = self.dir / "progress.csv"
        self.started = time.monotonic()

    def _measure(self, adapter: PostgresBenchBaseAdapter, config: dict[str, Any], space: FullKnobSpace, restore_snapshot: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        if restore_snapshot:
            adapter.restore_snapshot()
        applied = self.executor.apply(config, space)
        sampler = DiagnosticSampler(adapter.name).start()
        try:
            raw = adapter.runner.run(adapter.name, self.duration, self.terminals, False, applied["session_settings"], verbose=False)
        finally:
            samples = sampler.stop()
        measurement = {
            "throughput_rps": float(raw["throughput_rps"]),
            "p95_us": float(raw["latency"][P95]),
            "artifact": str(Path(raw["summary_file"]).parent),
            "telemetry": summarize(samples),
            "telemetry_samples": samples,
        }
        return measurement, applied

    def _append(self, row: dict[str, Any]) -> None:
        fields = ["timestamp", "method", "evaluation", "valid", "tps", "best_tps", "elapsed_seconds", "restart", "restart_seconds", "changed_knobs", "accepted_as_best", "error"]
        new = not self.progress_csv.exists()
        with self.progress_csv.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            if new:
                writer.writeheader()
            writer.writerow({name: row.get(name, "") for name in fields})
        self._write_markdown()

    def _write_markdown(self) -> None:
        rows = list(csv.DictReader(self.progress_csv.open(encoding="utf-8")))
        latest = {}
        for row in rows:
            latest[row["method"]] = row
        lines = [f"# {self.source} → {self.target}", "", "唯一优化目标：目标负载 TPS。", "", "| 方法 | 已完成评估 | 当前 TPS | 历史最好 TPS | 接受为当前最好 | 已用秒数 | 最近状态 |", "|---|---:|---:|---:|---|---:|---|"]
        for method in ("andromeda", "gp"):
            row = latest.get(method)
            if row:
                status = "valid" if row["valid"] == "True" else "invalid"
                lines.append(f"| {method} | {row['evaluation']}/{self.budget} | {row['tps'] or '-'} | {row['best_tps'] or '-'} | {row['accepted_as_best']} | {row['elapsed_seconds']} | {status} |")
            else:
                lines.append(f"| {method} | 0/{self.budget} | - | - | - | 0 | pending |")
        lines += ["", f"机器可读明细：`{self.progress_csv}`", "", "延迟、锁、WAL、缓存等只用于诊断，不参与目标函数。"]
        (self.dir / "LIVE_PROGRESS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _trial(self, method: str, index: int, config: dict[str, Any], point: list[float], space: FullKnobSpace, method_started: float, changed: list[str], diagnosis: dict[str, Any] | None = None, incumbent_tps: float | None = None) -> dict[str, Any]:
        directory = self.dir / method
        directory.mkdir(exist_ok=True)
        record: dict[str, Any] = {
            "method": method, "evaluation": index, "normalized": dict(zip(space.names, point)),
            "config": config, "changed_knobs": changed, "diagnosis": diagnosis,
            "started_at": datetime.now(timezone.utc).isoformat(), "valid": False,
        }
        try:
            measurement, applied = self._measure(self.target_adapter, config, space, True)
            samples = measurement.pop("telemetry_samples")
            _atomic(directory / f"evaluation_{index:03d}_telemetry_samples.json", samples)
            record.update({"valid": True, "measurement": measurement, "apply": applied, "tps": measurement["throughput_rps"]})
            record["accepted_as_incumbent"] = incumbent_tps is None or record["tps"] > incumbent_tps
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            record["tps"] = None
            record["accepted_as_incumbent"] = False
        record["elapsed_seconds"] = round(time.monotonic() - method_started, 3)
        _atomic(directory / f"evaluation_{index:03d}.json", record)
        with (directory / "trials.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    @staticmethod
    def _visible_measurement(prefix: str, measurement: dict[str, Any]) -> dict[str, Any]:
        return {
            "evidence_id": prefix,
            "throughput_rps": measurement["throughput_rps"],
            "p95_us_diagnostic_only": measurement["p95_us"],
            "telemetry": measurement["telemetry"],
        }

    def _andromeda(self, space: FullKnobSpace, start_config: dict[str, Any], source_measurement: dict[str, Any], baseline: dict[str, Any]) -> list[dict[str, Any]]:
        current = dict(start_config)
        current_measurement = baseline
        incumbent_tps = baseline["throughput_rps"]
        rows = []
        method_started = time.monotonic()
        llm = DiagnosticLLM(self.dir / "andromeda" / "llm")
        all_knobs = space.describe()
        for index in range(1, self.budget + 1):
            visible = {
                "objective": {"metric": "target_workload_throughput_rps", "direction": "maximize", "only_objective": True},
                "transition": f"{self.source}->{self.target}",
                "source_steady": self._visible_measurement("source_steady", source_measurement),
                "target_current": self._visible_measurement(f"target_evaluation_{index-1}", current_measurement),
                "current_config": current,
                "iteration": index,
            }
            changed, diagnosis_data = [], None
            candidate = dict(current)
            try:
                narrative = llm.compose(index, visible)
                retrieved = retrieve_catalog(narrative)
                diagnosis = llm.diagnose(index, {
                    "objective": visible["objective"], "transition": visible["transition"],
                    "metric_narrative": narrative.model_dump(mode="json"), "current_config": current,
                    "all_60_allowed_knobs": all_knobs, "retrieved_official_postgresql_14_knowledge": retrieved,
                }, space)
                diagnosis_data = diagnosis.model_dump(mode="json")
                if diagnosis.decision == "change":
                    for change in diagnosis.changes:
                        value = space.clamp(change.knob, change.value)
                        if candidate[change.knob] != value:
                            candidate[change.knob] = value
                            changed.append(change.knob)
                    candidate = space.reconcile(candidate)
            except Exception as error:
                diagnosis_data = {"decision": "llm_error_no_change", "error": f"{type(error).__name__}: {error}"}
            point = space.encode(candidate)
            record = self._trial("andromeda", index, candidate, point, space, method_started, changed, diagnosis_data, incumbent_tps)
            if record["valid"] and record["accepted_as_incumbent"]:
                current = dict(candidate)
                current_measurement = record["measurement"]
                incumbent_tps = record["tps"]
            rows.append(record)
            valid_tps = [r["tps"] for r in rows if r["valid"]]
            best = max([baseline["throughput_rps"], *valid_tps])
            self._append({"timestamp": datetime.now(timezone.utc).isoformat(), "method": "andromeda", "evaluation": index, "valid": record["valid"], "tps": record.get("tps"), "best_tps": best, "elapsed_seconds": record["elapsed_seconds"], "restart": record.get("apply", {}).get("restart", False), "restart_seconds": record.get("apply", {}).get("restart_seconds", 0), "changed_knobs": ";".join(changed), "accepted_as_best": record["accepted_as_incumbent"], "error": record.get("error", "")})
        return rows

    def _gp(self, space: FullKnobSpace, start_config: dict[str, Any], baseline: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        method_started = time.monotonic()
        xs = [space.encode(start_config)]
        ys = [baseline["throughput_rps"]]
        initial = latin_hypercube(min(8, self.budget), 60, self.seed)
        for index in range(1, self.budget + 1):
            point = initial[index - 1] if index <= len(initial) else propose(xs, ys, self.seed + index, pool=4096)[0]
            config = space.decode(point)
            changed = [name for name in space.names if config[name] != start_config[name]]
            record = self._trial("gp", index, config, point, space, method_started, changed, incumbent_tps=max(ys))
            rows.append(record)
            if record["valid"]:
                xs.append(point)
                ys.append(record["tps"])
            best = max(ys)
            self._append({"timestamp": datetime.now(timezone.utc).isoformat(), "method": "gp", "evaluation": index, "valid": record["valid"], "tps": record.get("tps"), "best_tps": best, "elapsed_seconds": record["elapsed_seconds"], "restart": record.get("apply", {}).get("restart", False), "restart_seconds": record.get("apply", {}).get("restart_seconds", 0), "changed_knobs": str(len(changed)), "accepted_as_best": record["accepted_as_incumbent"], "error": record.get("error", "")})
        return rows

    @staticmethod
    def _method_summary(rows: list[dict[str, Any]], baseline_tps: float, reference: float) -> dict[str, Any]:
        best = baseline_tps
        curve = [{"evaluation": 0, "elapsed_seconds": 0.0, "best_tps": best}]
        invalid = restarts = 0
        restart_seconds = 0.0
        for row in rows:
            if row["valid"]:
                best = max(best, row["tps"])
            else:
                invalid += 1
            restarts += int(row.get("apply", {}).get("restart", False))
            restart_seconds += float(row.get("apply", {}).get("restart_seconds", 0))
            curve.append({"evaluation": row["evaluation"], "elapsed_seconds": row["elapsed_seconds"], "best_tps": best})
        total = curve[-1]["elapsed_seconds"] or 1.0
        area = sum((a["best_tps"] + b["best_tps"]) * 0.5 * (b["elapsed_seconds"] - a["elapsed_seconds"]) for a, b in zip(curve, curve[1:]))
        def reached(fraction: float):
            return next(({"evaluation": x["evaluation"], "elapsed_seconds": x["elapsed_seconds"]} for x in curve if x["best_tps"] >= reference * fraction), None)
        return {"best_tps": best, "improvement_pct": 100 * (best / baseline_tps - 1), "best_so_far_curve": curve, "mean_best_tps_over_time": area / total, "time_to_90pct_posthoc_best": reached(0.90), "time_to_95pct_posthoc_best": reached(0.95), "invalid_trials": invalid, "restart_count": restarts, "restart_seconds": restart_seconds, "wall_seconds": curve[-1]["elapsed_seconds"]}

    def run(self) -> dict[str, Any]:
        old_handlers = {}
        def interrupted(signum, _frame):
            raise KeyboardInterrupt(f"signal {signum}")
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, interrupted)
        status = {"experiment_id": self.id, "transition": f"{self.source}->{self.target}", "state": "running"}
        _atomic(self.dir / "status.json", status)
        try:
            current_raw = self.executor.capture()
            space = FullKnobSpace(current_raw)
            start_config = space.current_config()
            _atomic(self.dir / "knob_space.json", {"count": len(space.names), "knobs": space.describe()})
            source_measurement, _ = self._measure(self.source_adapter, start_config, space, True)
            source_samples = source_measurement.pop("telemetry_samples")
            _atomic(self.dir / "source_steady.json", source_measurement)
            _atomic(self.dir / "source_steady_telemetry_samples.json", source_samples)
            baseline, _ = self._measure(self.target_adapter, start_config, space, True)
            baseline_samples = baseline.pop("telemetry_samples")
            _atomic(self.dir / "target_baseline.json", baseline)
            _atomic(self.dir / "target_baseline_telemetry_samples.json", baseline_samples)

            self.executor.restore()
            andromeda = self._andromeda(space, start_config, source_measurement, baseline)
            self.executor.restore()
            gp = self._gp(space, start_config, baseline)
            valid = [baseline["throughput_rps"]] + [r["tps"] for r in andromeda + gp if r["valid"]]
            posthoc_best = max(valid)
            report = {
                "experiment_id": self.id, "transition": f"{self.source}->{self.target}",
                "objective": {"metric": "target_workload_throughput_rps", "direction": "maximize", "other_metrics": "diagnostic_only"},
                "fairness": {"same_start_config": True, "same_snapshot": True, "same_budget_per_method": self.budget, "same_duration_seconds": self.duration, "same_terminals": self.terminals, "restart_time_charged": True},
                "knob_count": 60, "source_config": start_config, "target_baseline_tps": baseline["throughput_rps"],
                "posthoc_union_best_tps": posthoc_best,
                "methods": {"andromeda": self._method_summary(andromeda, baseline["throughput_rps"], posthoc_best), "gp": self._method_summary(gp, baseline["throughput_rps"], posthoc_best)},
                "state": "completed", "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic(self.dir / "comparison_report.json", report)
            status.update({"state": "completed", "report": str(self.dir / "comparison_report.json")})
            _atomic(self.dir / "status.json", status)
            return report
        except BaseException as error:
            status.update({"state": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed", "error": f"{type(error).__name__}: {error}"})
            _atomic(self.dir / "status.json", status)
            raise
        finally:
            try:
                self.executor.restore()
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)

