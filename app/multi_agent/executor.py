from __future__ import annotations

import hashlib
import json
import os
import platform
import time
from pathlib import Path
from typing import Any

from app.chain.plan_evidence import collect_plan_evidence
from app.lab.workloads import PostgresBenchBaseAdapter
from app.phase2.executor import PostgreSQLKnobExecutor
from app.phase2.space import FullKnobSpace
from app.phase2.telemetry import DiagnosticSampler, normalize_by_transactions, summarize
from .models import ExpertProposal


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
        self.executor = PostgreSQLKnobExecutor(self.system_dir)
        self.adapter = PostgresBenchBaseAdapter(workload)
        self.space = FullKnobSpace()
        self.workload = workload
        self.duration, self.warmup_seconds, self.terminals, self.seed = duration, warmup_seconds, terminals, seed
        self.measurement_count = 0
        self.warmup_count = 0
        self.restart_count = 0
        self.apply_count = 0
        self.experiment_context: dict[str, Any] = {}

    def capture(self) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = self.executor.capture()
        self.space = FullKnobSpace(raw)
        config = self.space.current_config()
        return config, EnvironmentProbe(self.executor).inspect()

    def context(self, source_config_hash: str, hardware: dict[str, Any]) -> dict[str, Any]:
        fingerprint = self.adapter.fingerprint()
        snapshot = self.adapter.snapshot
        snapshot_identity = {"path_name": snapshot.name, "sha256": self._file_hash(snapshot)}
        result = {"source_config_hash": source_config_hash, "workload_fingerprint": fingerprint,
                "snapshot": snapshot_identity, "hardware": hardware, "duration": self.duration,
                "warmup_seconds": self.warmup_seconds, "terminals": self.terminals, "seed": self.seed}
        self.experiment_context = result
        return result

    @staticmethod
    def _file_hash(path: Path) -> str | None:
        if not path.is_file(): return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""): digest.update(block)
        return digest.hexdigest()

    def measure(self, config: dict[str, Any], label: str) -> tuple[dict[str, Any], dict[str, Any]]:
        apply_result = self.executor.apply(config, self.space)
        self.apply_count += 1
        self.restart_count += int(bool(apply_result.get("restart")))
        self.adapter.restore_snapshot()
        if self.warmup_seconds:
            self.adapter.warmup(self.warmup_seconds, self.terminals, seed=self.seed)
            self.warmup_count += 1
        sampler = DiagnosticSampler(self.workload).start()
        started = time.monotonic()
        try:
            raw = self.adapter.run(self.duration, self.terminals, config=config, seed=self.seed)
        finally:
            samples = sampler.stop()
        parsed = self.adapter.parse_result(raw)
        self.measurement_count += 1
        measurement = {**parsed, "healthy": True, "label": label, "run_id": raw.get("run_id"),
            "elapsed_seconds": time.monotonic() - started,
            "telemetry": normalize_by_transactions(summarize(samples), parsed["throughput_rps"]),
            "plan_evidence": collect_plan_evidence(self.workload),
            "client": {"failed": 0, "timeouts": 0, "unexpected_sql_errors": 0,
                       "status": raw.get("status"), "goodput_rps": raw.get("goodput_rps")},
            "database_state": {"snapshot": self.adapter.snapshot.name, "restored_before_run": True},
            "experiment_context": dict(self.experiment_context),
            "requested_config_hash": stable_hash(config)}
        target = self.directory / "measurements" / f"{self.measurement_count:04d}_{label}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"measurement": measurement, "apply": apply_result}, indent=2), encoding="utf-8")
        return measurement, apply_result

    def run_paired(self, incumbent: dict[str, Any], candidate: dict[str, Any], round_number: int) -> dict[str, Any]:
        # Alternate temporal order across rounds while retaining C1/T1 and C2/T2 pair identities.
        order = [("control_1", incumbent), ("treatment_1", candidate), ("control_2", incumbent), ("treatment_2", candidate)]
        if round_number % 2 == 0:
            order = [("treatment_1", candidate), ("control_1", incumbent), ("treatment_2", candidate), ("control_2", incumbent)]
        values, applies = {}, {}
        for label, config in order:
            values[label], applies[label] = self.measure(config, f"round_{round_number:03d}_{label}")
        candidate_apply = applies["treatment_2"]
        observed = dict(candidate_apply.get("observed", {}))
        observed.update(candidate_apply.get("session_settings", {}))
        return {"order": [x[0] for x in order], "controls": [values["control_1"], values["control_2"]],
                "treatments": [values["treatment_1"], values["treatment_2"]], "applies": applies,
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
