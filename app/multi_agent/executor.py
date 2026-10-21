from __future__ import annotations

import hashlib
import random
import json
import os
import platform
import statistics
import time
from pathlib import Path
from typing import Any

from app.chain.plan_evidence import collect_plan_evidence
from app.lab.workloads import PostgresBenchBaseAdapter
from app.phase2.executor import PostgreSQLKnobExecutor
from app.phase2.space import FullKnobSpace
from app.phase2.telemetry import DiagnosticSampler, normalize_by_transactions, summarize
from app.continuous.query_evidence import PgStatStatementsWindow
from .models import ExpertProposal
from .paired_decision import PairedDecision, SequentialPairedPolicy, is_three_sigma_outlier, log_gain


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


class EnvironmentProbe:
    def __init__(self, executor: PostgreSQLKnobExecutor):
        self.executor = executor

    def inspect(self) -> dict[str, Any]:
        postgres_version = self.executor._psql("show server_version")
        data_directory = self.executor._psql("show data_directory")
        cpu_count = os.cpu_count()
        memory_bytes = None
        try:
            pages, page_size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
            memory_bytes = pages * page_size
        except (ValueError, OSError):
            pass
        return {"hostname": platform.node(), "machine": platform.machine(), "kernel": platform.release(),
                "cpu_count": cpu_count, "memory_bytes": memory_bytes, "postgresql_version": postgres_version,
                "data_directory_hash": hashlib.sha256(data_directory.encode()).hexdigest(),
                "cluster_version": self.executor.version, "cluster_name": self.executor.cluster}


