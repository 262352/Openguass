from __future__ import annotations

import json
import os
import statistics
import subprocess
import threading
from typing import Any


COUNTERS = (
    "xact_commit", "xact_rollback", "blks_read", "blks_hit", "tup_returned", "tup_fetched",
    "tup_inserted", "tup_updated", "tup_deleted", "conflicts", "temp_files", "temp_bytes",
    "deadlocks", "blk_read_time", "blk_write_time", "buffers_checkpoint", "buffers_clean",
    "buffers_backend", "checkpoints_timed", "checkpoints_req", "checkpoint_write_time",
    "checkpoint_sync_time", "wal_records", "wal_fpi", "wal_bytes", "wal_write", "wal_sync",
    "wal_write_time", "wal_sync_time", "io_pressure_some_total_us", "io_pressure_full_total_us",
    "seq_scan", "idx_scan", "n_live_tup", "n_dead_tup", "vacuum_count", "autovacuum_count",
    "analyze_count", "autoanalyze_count",
)
GAUGES = ("numbackends", "active_backends", "waiting_backends", "idle_in_transaction", "locks_total", "locks_waiting",
          "client_waiting_backends", "io_waiting_backends", "lock_waiting_backends", "wal_waiting_backends",
          "dirty_bytes", "writeback_bytes", "track_io_timing_enabled", "track_wal_io_timing_enabled")


class DiagnosticSampler:
    def __init__(self, database: str, interval: float = 1.0):
        self.database, self.interval = database, interval
        self.samples: list[dict[str, float]] = []
        self._event = threading.Event()
        self._thread: threading.Thread | None = None

    def _query(self) -> str:
        return """
select json_build_object(
 'timestamp',extract(epoch from clock_timestamp()),
 'xact_commit',d.xact_commit,'xact_rollback',d.xact_rollback,'blks_read',d.blks_read,'blks_hit',d.blks_hit,
 'tup_returned',d.tup_returned,'tup_fetched',d.tup_fetched,'tup_inserted',d.tup_inserted,'tup_updated',d.tup_updated,'tup_deleted',d.tup_deleted,
 'conflicts',d.conflicts,'temp_files',d.temp_files,'temp_bytes',d.temp_bytes,'deadlocks',d.deadlocks,
 'blk_read_time',d.blk_read_time,'blk_write_time',d.blk_write_time,'numbackends',d.numbackends,
 'buffers_checkpoint',b.buffers_checkpoint,'buffers_clean',b.buffers_clean,'buffers_backend',b.buffers_backend,
 'checkpoints_timed',b.checkpoints_timed,'checkpoints_req',b.checkpoints_req,'checkpoint_write_time',b.checkpoint_write_time,'checkpoint_sync_time',b.checkpoint_sync_time,
 'wal_records',w.wal_records,'wal_fpi',w.wal_fpi,'wal_bytes',w.wal_bytes,'wal_write',w.wal_write,'wal_sync',w.wal_sync,
 'wal_write_time',w.wal_write_time,'wal_sync_time',w.wal_sync_time,
 'seq_scan',t.seq_scan,'idx_scan',t.idx_scan,'n_live_tup',t.n_live_tup,'n_dead_tup',t.n_dead_tup,
 'vacuum_count',t.vacuum_count,'autovacuum_count',t.autovacuum_count,'analyze_count',t.analyze_count,'autoanalyze_count',t.autoanalyze_count,
 'active_backends',a.active_backends,'waiting_backends',a.waiting_backends,'idle_in_transaction',a.idle_in_transaction,
 'client_waiting_backends',a.client_waiting_backends,'io_waiting_backends',a.io_waiting_backends,
 'lock_waiting_backends',a.lock_waiting_backends,'wal_waiting_backends',a.wal_waiting_backends,
 'track_io_timing_enabled',(current_setting('track_io_timing')='on')::int,
 'track_wal_io_timing_enabled',(current_setting('track_wal_io_timing')='on')::int,
 'locks_total',l.locks_total,'locks_waiting',l.locks_waiting)
from pg_stat_database d cross join pg_stat_bgwriter b cross join pg_stat_wal w
cross join lateral (select coalesce(sum(seq_scan),0) seq_scan,coalesce(sum(idx_scan),0) idx_scan,coalesce(sum(n_live_tup),0) n_live_tup,coalesce(sum(n_dead_tup),0) n_dead_tup,coalesce(sum(vacuum_count),0) vacuum_count,coalesce(sum(autovacuum_count),0) autovacuum_count,coalesce(sum(analyze_count),0) analyze_count,coalesce(sum(autoanalyze_count),0) autoanalyze_count from pg_stat_all_tables where schemaname not in ('pg_catalog','information_schema')) t
cross join lateral (select count(*) filter(where state='active') active_backends,
 count(*) filter(where wait_event is not null) waiting_backends,
 count(*) filter(where state='idle in transaction') idle_in_transaction,
 count(*) filter(where wait_event_type='Client') client_waiting_backends,
 count(*) filter(where wait_event_type='IO') io_waiting_backends,
 count(*) filter(where wait_event_type='Lock') lock_waiting_backends,
 count(*) filter(where wait_event in ('WALSync','WALWrite','WALInitSync','WALInitWrite')) wal_waiting_backends
 from pg_stat_activity where datname=current_database()) a
cross join lateral (select count(*) locks_total,count(*) filter(where not granted) locks_waiting from pg_locks) l
where d.datname=current_database()
"""

    def _once(self) -> None:
        env = os.environ.copy()
        env.update({"LANG": "C", "LC_ALL": "C"})
        if not env.get("PGPASSWORD") and env.get("POSTGRES_BENCH_PASSWORD"):
            env["PGPASSWORD"] = env["POSTGRES_BENCH_PASSWORD"]
        command = ["psql", "-XAt", "-h", env.get("PGHOST", "127.0.0.1"), "-p", env.get("PGPORT", "5432"), "-U", env.get("PGUSER", "andromeda_bench"), "-d", self.database, "-c", self._query()]
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            row = json.loads(result.stdout.strip())
            row.update(self._host_io_sample())
            self.samples.append({key: float(value or 0) for key, value in row.items()})

    @staticmethod
    def _host_io_sample() -> dict[str, float]:
        result = {"io_pressure_some_total_us": 0.0, "io_pressure_full_total_us": 0.0,
                  "dirty_bytes": 0.0, "writeback_bytes": 0.0}
        try:
            for line in open("/proc/pressure/io", encoding="utf-8"):
                parts = line.split()
                if parts and parts[0] in {"some", "full"}:
                    values = dict(item.split("=", 1) for item in parts[1:] if "=" in item)
                    result[f"io_pressure_{parts[0]}_total_us"] = float(values.get("total", 0))
        except OSError:
            pass
        try:
            for line in open("/proc/meminfo", encoding="utf-8"):
                key, _, value = line.partition(":")
                if key in {"Dirty", "Writeback"}:
                    result[f"{key.lower()}_bytes"] = float(value.strip().split()[0]) * 1024.0
        except (OSError, ValueError, IndexError):
            pass
        return result

    def start(self) -> "DiagnosticSampler":
        self._once()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._event.wait(self.interval):
            self._once()

    def stop(self) -> list[dict[str, float]]:
        self._event.set()
        if self._thread:
            self._thread.join(5)
        self._once()
        return self.samples


