from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.multi_agent.models import (
    AlternativeExplanation, CostEstimate, ExpertProposal, FrozenPrediction, KnobChange,
    MetricSpec, ProposedEdge, ProposedNode,
)
from app.phase2.space import FullKnobSpace


GROUPS: dict[str, tuple[str, ...]] = {
    "query_planner": ("random_page_cost", "seq_page_cost", "effective_cache_size",
                      "effective_io_concurrency", "default_statistics_target", "jit",
                      "from_collapse_limit", "join_collapse_limit"),
    "memory": ("shared_buffers", "work_mem", "temp_buffers", "maintenance_work_mem"),
    "wal_commit": ("wal_buffers", "wal_writer_delay", "wal_writer_flush_after",
                   "commit_delay", "commit_siblings", "wal_sync_method", "wal_compression"),
    "writeback_checkpoint": ("checkpoint_completion_target", "checkpoint_timeout",
                             "max_wal_size", "min_wal_size", "bgwriter_delay",
                             "bgwriter_lru_maxpages", "bgwriter_lru_multiplier",
                             "backend_flush_after", "bgwriter_flush_after",
                             "checkpoint_flush_after", "checkpoint_warning"),
    "workers": ("max_parallel_workers", "max_parallel_workers_per_gather",
                "max_parallel_maintenance_workers", "max_worker_processes"),
    "maintenance": ("autovacuum_analyze_scale_factor", "autovacuum_vacuum_scale_factor",
                    "autovacuum_max_workers", "autovacuum_naptime",
                    "autovacuum_vacuum_cost_delay", "autovacuum_vacuum_cost_limit",
                    "autovacuum_freeze_max_age", "vacuum_cost_delay", "vacuum_cost_limit",
                    "deadlock_timeout", "max_connections", "max_replication_slots",
                    "max_standby_streaming_delay", "max_wal_senders", "hot_standby_feedback",
                    "huge_pages"),
    "serializable_predicate_locks": ("max_pred_locks_per_transaction",
                                     "max_pred_locks_per_relation",
                                     "max_pred_locks_per_page",
                                     "max_locks_per_transaction"),
}

ANOMALY_GROUPS = {
    "temp_spill": "memory", "low_buffer_hit_ratio": "memory",
    "wal_wait": "wal_commit", "wal_sync_latency": "wal_commit",
    "checkpoint_write_pressure": "writeback_checkpoint", "host_io_pressure": "writeback_checkpoint",
    "lock_contention": "maintenance",
}

MECHANISM_METRICS = {
    "query_planner": ("client.transaction_latency.GetUserTweets.p99_us", "decrease"),
    "memory": ("buffer_hit_ratio", "increase"),
    "wal_commit": ("telemetry.wal_timing.write_average_ms", "decrease"),
    "writeback_checkpoint": ("buffers_backend_per_txn", "decrease"),
    "workers": ("p99_us", "decrease"),
    "maintenance": ("n_dead_tup_per_txn", "decrease"),
}


@dataclass
class GroupState:
    status: str = "active"
    step: float = .20
    cursor: int = 0
    direction: int = 1
    successes: int = 0
    failures: int = 0
    consecutive_low_gain: int = 0
    dormant_since: int | None = None
    tried: list[str] = field(default_factory=list)