class ExperimentExecutor:
    """Only this object may mutate PostgreSQL; all runs are serialized and auditable."""

    def __init__(self, directory: Path, workload: str, *, duration: int, warmup_seconds: int, terminals: int, seed: int):
        self.directory = directory
        self.system_dir = directory / "system"
        self.executor = PostgreSQLKnobExecutor(self.system_dir, database=workload)
        self.adapter = PostgresBenchBaseAdapter(workload)
        self.space = FullKnobSpace()
        self.workload = workload
        self.duration, self.warmup_seconds, self.terminals, self.seed = duration, warmup_seconds, terminals, seed
        self.measurement_count = 0
        self.paired_blocks = max(2, int(os.getenv("PAIRED_BLOCKS", "3")))
        self.delta_min = float(os.getenv("PAIRED_DELTA_MIN", "0.03"))
        self.tps_history: dict[str, list[float]] = {}
        self.warmup_count = 0
        self.restart_count = 0
        self.apply_count = 0
        self.experiment_context: dict[str, Any] = {}
        self.query_evidence = PgStatStatementsWindow(
            self.executor._psql,
            top_n=int(os.getenv("QUERY_EVIDENCE_TOP_N", "10")),
            wait_interval=float(os.getenv("QUERY_WAIT_SAMPLE_SECONDS", "1")),
        )

    def capture(self) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = self.executor.capture()
        self.executor.enable_diagnostic_timing()
        query_preflight = self.query_evidence.prepare()
        (self.system_dir / "query_evidence_preflight.json").write_text(
            json.dumps(query_preflight, ensure_ascii=False, indent=2), encoding="utf-8")
        self.space = FullKnobSpace(raw)
        config = self.space.current_config()
        return config, EnvironmentProbe(self.executor).inspect()

    def context(self, source_config_hash: str, hardware: dict[str, Any]) -> dict[str, Any]:
        fingerprint = self.adapter.fingerprint()
        snapshot = self.adapter.snapshot
        snapshot_identity = {"path_name": snapshot.name, "sha256": self._file_hash(snapshot)}
        result = {"source_config_hash": source_config_hash, "workload_fingerprint": fingerprint,
                "snapshot": snapshot_identity, "hardware": hardware, "duration": self.duration,
                "warmup_seconds": self.warmup_seconds, "snapshot_settle_seconds":
                max(0.0, float(os.getenv("SNAPSHOT_SETTLE_SECONDS", "5"))),
                "terminals": self.terminals, "seed": self.seed,
                "diagnostic_instrumentation": self.executor.diagnostic_instrumentation}
        self.experiment_context = result
        return result

    @staticmethod
    def _file_hash(path: Path) -> str | None:
        if not path.is_file(): return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""): digest.update(block)
        return digest.hexdigest()

    def measure(self, config: dict[str, Any], label: str, *,
                duration_override: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        apply_result = self.executor.apply(config, self.space)
        self.apply_count += 1
        self.restart_count += int(bool(apply_result.get("restart")))
        restore_result = self.adapter.restore_snapshot()
        if self.warmup_seconds:
            self.adapter.warmup(self.warmup_seconds, self.terminals, seed=self.seed)
            self.warmup_count += 1
        quiescence = self.executor.quiesce()
        query_window = self.query_evidence.begin()
        sampler = DiagnosticSampler(self.workload).start()
        started = time.monotonic()
        try:
            run_duration = duration_override or self.duration
            raw = self.adapter.run(run_duration, self.terminals, config=config, seed=self.seed)
        finally:
            samples = sampler.stop()
            query_evidence = self.query_evidence.finish(query_window)
        parsed = self.adapter.parse_result(raw)
        policy = raw.get("sql_error_policy", {})
        successful_transactions = int(policy.get("completed_transactions", 0) or 0)
        transaction_attempts = int(policy.get("transaction_attempts", 0) or 0)
        final_failures = int(policy.get("aborted_transactions", 0) or 0) \
            + int(policy.get("nonretryable_sql_errors", 0) or 0)
        self.measurement_count += 1
        retry_ratio = float(policy.get("server_retry_ratio", 0.0) or 0.0)
        normalized_retry_ratio = retry_ratio / 100.0 if retry_ratio > 1.0 else retry_ratio
        regime = ("storm" if normalized_retry_ratio >= 0.50 else
                  "clean" if normalized_retry_ratio <= 0.10 else "transition")
        measurement = {**parsed, "healthy": True, "label": label, "run_id": raw.get("run_id"),
            "elapsed_seconds": time.monotonic() - started,
            "telemetry": normalize_by_transactions(summarize(samples), parsed["throughput_rps"]),
            "query_evidence": query_evidence,
            "plan_evidence": collect_plan_evidence(self.workload),
            "client": {"failed": 0, "timeouts": 0,
                       "objective": "successful completed transaction Goodput",
                       "successful_transactions": successful_transactions,
                       "completed_transactions_by_type": {
                           name: int(values.get("count", 0) or 0)
                           for name, values in parsed.get("transaction_latency", {}).items()},
                       "transaction_attempts": transaction_attempts,
                       "aborted_transactions": int(policy.get("aborted_transactions", 0) or 0),
                       "final_failures": final_failures,
                       "reconnect_retry_transactions": int(policy.get("reconnect_retry_transactions", 0) or 0),
                       "successful_transaction_tps": raw.get("goodput_rps", parsed.get("throughput_rps")),
                       "attempted_transaction_tps": transaction_attempts / max(float(duration_override or self.duration), 1.0),
                       "counter_window": {"scope": "single_benchbase_run_histograms",
                                          "run_id": raw.get("run_id"),
                                          "duration_seconds": duration_override or self.duration,
                                          "cumulative_postgresql_counters": False},
                       "unexpected_sql_errors": policy.get("nonretryable_sql_errors", 0),
                       "retryable_database_aborts": (
                           policy.get("retryable_sql_errors", 0)
                           + policy.get("server_retry_transactions", 0)),
                       "server_retry_transactions": policy.get("server_retry_transactions", 0),
                       "server_retry_ratio": policy.get("server_retry_ratio", 0.0),
                       "regime": regime,
                       "regime_basis": {"metric": "server_retry_ratio",
                                        "normalized_value": normalized_retry_ratio,
                                        "clean_max": 0.10, "storm_min": 0.50},
                       "unexpected_error_ratio": policy.get("unexpected_error_ratio", 0),
                       "status": raw.get("status"), "goodput_rps": raw.get("goodput_rps"),
                       "benchbase_reported_throughput_rps": raw.get("benchbase_reported_throughput_rps"),
                       "average_latency_us": parsed.get("average_us"), "p99_latency_us": parsed.get("p99_us"),
                       "max_latency_us": parsed.get("max_us"),
                       "transaction_latency": parsed.get("transaction_latency", {}),
                       "bottleneck_summary": self._client_bottleneck(parsed)},
            "database_state": {"snapshot": self.adapter.snapshot.name, "restored_before_run": True,
                               "snapshot_restore": restore_result, "pre_measurement_quiescence": quiescence},
            "experiment_context": dict(self.experiment_context),
            "requested_config_hash": stable_hash(config)}
        target = self.directory / "measurements" / f"{self.measurement_count:04d}_{label}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"measurement": measurement, "apply": apply_result}, indent=2), encoding="utf-8")
        self.tps_history.setdefault(stable_hash(config), []).append(float(measurement["throughput_rps"]))
        return measurement, apply_result

    @staticmethod
    def _client_bottleneck(parsed: dict[str, Any]) -> dict[str, Any]:
        transactions = parsed.get("transaction_latency", {})
        if not transactions:
            return {"detected": False, "reason": "transaction-level latency unavailable"}
        name, evidence = max(transactions.items(), key=lambda item: float(item[1].get("terminal_capacity_fraction", 0) or 0))
        capacity = float(evidence.get("terminal_capacity_fraction", 0) or 0)
        transaction_p50 = float(evidence.get("p50_us", 0) or 0)
        overall_p95 = float(parsed.get("p95_us", 0) or 0)
        masked = transaction_p50 > max(overall_p95 * 10.0, 10_000.0)
        return {"detected": capacity >= 0.5, "critical_transaction": name,
                "terminal_capacity_fraction": capacity, "transaction_p50_us": transaction_p50,
                "transaction_p99_us": evidence.get("p99_us"), "overall_p95_us": overall_p95,
                "tail_masked_by_overall_p95": masked,
                "reason": "one transaction type consumes at least half of total client terminal capacity" if capacity >= 0.5
                          else "no transaction type consumes half of terminal capacity"}

    def run_paired(self, incumbent: dict[str, Any], candidate: dict[str, Any], round_number: int,
                   *, duration_override: int | None = None,
                   minimum_blocks: int = 1) -> dict[str, Any]:
        """Run synchronous blocks and stop as soon as the sequential policy resolves."""
        rng = random.Random(self.seed + round_number)
        policy = SequentialPairedPolicy(delta_min=self.delta_min,
                                        max_blocks=min(3, self.paired_blocks))
        order: list[tuple[str, dict[str, Any]]] = []
        values: dict[str, dict[str, Any]] = {}
        applies: dict[str, dict[str, Any]] = {}
        block_records: list[dict[str, Any]] = []
        valid_gains: list[float] = []
        consecutive_outliers = 0
        champion_hash, candidate_hash = stable_hash(incumbent), stable_hash(candidate)
        decision = policy.decide([])
        for block in range(1, min(3, self.paired_blocks) + 1):
            pair = [(f"control_{block}", incumbent), (f"treatment_{block}", candidate)]
            rng.shuffle(pair)
            order.extend(pair)
            champion_history = list(self.tps_history.get(champion_hash, []))
            candidate_history = list(self.tps_history.get(candidate_hash, []))
            for label, config in pair:
                values[label], applies[label] = self.measure(
                    config, f"round_{round_number:03d}_{label}",
                    duration_override=duration_override)
            control_tps = float(values[f"control_{block}"]["throughput_rps"])
            treatment_tps = float(values[f"treatment_{block}"]["throughput_rps"])
            control_outlier = is_three_sigma_outlier(control_tps, champion_history)
            treatment_outlier = is_three_sigma_outlier(treatment_tps, candidate_history)
            outlier = control_outlier or treatment_outlier
            gain = log_gain(control_tps, treatment_tps)
            if outlier:
                consecutive_outliers += 1
            else:
                consecutive_outliers = 0
                valid_gains.append(gain)
            history = champion_history + candidate_history
            high_noise = False
            if len(history) >= 3:
                mean = sum(history) / len(history)
                deviation = statistics.stdev(history)
                high_noise = mean > 0 and deviation / mean > 2 * abs(gain)
            decision = policy.decide(valid_gains,
                                     outlier_blocks=consecutive_outliers,
                                     high_noise=high_noise)
            block_records.append({
                "block": block, "champion_tps": control_tps,
                "candidate_tps": treatment_tps, "log_gain": gain,
                "outlier": outlier,
                "outlier_reason": ({"champion_three_sigma": control_outlier,
                                    "candidate_three_sigma": treatment_outlier,
                                    "possible_causes": ["snapshot_not_quiet", "background_maintenance", "storage_jitter"]}
                                   if outlier else None),
                "decision_after_block": decision.as_dict(),
            })
            if block >= minimum_blocks and not decision.should_continue:
                break
        if decision.should_continue:
            decision = PairedDecision(
                "inconclusive", "maximum paired-block budget reached without a stable decision",
                decision.effect_mean, decision.ci_low, decision.ci_high,
                decision.delta_min, decision.blocks, decision.valid_blocks,
                decision.outlier_blocks, False)
        last_treatment = max(int(label.rsplit("_", 1)[1]) for label in applies if label.startswith("treatment_"))
        candidate_apply = applies[f"treatment_{last_treatment}"]
        observed = dict(candidate_apply.get("observed", {}))
        observed.update(candidate_apply.get("session_settings", {}))
        return {"order": [x[0] for x in order],
                "controls": [values[f"control_{i}"] for i in range(1, last_treatment + 1)],
                "treatments": [values[f"treatment_{i}"] for i in range(1, last_treatment + 1)],
                "paired_blocks": last_treatment, "block_records": block_records,
                "valid_log_gains": valid_gains, "sequential_decision": decision.as_dict(),
                "applies": applies,
                "observed_candidate_settings": observed}

    def run_factorial(self, incumbent: dict[str, Any], candidate: dict[str, Any], proposal: ExpertProposal, round_number: int) -> dict[str, Any]:
        design = proposal.interaction_design
        if design is None:
            raise ValueError("factorial execution requires InteractionDesign")
        a_config, b_config = dict(incumbent), dict(incumbent)
        for name in design.factor_a_knobs: a_config[name] = candidate[name]
        for name in design.factor_b_knobs: b_config[name] = candidate[name]
        configs = {"baseline": incumbent, "a_only": self.space.reconcile(a_config),
                   "b_only": self.space.reconcile(b_config), "a_plus_b": candidate}
        sequence = ["baseline", "a_only", "b_only", "a_plus_b"]
        values: dict[str, list[dict[str, Any]]] = {name: [] for name in sequence}
        applies: dict[str, list[dict[str, Any]]] = {name: [] for name in sequence}
        execution_order = []
        for repeat in range(design.repeats_per_cell):
            order = sequence if (round_number + repeat) % 2 else list(reversed(sequence))
            for cell in order:
                label = f"round_{round_number:03d}_{cell}_repeat_{repeat+1}"
                measurement, apply_result = self.measure(configs[cell], label)
                values[cell].append(measurement); applies[cell].append(apply_result); execution_order.append(cell)
        final_apply = applies["a_plus_b"][-1]
        observed = dict(final_apply.get("observed", {})); observed.update(final_apply.get("session_settings", {}))
        return {"order": execution_order, "controls": values["baseline"], "treatments": values["a_plus_b"],
                "factorial_measurements": values, "applies": applies, "observed_candidate_settings": observed}

    def restore(self) -> dict[str, Any]:
        result = self.executor.restore()
        self.restart_count += int(bool(result.get("restored")))
        return result