def summarize(samples: list[dict[str, float]]) -> dict[str, Any]:
    if len(samples) < 2:
        return {"sample_count": len(samples), "rates_per_second": {}, "gauges": {}, "anomalies": ["insufficient_samples"]}
    elapsed = max(samples[-1]["timestamp"] - samples[0]["timestamp"], 1e-6)
    rates = {name: max(0.0, samples[-1].get(name, 0.0) - samples[0].get(name, 0.0)) / elapsed for name in COUNTERS}
    rates["buffer_hit_ratio"] = rates["blks_hit"] / max(1.0, rates["blks_hit"] + rates["blks_read"])
    rates["rollback_ratio"] = rates["xact_rollback"] / max(1.0, rates["xact_commit"] + rates["xact_rollback"])
    gauges = {}
    for name in GAUGES:
        values = sorted(sample.get(name, 0.0) for sample in samples)
        gauges[name] = {"median": statistics.median(values), "p95": values[min(len(values) - 1, int(0.95 * len(values)))], "max": max(values)}
    anomalies = []
    if rates["buffer_hit_ratio"] < 0.98 and rates["blks_read"] > 1:
        anomalies.append("low_buffer_hit_ratio")
    if rates["temp_bytes"] > 1024 * 1024:
        anomalies.append("temp_spill")
    if gauges["locks_waiting"]["max"] > 0 or rates["deadlocks"] > 0:
        anomalies.append("lock_contention")
    if rates["checkpoint_write_time"] > 100:
        anomalies.append("checkpoint_write_pressure")
    wal_sync_average_ms = rates["wal_sync_time"] / rates["wal_sync"] if rates["wal_sync"] > 0 else None
    wal_write_average_ms = rates["wal_write_time"] / rates["wal_write"] if rates["wal_write"] > 0 else None
    io_pressure = {"some_stall_fraction": rates["io_pressure_some_total_us"] / 1_000_000.0,
                   "full_stall_fraction": rates["io_pressure_full_total_us"] / 1_000_000.0}
    if wal_sync_average_ms is not None and wal_sync_average_ms >= 10:
        anomalies.append("wal_sync_latency")
    if io_pressure["full_stall_fraction"] >= 0.05:
        anomalies.append("host_io_pressure")
    if gauges["wal_waiting_backends"]["max"] > 0:
        anomalies.append("wal_wait")
    return {"sample_count": len(samples), "elapsed_seconds": elapsed, "rates_per_second": rates, "gauges": gauges,
            "wal_timing": {"sync_average_ms": wal_sync_average_ms, "write_average_ms": wal_write_average_ms,
                           "timing_enabled": bool(gauges["track_wal_io_timing_enabled"]["max"])},
            "host_io": io_pressure, "anomalies": sorted(set(anomalies))}


def normalize_by_transactions(summary: dict[str, Any], throughput_rps: float) -> dict[str, Any]:
    """Add rate/TPS metrics; these describe work per completed benchmark operation."""
    denominator = max(float(throughput_rps), 1e-12)
    summary = dict(summary)
    rates = summary.get("rates_per_second", {})
    summary["per_transaction"] = {
        f"{name}_per_txn": float(value) / denominator
        for name, value in rates.items()
        if isinstance(value, (int, float)) and name not in {"buffer_hit_ratio", "rollback_ratio"}
    }
    summary["normalization"] = {
        "denominator": "BenchBase throughput_rps",
        "warning": "per-second counters alone are not causal mechanism evidence",
    }
    return summary