class AdaptiveSearchScheduler:
    """Deterministic local/cross-group search; no GP, BO, or hidden reference."""

    def __init__(self, space: FullKnobSpace, tunable_knobs: tuple[str, ...], directory: Path,
                 *, seed: int = 20261015, dormancy_failures: int = 2, reactivate_after: int = 3):
        self.space, self.directory, self.seed = space, directory, seed
        allowed = set(tunable_knobs)
        self.groups = {name: tuple(k for k in knobs if k in allowed and k in space.names)
                       for name, knobs in GROUPS.items()}
        self.groups = {name: knobs for name, knobs in self.groups.items() if knobs}
        self.state = {name: GroupState() for name in self.groups}
        self.order = tuple(self.groups)
        self.group_cursor = seed % max(len(self.order), 1)
        self.global_low_gain = 0
        self.dormancy_failures = dormancy_failures
        self.reactivate_after = reactivate_after
        self.directory.mkdir(parents=True, exist_ok=True)
        self.write()

    def stage(self, anomalies: list[str], last_accepted: bool) -> str:
        if any(item in ANOMALY_GROUPS for item in anomalies):
            return "bottleneck_repair"
        if last_accepted and self.global_low_gain < 2:
            return "nearby_exploration"
        return "cross_group_exploration"

    def choose_group(self, round_number: int, anomalies: list[str], expert_knobs: list[str]) -> tuple[str, str]:
        self._reactivate(round_number)
        for knob in expert_knobs:
            for name, knobs in self.groups.items():
                if knob in knobs and self.state[name].status != "dormant":
                    return name, "expert mechanism selected the parameter group"
        for anomaly in anomalies:
            name = ANOMALY_GROUPS.get(anomaly)
            if name in self.state and self.state[name].status != "dormant":
                return name, f"resource evidence prioritized {anomaly}"
        for offset in range(len(self.order)):
            index = (self.group_cursor + offset) % len(self.order)
            name = self.order[index]
            if self.state[name].status != "dormant":
                self.group_cursor = (index + 1) % len(self.order)
                return name, "periodic cross-group coverage"
        # Dormancy is reversible: if every group slept, reactivate the oldest.
        name = min(self.order, key=lambda item: self.state[item].dormant_since or 0)
        self.state[name].status = "active"; self.state[name].dormant_since = None
        return name, "all groups dormant; oldest group reactivated"

    def candidate(self, incumbent: dict[str, Any], group: str, round_number: int) -> tuple[dict[str, Any], list[KnobChange]]:
        state, knobs = self.state[group], self.groups[group]
        by_name = {d.name: d for d in self.space.dimensions}
        width = 2 if len(knobs) > 1 and (state.successes > 0 or self.global_low_gain >= 2) else 1
        for attempt in range(len(knobs) * 2):
            knob = knobs[state.cursor % len(knobs)]
            state.cursor += 1
            dim = by_name[knob]
            old = incumbent[knob]
            encoded = dim.encode(old)
            direction = state.direction
            proposed = dim.decode(encoded + direction * state.step)
            if str(proposed).lower() == str(old).lower():
                direction *= -1
                proposed = dim.decode(encoded + direction * state.step)
            state.direction = -state.direction
            if str(proposed).lower() == str(old).lower():
                continue
            value = dict(incumbent); value[knob] = proposed
            changes = [KnobChange(knob=knob, old_value=old, new_value=proposed,
                numerical_basis=f"deterministic normalized step={state.step:.4f}, direction={direction:+d}",
                mechanism_role=group)]
            if width == 2:
                second = knobs[state.cursor % len(knobs)]
                state.cursor += 1
                if second != knob:
                    second_dim, second_old = by_name[second], incumbent[second]
                    second_direction = state.direction
                    second_value = second_dim.decode(second_dim.encode(second_old) + second_direction * state.step)
                    if str(second_value).lower() == str(second_old).lower():
                        second_direction *= -1
                        second_value = second_dim.decode(second_dim.encode(second_old) + second_direction * state.step)
                    if str(second_value).lower() != str(second_old).lower():
                        value[second] = second_value
                        changes.append(KnobChange(knob=second, old_value=second_old, new_value=second_value,
                            numerical_basis=f"deterministic same-group combination step={state.step:.4f}, direction={second_direction:+d}",
                            mechanism_role=group))
            value = self.space.reconcile(value)
            # Reconciliation can constrain dependent worker/WAL dimensions; freeze actual values.
            changes = [item.model_copy(update={"new_value": value[item.knob]}) for item in changes]
            recorded = {item.knob for item in changes}
            for dependent in self.space.names:
                if dependent not in recorded and str(value[dependent]).lower() != str(incumbent[dependent]).lower():
                    changes.append(KnobChange(knob=dependent, old_value=incumbent[dependent],
                        new_value=value[dependent], numerical_basis="deterministic catalog dependency reconciliation",
                        mechanism_role=group))
            signature = json.dumps({item.knob: value[item.knob] for item in changes}, sort_keys=True, default=str)
            if signature in state.tried:
                continue
            state.tried.append(signature)
            state.status = "refining" if state.successes else "active"
            self.write()
            return value, changes
        state.status = "dormant"; state.dormant_since = round_number
        self.write()
        raise LookupError(f"parameter group {group} has no untested legal candidate")

    def update(self, group: str, round_number: int, accepted: bool) -> None:
        state = self.state[group]
        if accepted:
            state.successes += 1; state.failures = 0; state.consecutive_low_gain = 0
            state.status = "refining"; state.step = min(.35, state.step * 1.20)
            self.global_low_gain = 0
        else:
            state.failures += 1; state.consecutive_low_gain += 1; self.global_low_gain += 1
            state.step = max(.025, state.step * .5)
            if state.consecutive_low_gain >= self.dormancy_failures:
                state.status = "dormant"; state.dormant_since = round_number
            else:
                state.status = "active"
        self.write()

    def _reactivate(self, round_number: int) -> None:
        for state in self.state.values():
            if state.status == "dormant" and state.dormant_since is not None \
                    and round_number - state.dormant_since >= self.reactivate_after:
                state.status = "active"; state.dormant_since = None; state.consecutive_low_gain = 0

    def write(self) -> None:
        payload = {"schema_version": "andromeda.adaptive-search/v1", "algorithm": "deterministic_local_and_cross_group",
                   "global_low_gain": self.global_low_gain,
                   "groups": {name: asdict(state) | {"knobs": list(self.groups[name])}
                              for name, state in self.state.items()}}
        (self.directory / "search_state.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def exploration_proposal(*, round_number: int, group: str, changes: list[KnobChange],
                         config_hash: str, workload_hash: str, duration: int,
                         stage: str, reason: str, restart_required: bool = False,
                         workload: str = "twitter") -> ExpertProposal:
    """Represent a deterministic exploration as a frozen, auditable hypothesis."""
    prefix = f"r{round_number:03d}_search_{group}"
    metric, direction = MECHANISM_METRICS[group]
    if workload == "tpcc" and group == "query_planner":
        metric = "client.transaction_latency.NewOrder.p99_us"
    nodes = [
        ProposedNode(node_id=f"{prefix}_context", kind="context", label=f"{workload} incumbent during {stage}"),
        ProposedNode(node_id=f"{prefix}_intervention", kind="intervention",
                     label="; ".join(f"{x.knob}: {x.old_value} -> {x.new_value}" for x in changes)),
        ProposedNode(node_id=f"{prefix}_mechanism", kind="mechanism", label=f"{group} optimization opportunity"),
        ProposedNode(node_id=f"{prefix}_objective", kind="objective", label=f"maximize {workload} goodput TPS"),
    ]
    edges = [
        ProposedEdge(edge_id=f"{prefix}_mechanism_edge", source=nodes[1].node_id,
                     target=nodes[2].node_id, relation="tests_group_mechanism", context_hash=config_hash,
                     prediction_ids=[f"{prefix}_mechanism_prediction"]),
        ProposedEdge(edge_id=f"{prefix}_objective_edge", source=nodes[2].node_id,
                     target=nodes[3].node_id, relation="may_improve_goodput", context_hash=config_hash,
                     prediction_ids=[f"{prefix}_objective_prediction"]),
    ]
    return ExpertProposal(
        proposal_id=prefix, expert="query_execution" if group == "query_planner" else (
            "memory_storage" if group in {"memory", "wal_commit", "writeback_checkpoint"}
            else "concurrency_maintenance"),
        incumbent_config_hash=config_hash, workload_fingerprint_hash=workload_hash,
        title=f"{stage}: deterministic {group} candidate",
        anomaly=MetricSpec(metric_id="client.goodput_rps", source="BenchBase", measurement_type="state",
            unit="transactions/s", aggregation="current verified incumbent", sampling_window_seconds=duration),
        configuration_cause=f"Optimization opportunity in {group}; no anomaly is asserted.",
        mechanism=f"A bounded perturbation tests whether the {group} parameter group has local TPS headroom.",
        intermediate_metrics=[MetricSpec(metric_id=metric, source="parallel evidence window",
            measurement_type="per_query" if metric.startswith("client.") else "per_transaction",
            unit="recorded native unit", aggregation="paired control/treatment", sampling_window_seconds=duration)],
        query_effect="May change the measured query/transaction critical path; SQL and indexes remain unchanged.",
        expected_tps_effect="Candidate opportunity: TPS may increase; failure is informative and does not prove a global optimum.",
        expected_latency_effect="P95/P99 must remain within configured guardrails.", knob_changes=changes,
        predictions=[
            FrozenPrediction(prediction_id=f"{prefix}_mechanism_prediction", edge_id=edges[0].edge_id,
                metric_id=metric, expected_direction=direction, minimum_change=0.1,
                comparison_method="relative_percent", rationale="directional mechanism prediction frozen before execution"),
            FrozenPrediction(prediction_id=f"{prefix}_objective_prediction", edge_id=edges[1].edge_id,
                metric_id="throughput_rps", expected_direction="increase", minimum_change=0.0,
                comparison_method="relative_percent", rationale="TPS is the sole optimization objective"),
        ],
        alternatives=[AlternativeExplanation(alternative_id=f"{prefix}_noise", statement="Observed change is temporal noise",
            discriminating_metric_id="throughput_rps", expected_direction="unchanged")],
        cross_domain_requirements=[], why_exceeds_incumbent=reason,
        cost=CostEstimate(restart_required=restart_required, workload_measurements=4, estimated_seconds=max(1, duration * 4)),
        nodes=nodes, edges=edges,
    )
