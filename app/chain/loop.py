from __future__ import annotations

import hashlib
import csv
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.lab.workloads import PostgresBenchBaseAdapter
from app.phase2.executor import PostgreSQLKnobExecutor
from app.phase2.space import FullKnobSpace
from app.phase2.telemetry import DiagnosticSampler, normalize_by_transactions, summarize
from .agent import SingleAgentChainDiagnoser, assert_no_online_leakage
from .analyzer import BottleneckFrontierAnalyzer
from .incumbent import IncumbentPolicy, IncumbentState, verified_performance
from .planner import ExperimentPlanner
from .session import SequentialGraphSession
from .validator import validate_contract
from .models import utc_now
from .plan_evidence import collect_plan_evidence
from .source_config import load_source_config


STOP_REASONS = {
    "no_actionable_hypothesis", "no_further_observed_improvement", "budget_exhausted",
    "restart_budget_exhausted", "wall_clock_exhausted", "blocked_by_model",
    "blocked_by_execution", "stopped_by_user",
}


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _append(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def _hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class ChainRunOptions:
    mode: str = "formal"
    duration: int = 30
    warmup: int = 30
    terminals: int = 2
    max_experiments: int = 12
    max_restarts: int = 60
    max_wall_seconds: int = 7200
    min_tps_percent: float = 5.0
    patience: int = 3
    seed: int = 20260929
    validation_mode: str = "paired_independent_retest"
    source_config_dir: str | None = None
    max_p95_regression_percent: float = 10.0
    max_rollback_ratio_increase_pp: float = 1.0
    min_screening_groups: int = 4

    def __post_init__(self):
        if self.mode not in {"quick", "formal"}:
            raise ValueError("mode must be quick or formal")
        if self.validation_mode != "paired_independent_retest":
            raise ValueError("v3/v4 supports only paired_independent_retest")
        if self.duration < 5 or (0 < self.warmup < 5) or self.warmup < 0 or self.max_experiments < 1:
            raise ValueError("invalid run budget")
        if self.min_screening_groups < 1:
            raise ValueError("group screening budget must be positive")


class ChainValidationLoop:
    def __init__(self, experiment_id: str, source: str, target: str, options: ChainRunOptions,
                 root: Path = Path("."), agent: Any | None = None, adapter_factory=PostgresBenchBaseAdapter,
                 executor_factory=PostgreSQLKnobExecutor):
        if source == target or source not in {"tpcc", "twitter", "ycsb"} or target not in {"tpcc", "twitter", "ycsb"}:
            raise ValueError("invalid workload direction")
        self.experiment_id, self.source, self.target, self.options = experiment_id, source, target, options
        self.root = root
        self.direction = f"{source}_to_{target}"
        self.dir = root / "artifacts" / "v3_v4" / experiment_id / self.direction
        self.dir.mkdir(parents=True, exist_ok=True)
        self.system_dir = self.dir / "system"
        self.agent = agent
        self.adapter_factory, self.executor_factory = adapter_factory, executor_factory
        self.started = time.monotonic()
        self.experiments = self.restarts = self.no_improvement = 0
        self.measurement_runs = 0
        self.history: list[dict[str, Any]] = []
        self.tested_groups: set[str] = set()
        self.tested_group_signatures: set[str] = set()
        self.ranked_group_backlog: list[str] = []
        self.progress: dict[str, Any] = {"initial_tps": None, "incumbent_tps": None, "control_tps": None,
                                        "treatment_tps": None, "active_hypothesis": None, "frontier": None,
                                        "last_conclusion": None, "accepted_changes": [], "rollback": None,
                                        "candidate_edges": 0, "supported_edges": 0, "unresolved_evidence": []}

    def _status(self, state: str, **extra: Any) -> None:
        value = {"experiment_id": self.experiment_id, "direction": self.direction, "state": state,
                 "updated_at": utc_now(), "experiments": self.experiments, "restarts": self.restarts, **extra}
        _write(self.dir / "status.json", value)
        lines = [f"# {self.direction}", "", f"- 状态：`{state}`", f"- 已完成干预：`{self.experiments}`",
                 f"- PostgreSQL 重启：`{self.restarts}`", f"- 更新时间：`{value['updated_at']}`"]
        initial, incumbent = self.progress["initial_tps"], self.progress["incumbent_tps"]
        improvement = None if initial in (None, 0) or incumbent is None else (incumbent - initial) / abs(initial) * 100
        lines.extend([
            f"- 初始 TPS：`{initial}`", f"- 当前 incumbent TPS：`{incumbent}`",
            f"- 本次 control TPS：`{self.progress['control_tps']}`", f"- 本次 treatment TPS：`{self.progress['treatment_tps']}`",
            f"- 相对初始 TPS 改善：`{improvement}`", f"- active hypothesis：`{self.progress['active_hypothesis']}`",
            f"- validation frontier：`{self.progress['frontier']}`", f"- 最近结论：`{self.progress['last_conclusion']}`",
            f"- 已接受配置变化：`{self.progress['accepted_changes']}`",
            f"- 候选边/支持边：`{self.progress['candidate_edges']}/{self.progress['supported_edges']}`",
            f"- 未解决证据：`{self.progress['unresolved_evidence']}`",
            f"- 剩余干预算：`{max(0, self.options.max_experiments-self.experiments)}`",
            f"- 回滚状态：`{self.progress['rollback']}`",
        ])
        if extra.get("message"):
            lines.append(f"- 当前说明：{extra['message']}")
        (self.dir / "LIVE_PROGRESS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _budget_stop(self) -> str | None:
        if (self.dir.parent / "STOP").exists() or (self.dir / "STOP").exists():
            return "stopped_by_user"
        if self.experiments >= self.options.max_experiments:
            return "budget_exhausted"
        if self.restarts >= self.options.max_restarts:
            return "restart_budget_exhausted"
        if time.monotonic() - self.started >= self.options.max_wall_seconds:
            return "wall_clock_exhausted"
        return None

    def _model_usage(self) -> dict[str, Any]:
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        calls = 0
        for path in (self.dir / "llm").glob("*_response_metadata_*.json"):
            try:
                usage = json.loads(path.read_text(encoding="utf-8")).get("usage") or {}
            except (OSError, json.JSONDecodeError):
                continue
            calls += 1
            for key in totals:
                totals[key] += int(usage.get(key, 0) or 0)
        return {"calls": calls, **totals, "cost": None,
                "cost_reason": "not computed because no versioned price snapshot is stored"}

    def _measure(self, adapter: PostgresBenchBaseAdapter, config: dict[str, Any], apply_result: dict[str, Any], label: str) -> dict[str, Any]:
        self.measurement_runs += 1
        adapter.restore_snapshot()
        if self.options.warmup:
            adapter.warmup(self.options.warmup, self.options.terminals, self.options.seed)
        sampler = DiagnosticSampler(adapter.name).start()
        try:
            raw = adapter.run(self.options.duration, self.options.terminals, config, self.options.seed)
        finally:
            samples = sampler.stop()
        parsed = adapter.parse_result(raw)
        measurement = {**parsed, "telemetry": normalize_by_transactions(summarize(samples), parsed["throughput_rps"]), "healthy": True,
                       "benchmark_artifact": raw.get("summary_file"), "benchmark_run_id": raw.get("run_id"), "label": label,
                       "measured_at": utc_now(), "apply_result": apply_result}
        measurement["plan_evidence"] = collect_plan_evidence(adapter.name)
        return measurement

    def _state(self, state_id: str, workload: str, config: dict[str, Any], measurement: dict[str, Any],
               workload_fingerprints: dict[str, Any] | None = None) -> dict[str, Any]:
        value = {"state_id": state_id, "workload": workload, "config": config, "config_hash": _hash(config),
                 "measurement": measurement, "workload_fingerprints": workload_fingerprints or {}, "at": utc_now()}
        assert_no_online_leakage(value)
        _append(self.dir / "states.jsonl", value)
        return value

    @staticmethod
    def _activation_confirmed(apply_result: dict[str, Any], config: dict[str, Any], knobs: list[str], space: FullKnobSpace) -> bool:
        dimensions = {item.name: item for item in space.dimensions}
        for knob in knobs:
            expected = str(config[knob]).lower()
            if dimensions[knob].execution_class == "session":
                actual = apply_result.get("session_settings", {}).get(knob)
            else:
                actual = apply_result.get("observed", {}).get(knob, {}).get("setting")
            if actual is None or str(actual).lower() != expected:
                return False
        return True

    def _initialize_results(self, initial: dict[str, Any], incumbent: IncumbentState) -> None:
        (self.dir / "RESULTS.md").write_text(
            f"# {self.direction} 性能结果\n\n"
            f"初始目标负载单次 TPS：**{initial['measurement']['throughput_rps']:.3f}**  "
            f"当前 verified incumbent TPS：**{incumbent.tps:.3f}**\n\n"
            "| 步骤 | 实际旋钮变化 | Incumbent before | C1 | T1 | C2 | T2 | Candidate verified | 相对 incumbent | 性能判定 | 机制判定 | 接受 |\n"
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|\n", encoding="utf-8")
        with (self.dir / "performance.csv").open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerow(["experiment_id", "knob_changes", "incumbent_before_tps", "control_1_tps",
                "treatment_1_tps", "control_2_tps", "treatment_2_tps", "candidate_verified_tps",
                "candidate_vs_incumbent_percent", "performance_verdict", "mechanism_verdict", "accepted", "reason"])
        with (self.dir / "knob_group_effects.csv").open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerow(["experiment_id", "group_name", "group_size", "knob_changes",
                "coupling_rationale", "local_change_percent", "retest_local_change_percent",
                "candidate_vs_incumbent_percent", "performance_verdict", "mechanism_verdict", "accepted"])

    def _append_result(self, experiment_id: str, knob_changes: dict[str, Any], control: dict[str, Any],
                       treatment: dict[str, Any], retest_control: dict[str, Any], retest_treatment: dict[str, Any],
                       decision: dict[str, Any]) -> None:
        knobs = "; ".join(f"{name}={value}" for name, value in knob_changes.items())
        row = [experiment_id, knobs, f"{decision['incumbent_before_tps']:.3f}", f"{control['throughput_rps']:.3f}",
               f"{treatment['throughput_rps']:.3f}", f"{retest_control['throughput_rps']:.3f}",
               f"{retest_treatment['throughput_rps']:.3f}", f"{decision['candidate_verified_performance']['throughput_rps']:.3f}",
               f"{decision['candidate_vs_incumbent_percent']:.3f}%", decision["performance_verdict"],
               decision["mechanism_verdict"], "是" if decision["accepted"] else "否", decision["reason"]]
        with (self.dir / "performance.csv").open("a", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerow(row)
        with (self.dir / "RESULTS.md").open("a", encoding="utf-8") as stream:
            stream.write("| " + " | ".join(str(value).replace("|", "/") for value in row) + " |\n")

    def run(self) -> dict[str, Any]:
        self._status("starting", message="capturing experiment-start PostgreSQL configuration")
        _write(self.dir / "manifest.json", {"experiment_id": self.experiment_id, "direction": self.direction,
                                             "validation_mode": self.options.validation_mode, "options": self.options.__dict__,
                                             "started_at": utc_now()})
        executor = self.executor_factory(self.system_dir)
        start_raw = executor.capture()
        space = FullKnobSpace(start_raw)
        captured_config = space.current_config()
        source_raw, source_origin = load_source_config(self.root, self.source, captured_config, self.options.source_config_dir)
        start_config = space.reconcile(source_raw)
        incumbent_config = dict(start_config)
        _write(self.dir / "source_config.json", {"workload": self.source, "config": start_config,
               "config_hash": _hash(start_config), **source_origin})
        session = SequentialGraphSession(self.dir, f"{self.direction} sequential diagnosis")
        agent = self.agent or SingleAgentChainDiagnoser(self.dir)
        stop_reason = "blocked_by_execution"
        failure: str | None = None
        initial: dict[str, Any] | None = None
        incumbent: IncumbentState | None = None
        try:
            source_adapter = self.adapter_factory(self.source)
            target_adapter = self.adapter_factory(self.target)
            fingerprints = {self.source: source_adapter.fingerprint(), self.target: target_adapter.fingerprint()}
            apply_result = executor.apply(start_config, space)
            self.restarts += int(bool(apply_result.get("restart")))
            self._status("measuring_source", message=f"warming up and measuring {self.source} steady state")
            source_state = self._state("state:source_steady", self.source, start_config,
                                      self._measure(source_adapter, start_config, apply_result, "source_steady"), fingerprints)
            _write(self.dir / "source_steady.json", source_state)
            self._status("measuring_initial_control", message=f"restoring {self.target} snapshot and measuring initial control twice")
            initial = self._state("state:target_initial_control", self.target, start_config,
                                  self._measure(target_adapter, start_config, apply_result, "target_initial_control"), fingerprints)
            initial_verify_measurement = self._measure(target_adapter, start_config, apply_result, "target_initial_verification")
            initial_verify = self._state("state:target_initial_verification", self.target, start_config,
                                         initial_verify_measurement, fingerprints)
            _write(self.dir / "initial_control.json", initial)
            _write(self.dir / "initial_control_verification.json", initial_verify)
            incumbent = IncumbentState(config=dict(start_config), config_hash=_hash(start_config),
                verified_performance=verified_performance([initial["measurement"], initial_verify_measurement]),
                accepted_experiment_id="initial", accepted_at=utc_now(),
                evidence_summary={"reason": "two initial target measurements"},
                raw_run_records=[initial["measurement"], initial_verify_measurement],
                telemetry_snapshot=initial_verify_measurement.get("telemetry", {}),
                workload_fingerprint=fingerprints[self.target],
                database_state={"snapshot": f"{self.target}.dump", "source_config_hash": _hash(start_config)},
                experiment_conditions={"duration": self.options.duration, "warmup": self.options.warmup,
                    "terminals": self.options.terminals, "seed": self.options.seed})
            _write(self.dir / "incumbent.json", incumbent.model_dump())
            self._initialize_results(initial, incumbent)
            self.progress["initial_tps"] = initial["measurement"]["throughput_rps"]
            self.progress["incumbent_tps"] = incumbent.tps
            current_state = initial_verify
            current_state["incumbent"] = incumbent.model_dump()
            current_state["initial_target_performance"] = initial["measurement"]
            active_hypothesis = None
            consecutive_invalid_actions = 0
            _append(self.dir / "incumbent_history.jsonl", {"at": utc_now(), "state_id": initial_verify["state_id"],
                    **incumbent.model_dump(), "reason": "initial target control independently repeated"})
            step = 1
            while not (reason := self._budget_stop()):
                self._status("diagnosing", message=f"ranking high-impact mechanism groups for step {step}")
                screening_status = {
                    "tested_groups": sorted(self.tested_groups),
                    "unique_groups_tested": len(self.tested_groups),
                    "minimum_unique_groups": self.options.min_screening_groups,
                    "minimum_coverage_reached": len(self.tested_groups) >= self.options.min_screening_groups,
                    "ranked_group_backlog": [name for name in self.ranked_group_backlog
                                             if name not in self.tested_groups],
                }
                try:
                    decision = agent.decide(step, self.source, self.target, current_state, self.history, space,
                                            active_hypothesis.model_dump() if active_hypothesis else None,
                                            {"experiments": self.options.max_experiments - self.experiments,
                                             "restarts": self.options.max_restarts - self.restarts,
                                             "wall_seconds": max(0, self.options.max_wall_seconds - int(time.monotonic() - self.started)),
                                             "screening_status": screening_status},
                                            session.graph.model_dump(), session.graph.effective().model_dump())
                except Exception as error:
                    failure = f"{type(error).__name__}: {error}"
                    reason = "blocked_by_model"
                    break
                for name in decision.ranked_candidate_groups:
                    if name not in self.ranked_group_backlog:
                        self.ranked_group_backlog.append(name)
                if self._budget_stop() == "stopped_by_user":
                    reason = "stopped_by_user"
                    break
                if decision.hypothesis_action == "stop":
                    reason = decision.stop_reason or "no_actionable_hypothesis"
                    break
                if decision.hypothesis_action in {"abandon", "close_hypothesis"}:
                    if active_hypothesis is None:
                        failure = "model tried to close a hypothesis when none was active"
                        reason = "blocked_by_model"
                        break
                    active_hypothesis.status = "abandoned" if decision.hypothesis_action == "abandon" else "closed"
                    active_hypothesis.closure_reason = decision.result_interpretation
                    session.record_hypothesis(active_hypothesis, "hypothesis_closed")
                    self.history.append({"step": step, "hypothesis_id": active_hypothesis.hypothesis_id,
                                         "action": decision.hypothesis_action, "reason": decision.result_interpretation})
                    active_hypothesis = None
                    step += 1
                    continue
                try:
                    bundle = agent.materialize(decision, step, self.direction, current_state["state_id"], current_state,
                                               space, self.options.duration, self.options.warmup, self.options.terminals,
                                               self.options.seed + step, self.options.min_tps_percent, active_hypothesis)
                except Exception as error:
                    consecutive_invalid_actions += 1
                    rejected = {"step": step, "action": "invalid_contract_rejected", "error_type": type(error).__name__,
                                "reason": str(error), "budget_consumed": False}
                    self.history.append(rejected)
                    _append(self.dir / "model_action_rejections.jsonl", {"at": utc_now(), **rejected})
                    if consecutive_invalid_actions >= 3:
                        failure = f"{type(error).__name__}: three consecutive invalid model contracts: {error}"
                        reason = "blocked_by_model"
                        break
                    self.progress["last_conclusion"] = f"模型动作未改变实际配置，未执行、未消耗预算：{error}"
                    self._status("diagnosing", message="rejected a no-op/invalid action; requesting a corrected contract")
                    step += 1
                    continue
                consecutive_invalid_actions = 0
                active_hypothesis = bundle["hypothesis"]
                contract, frontier = bundle["contract"], bundle["frontier"]
                group_signature = json.dumps(
                    sorted((action.knob, str(action.proposed_value).lower())
                           for action in contract.knob_actions), separators=(",", ":"))
                if group_signature in self.tested_group_signatures:
                    consecutive_invalid_actions += 1
                    rejected = {"step": step, "action": "duplicate_group_configuration_rejected",
                                "group_name": decision.group_name, "signature": group_signature,
                                "budget_consumed": False}
                    self.history.append(rejected)
                    _append(self.dir / "model_action_rejections.jsonl", {"at": utc_now(), **rejected})
                    self.progress["last_conclusion"] = "重复旋钮组配置未执行、未消耗预算"
                    step += 1
                    continue
                self.progress["active_hypothesis"] = active_hypothesis.hypothesis_id
                self.progress["frontier"] = frontier.frontier_id
                _write(self.dir / "contracts" / f"{contract.contract_id.replace(':', '_')}.json", contract.model_dump())
                session.propose_step(f"step_{step:03d}", bundle["nodes"], bundle["edges"], contract.contract_id,
                                     bundle["hypothesis"], frontier)
                try:
                    plans = ExperimentPlanner().plan(contract, incumbent_config, self.options.max_experiments - self.experiments)
                except RuntimeError:
                    reason = "budget_exhausted"
                    break
                for plan_index, plan in enumerate(plans, 1):
                    if self._budget_stop() == "stopped_by_user":
                        reason = "stopped_by_user"
                        break
                    experiment_id = f"experiment:{step:03d}:{plan_index:02d}"
                    exp_dir = self.dir / "experiments" / experiment_id.replace(":", "_")
                    exp_dir.mkdir(parents=True, exist_ok=True)
                    self._status("running_control", message=experiment_id)
                    control_apply = executor.apply(incumbent_config, space)
                    self.restarts += int(bool(control_apply.get("restart")))
                    control = self._measure(target_adapter, incumbent_config, control_apply, f"{experiment_id}:control")
                    self.progress["control_tps"] = control["throughput_rps"]
                    if self._budget_stop() == "stopped_by_user":
                        reason = "stopped_by_user"
                        break
                    self._status("running_treatment", message=experiment_id)
                    requested = plan.config
                    reconciled = space.reconcile(requested)
                    reconciliation = {k: {"requested": requested.get(k), "actual": v} for k, v in reconciled.items() if requested.get(k) != v}
                    actual_changed_knobs = [k for k, v in reconciled.items() if str(incumbent_config.get(k)).lower() != str(v).lower()]
                    if not actual_changed_knobs:
                        raise RuntimeError("validated treatment has no effective PostgreSQL knob change")
                    treatment_apply = executor.apply(reconciled, space)
                    self.restarts += int(bool(treatment_apply.get("restart")))
                    treatment = self._measure(target_adapter, reconciled, treatment_apply, f"{experiment_id}:treatment")
                    self.progress["treatment_tps"] = treatment["throughput_rps"]
                    # Independent alternating repeat: C1 -> T1 -> C2 -> T2.  Every
                    # measurement restores the same target snapshot in _measure.
                    self._status("running_retest_control", message=experiment_id)
                    retest_control_apply = executor.apply(incumbent_config, space)
                    self.restarts += int(bool(retest_control_apply.get("restart")))
                    retest_control = self._measure(target_adapter, incumbent_config, retest_control_apply,
                                                   f"{experiment_id}:retest_control")
                    self._status("running_retest_treatment", message=experiment_id)
                    retest_treatment_apply = executor.apply(reconciled, space)
                    self.restarts += int(bool(retest_treatment_apply.get("restart")))
                    retest_treatment = self._measure(target_adapter, reconciled, retest_treatment_apply,
                                                     f"{experiment_id}:retest_treatment")
                    validation = validate_contract(contract, control, treatment)
                    transitions = BottleneckFrontierAnalyzer().analyze_measurements(
                        control, treatment, (*contract.direct_predictions, *contract.intermediate_predictions))
                    decision_result = IncumbentPolicy().decide(control, treatment, validation, treatment_apply,
                        self.options.min_tps_percent, incumbent=incumbent, retest_control=retest_control,
                        retest_treatment=retest_treatment,
                        activation_confirmed=self._activation_confirmed(treatment_apply, reconciled, actual_changed_knobs, space),
                        max_p95_regression_percent=self.options.max_p95_regression_percent,
                        max_rollback_ratio_increase_pp=self.options.max_rollback_ratio_increase_pp)
                    changed_values = {name: reconciled[name] for name in actual_changed_knobs}
                    self._append_result(experiment_id, changed_values, control, treatment, retest_control,
                                        retest_treatment, decision_result)
                    before_state_id = current_state["state_id"]
                    experiment = {"experiment_id": experiment_id, "contract_id": contract.contract_id, "frontier_id": frontier.frontier_id,
                                  "plan": {"label": plan.label, "changed_knobs": plan.changed_knobs, "actual_changed_knobs": actual_changed_knobs}, "requested_config": requested,
                                  "actual_config": reconciled, "reconciliation_changes": reconciliation, "control": control,
                                  "treatment": treatment, "retest_control": retest_control,
                                  "retest_treatment": retest_treatment, "validation": validation, "bottleneck_transitions": transitions,
                                  "incumbent_decision": decision_result, "completed_at": utc_now()}
                    _write(exp_dir / "result.json", experiment)
                    _append(self.dir / "experiments.jsonl", experiment)
                    self.experiments += 1
                    self.tested_groups.add(decision.group_name)
                    self.tested_group_signatures.add(group_signature)
                    group_effect = {
                        "at": utc_now(), "experiment_id": experiment_id,
                        "group_name": decision.group_name, "group_size": len(actual_changed_knobs),
                        "knob_changes": {name: {"old": incumbent_config[name], "new": reconciled[name]}
                                         for name in actual_changed_knobs},
                        "coupling_rationale": decision.coupling_rationale,
                        "local_change_percent": decision_result["local_change_percent"],
                        "retest_local_change_percent": decision_result["retest_local_change_percent"],
                        "candidate_vs_incumbent_percent": decision_result["candidate_vs_incumbent_percent"],
                        "performance_verdict": decision_result["performance_verdict"],
                        "mechanism_verdict": decision_result["mechanism_verdict"],
                        "accepted": decision_result["accepted"],
                    }
                    _append(self.dir / "knob_group_effects.jsonl", group_effect)
                    with (self.dir / "knob_group_effects.csv").open("a", newline="", encoding="utf-8") as stream:
                        csv.writer(stream).writerow([experiment_id, decision.group_name,
                            group_effect["group_size"], json.dumps(group_effect["knob_changes"], ensure_ascii=False),
                            decision.coupling_rationale, decision_result["local_change_percent"],
                            decision_result["retest_local_change_percent"],
                            decision_result["candidate_vs_incumbent_percent"],
                            decision_result["performance_verdict"], decision_result["mechanism_verdict"],
                            decision_result["accepted"]])
                    session.resolve_step(f"step_{step:03d}", validation["edge_updates"], experiment_id, frontier)
                    self.progress["candidate_edges"] = len(session.active_edges)
                    self.progress["supported_edges"] = sum(e.status == "supported" for e in session.graph.edges)
                    self.progress["unresolved_evidence"] = [e.edge_id for e in session.active_edges]
                    active_hypothesis.experiment_ids.append(experiment_id)
                    for edge_id, update in validation["edge_updates"].items():
                        if edge_id in active_hypothesis.candidate_edge_ids:
                            active_hypothesis.candidate_edge_ids.remove(edge_id)
                        destination = active_hypothesis.supported_edge_ids if update["status"] == "supported" else active_hypothesis.refuted_edge_ids if update["status"] == "refuted" else None
                        if destination is not None and edge_id not in destination:
                            destination.append(edge_id)
                    if decision_result["accepted"]:
                        incumbent_config = reconciled
                        old_incumbent_tps = incumbent.tps
                        incumbent = IncumbentState(config=dict(reconciled), config_hash=_hash(reconciled),
                            verified_performance=decision_result["candidate_verified_performance"],
                            accepted_experiment_id=experiment_id, accepted_at=utc_now(),
                            evidence_summary={"performance_verdict": decision_result["performance_verdict"],
                                "mechanism_verdict": decision_result["mechanism_verdict"],
                                "unexplained_performance": decision_result["unexplained_performance"]},
                            raw_run_records=[treatment, retest_treatment],
                            telemetry_snapshot=retest_treatment.get("telemetry", {}),
                            effective_chain=session.graph.effective().model_dump(),
                            workload_fingerprint=fingerprints[self.target],
                            database_state={"snapshot": f"{self.target}.dump", "source_config_hash": _hash(start_config)},
                            experiment_conditions={"duration": self.options.duration, "warmup": self.options.warmup,
                                "terminals": self.options.terminals, "seed": self.options.seed + step})
                        if incumbent.tps < old_incumbent_tps:
                            raise AssertionError("verified incumbent TPS must be monotonic nondecreasing")
                        _write(self.dir / "incumbent.json", incumbent.model_dump())
                        self.no_improvement = 0
                        current_state = self._state(f"state:incumbent_{step:03d}", self.target, incumbent_config,
                                                    retest_treatment, fingerprints)
                        active_hypothesis.accepted_intervention_ids.append(experiment_id)
                        self.progress["incumbent_tps"] = incumbent.tps
                        self.progress["accepted_changes"].append({k: reconciled[k] for k in plan.changed_knobs})
                    else:
                        rollback_apply = executor.apply(incumbent_config, space)
                        self.restarts += int(bool(rollback_apply.get("restart")))
                        recovery = self._measure(target_adapter, incumbent_config, rollback_apply,
                                                 f"{experiment_id}:incumbent_recovery_verification")
                        if not recovery.get("healthy"):
                            raise RuntimeError("incumbent recovery measurement was unhealthy")
                        selected_metrics = {p.metric_id for p in (*contract.direct_predictions, *contract.intermediate_predictions)}
                        worth_following = any(t["metric_id"] in selected_metrics and t["classification"] in {"emerged", "worsened"} for t in transitions)
                        self.no_improvement = 0 if worth_following else self.no_improvement + 1
                        current_state = self._state(f"state:control_{step:03d}", self.target, incumbent_config,
                                                    recovery, fingerprints)
                    current_state["incumbent"] = incumbent.model_dump()
                    current_state["initial_target_performance"] = initial["measurement"]
                    active_hypothesis.current_state_id = current_state["state_id"]
                    active_hypothesis.status = "partially_supported" if active_hypothesis.supported_edge_ids else "testing"
                    session.record_hypothesis(active_hypothesis, "hypothesis_updated")
                    classifications = {item["metric_id"]: item["classification"] for item in transitions}
                    transition_event = {"before_state_id": before_state_id,
                                        "after_state_id": current_state["state_id"], "experiment_id": experiment_id,
                                        "resolved_metric_ids": [k for k,v in classifications.items() if v == "resolved"],
                                        "persistent_metric_ids": [k for k,v in classifications.items() if v in {"persistent","unchanged"}],
                                        "emerged_metric_ids": [k for k,v in classifications.items() if v == "emerged"],
                                        "worsened_metric_ids": [k for k,v in classifications.items() if v == "worsened"],
                                        "candidate_bottleneck_frontier": [k for k,v in classifications.items() if v in {"persistent","worsened","emerged"}],
                                        "details": transitions}
                    _append(self.dir / "bottleneck_transitions.jsonl", transition_event)
                    event = {"step": step, "hypothesis_id": bundle["hypothesis"].hypothesis_id,
                             "frontier_id": frontier.frontier_id, "experiment_id": experiment_id,
                             "group_name": decision.group_name,
                             "knob_group": [item.model_dump() for item in decision.knob_group],
                             "actual_knob_changes": changed_values,
                             "ranked_candidate_groups": decision.ranked_candidate_groups,
                             "accepted": decision_result["accepted"], "reason": decision_result["reason"],
                             "performance_verdict": decision_result["performance_verdict"],
                             "mechanism_verdict": decision_result["mechanism_verdict"],
                             "tps_change_percent": decision_result["tps_change_percent"], "transitions": transitions,
                             "control_result": {"throughput_rps": control["throughput_rps"], "p95_us": control["p95_us"], "telemetry": control["telemetry"]},
                             "treatment_result": {"throughput_rps": treatment["throughput_rps"], "p95_us": treatment["p95_us"], "telemetry": treatment["telemetry"]},
                             "retest_control_result": retest_control, "retest_treatment_result": retest_treatment}
                    self.history.append(event)
                    self.progress["last_conclusion"] = decision_result["reason"]
                    _append(self.dir / "incumbent_history.jsonl", {"at": utc_now(), "state_id": current_state["state_id"],
                            "config_hash": current_state["config_hash"], **event})
                step += 1
            stop_reason = reason or "blocked_by_execution"
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            stop_reason = "blocked_by_execution"
        finally:
            try:
                rollback = executor.restore()
            except Exception as error:
                rollback = {"restored": False, "error_type": type(error).__name__, "message": str(error)}
            _write(self.system_dir / "final_rollback.json", rollback)
            self.progress["rollback"] = rollback
        if stop_reason not in STOP_REASONS:
            stop_reason = "blocked_by_execution"
        unresolved = [edge.model_dump() for edge in session.graph.edges if edge.status == "candidate"]
        _write(self.dir / "unresolved_hypotheses.json", {"candidate_edges": unresolved})
        _write(self.dir / "unresolved_evidence.json", {"candidate_edges": unresolved,
                                                        "statement": "unresolved under the current observations, action space, and budget"})
        initial_measurement = initial["measurement"] if initial else {}
        incumbent_dump = incumbent.model_dump() if incumbent else None
        incumbent_tps = incumbent.tps if incumbent else None
        report = {"schema_version": "andromeda.single-agent-chain/v3", "experiment_id": self.experiment_id,
                  "direction": self.direction, "validation_mode": self.options.validation_mode,
                  "stop_reason": stop_reason, "failure": failure, "experiments": self.experiments,
                  "restarts": self.restarts, "final_incumbent_config_hash": _hash(incumbent_config),
                  "source_config_hash": _hash(start_config), "source_config_origin": source_origin,
                  "initial_target_performance": initial_measurement,
                  "current_incumbent": incumbent_dump,
                  "final_incumbent_config": incumbent_config,
                  "measurement_protocol": "initial C0/C0-repeat; each candidate C1/T1/C2/T2 with snapshot restore",
                  "search_protocol": {"method": "diagnosis_ranked_coupled_group_screening",
                      "one_group_configuration_per_experiment": True,
                      "minimum_unique_groups": self.options.min_screening_groups,
                      "tested_groups": sorted(self.tested_groups),
                      "tested_group_signatures": sorted(self.tested_group_signatures),
                      "remaining_ranked_group_backlog": [name for name in self.ranked_group_backlog
                                                          if name not in self.tested_groups]},
                  "total_measurement_runs": self.measurement_runs,
                  "elapsed_seconds": round(time.monotonic() - self.started, 3),
                  "model_usage": self._model_usage(),
                  "relative_to_initial_percent": ((incumbent_tps - initial_measurement["throughput_rps"]) /
                      abs(initial_measurement["throughput_rps"]) * 100.0
                      if incumbent_tps is not None and initial_measurement.get("throughput_rps") else None),
                  "start_config_restored": bool(rollback.get("restored")), "generated_at": utc_now(),
                  "supported_edges": [e.edge_id for e in session.graph.edges if e.status == "supported"],
                  "unresolved_edge_count": len(unresolved)}
        _write(self.dir / "final_chain.json", session.graph.model_dump())
        _write(self.dir / "chain_report.json", report)
        (self.dir / "chain_report.md").write_text(
            f"# {self.direction} 单智能体诊断链报告\n\n"
            f"- 停止原因：`{stop_reason}`\n- 有效干预：`{self.experiments}`\n- 支持边：`{len(report['supported_edges'])}`\n"
            f"- 未解决边：`{len(unresolved)}`\n- 初始目标 TPS：`{initial_measurement.get('throughput_rps')}`\n"
            f"- 最终 verified incumbent TPS：`{incumbent_tps}`\n- 源配置哈希：`{_hash(start_config)}`\n"
            f"- 已恢复实验开始配置：`{report['start_config_restored']}`\n\n"
            "结论只适用于当前观测、动作空间、成对独立复测协议和预算；不表示达到全局最优。\n", encoding="utf-8")
        self._status("completed" if not failure else "blocked", stop_reason=stop_reason, message=failure or stop_reason)
        return report


def create_manifest(root: Path, experiment_id: str, directions: list[tuple[str, str]], options: ChainRunOptions) -> Path:
    base = root / "artifacts" / "v3_v4" / experiment_id
    base.mkdir(parents=True, exist_ok=True)
    manifest = {"experiment_id": experiment_id, "created_at": utc_now(), "pid": os.getpid(),
                "directions": [f"{a}_to_{b}" for a, b in directions], "options": options.__dict__}
    _write(base / "manifest.json", manifest)
    (root / "artifacts" / "current_experiment_id.txt").write_text(experiment_id + "\n", encoding="utf-8")
    (root / "artifacts" / "current_v3_v4_experiment_id.txt").write_text(experiment_id + "\n", encoding="utf-8")
    return base


def new_experiment_id() -> str:
    return "chain-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
