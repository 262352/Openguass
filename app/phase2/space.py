from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "knowledge/postgres_60_knobs.json"


@dataclass(frozen=True)
class Dimension:
    name: str
    vartype: str
    execution_class: str
    unit: str
    current: Any
    low: float | None = None
    high: float | None = None
    choices: tuple[Any, ...] = ()
    log: bool = False

    def decode(self, x: float) -> Any:
        x = min(1.0, max(0.0, float(x)))
        if self.choices:
            return self.choices[min(len(self.choices) - 1, int(x * len(self.choices)))]
        assert self.low is not None and self.high is not None
        if self.log and self.low > 0:
            value = math.exp(math.log(self.low) + x * (math.log(self.high) - math.log(self.low)))
        else:
            value = self.low + x * (self.high - self.low)
        return int(round(value)) if self.vartype == "integer" else round(value, 6)

    def encode(self, value: Any) -> float:
        if self.choices:
            normalized = str(value).lower()
            choices = [str(v).lower() for v in self.choices]
            index = choices.index(normalized) if normalized in choices else 0
            return (index + 0.5) / len(choices)
        value = float(value)
        assert self.low is not None and self.high is not None
        if self.high == self.low:
            return 0.5
        if self.log and self.low > 0 and value > 0:
            return (math.log(value) - math.log(self.low)) / (math.log(self.high) - math.log(self.low))
        return (value - self.low) / (self.high - self.low)


# Conservative experiment ranges. PostgreSQL's physical min/max are often far too broad
# for a live benchmark (for example max_connections=262143).
RANGES: dict[str, tuple[float, float, bool]] = {
    "autovacuum_analyze_scale_factor": (0.02, 0.3, False),
    "autovacuum_freeze_max_age": (100_000_000, 400_000_000, True),
    "autovacuum_max_workers": (2, 8, False),
    "autovacuum_naptime": (10, 120, True),
    "autovacuum_vacuum_cost_delay": (-1, 10, False),
    "autovacuum_vacuum_cost_limit": (-1, 2000, False),
    "autovacuum_vacuum_scale_factor": (0.02, 0.4, False),
    "backend_flush_after": (0, 256, False),
    "bgwriter_delay": (10, 500, True),
    "bgwriter_flush_after": (0, 256, False),
    "bgwriter_lru_maxpages": (0, 1000, False),
    "bgwriter_lru_multiplier": (0.5, 6, False),
    "checkpoint_completion_target": (0.5, 0.95, False),
    "checkpoint_flush_after": (0, 256, False),
    "checkpoint_timeout": (60, 900, True),
    "checkpoint_warning": (0, 120, False),
    "commit_delay": (0, 1000, False),
    "commit_siblings": (0, 20, False),
    "deadlock_timeout": (100, 3000, True),
    "default_statistics_target": (50, 500, True),
    "effective_cache_size": (131072, 1048576, True),
    "effective_io_concurrency": (0, 256, False),
    "from_collapse_limit": (2, 16, False),
    "join_collapse_limit": (2, 16, False),
    "log_min_duration_statement": (-1, 1000, False),
    "log_rotation_size": (1024, 102400, True),
    "log_temp_files": (-1, 10240, False),
    "maintenance_work_mem": (16384, 524288, True),
    "max_connections": (100, 300, False),
    "max_locks_per_transaction": (64, 256, False),
    "max_parallel_maintenance_workers": (0, 8, False),
    "max_parallel_workers": (0, 16, False),
    "max_parallel_workers_per_gather": (0, 8, False),
    "max_pred_locks_per_transaction": (64, 256, False),
    "max_replication_slots": (0, 16, False),
    "max_standby_streaming_delay": (-1, 60000, False),
    "max_wal_senders": (0, 16, False),
    "max_wal_size": (512, 4096, True),
    "max_worker_processes": (8, 32, False),
    "min_wal_size": (80, 512, True),
    "random_page_cost": (1.0, 6.0, False),
    "seq_page_cost": (0.5, 2.0, False),
    "shared_buffers": (8192, 65536, True),
    "temp_buffers": (512, 16384, True),
    "track_activity_query_size": (1024, 16384, True),
    "vacuum_cost_delay": (0, 10, False),
    "vacuum_cost_limit": (100, 2000, True),
    "wal_buffers": (-1, 2048, False),
    "wal_writer_delay": (20, 500, True),
    "wal_writer_flush_after": (0, 1024, False),
    "work_mem": (1024, 65536, True),
}


def _parse_enum(raw: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in raw.strip("{}").split(",") if x.strip())


class FullKnobSpace:
    def __init__(self, current: dict[str, Any] | None = None, catalog_path: Path = CATALOG):
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))["knobs"]
        current = current or {}
        dimensions = []
        for item in catalog:
            name, kind = item["name"], item["vartype"]
            value = current.get(name, item["setting"])
            if kind == "bool":
                dim = Dimension(name, kind, item["execution_class"], item["unit"], value, choices=("off", "on"))
            elif kind == "enum":
                choices = _parse_enum(item["enumvals"])
                if name == "huge_pages":
                    choices = ("off", "try")  # "on" can make PostgreSQL unable to start.
                dim = Dimension(name, kind, item["execution_class"], item["unit"], value, choices=choices)
            else:
                low, high, use_log = RANGES[name]
                physical_low = float(item["min_val"])
                physical_high = float(item["max_val"])
                low, high = max(low, physical_low), min(high, physical_high)
                dim = Dimension(name, kind, item["execution_class"], item["unit"], value, low, high, log=use_log)
            dimensions.append(dim)
        if len(dimensions) != 60 or len({x.name for x in dimensions}) != 60:
            raise RuntimeError("the full experiment requires exactly 60 unique PostgreSQL knobs")
        self.dimensions = tuple(dimensions)

    @property
    def names(self) -> list[str]:
        return [d.name for d in self.dimensions]

    def decode(self, point: list[float]) -> dict[str, Any]:
        if len(point) != len(self.dimensions):
            raise ValueError(f"expected 60 dimensions, got {len(point)}")
        return self.reconcile({d.name: d.decode(x) for d, x in zip(self.dimensions, point)})

    def encode(self, config: dict[str, Any]) -> list[float]:
        return [min(1.0, max(0.0, d.encode(config.get(d.name, d.current)))) for d in self.dimensions]

    def current_config(self) -> dict[str, Any]:
        value = {}
        for d in self.dimensions:
            if d.vartype == "integer":
                value[d.name] = int(float(d.current))
            elif d.vartype == "real":
                value[d.name] = float(d.current)
            else:
                value[d.name] = str(d.current).lower()
        return self.reconcile(value)

    def clamp(self, name: str, value: Any) -> Any:
        d = next((d for d in self.dimensions if d.name == name), None)
        if d is None:
            raise ValueError(f"unknown knob: {name}")
        if d.choices:
            normalized = str(value).lower()
            matches = [v for v in d.choices if str(v).lower() == normalized]
            if not matches:
                raise ValueError(f"{name} must be one of {d.choices}")
            return matches[0]
        number = float(value)
        assert d.low is not None and d.high is not None
        number = min(d.high, max(d.low, number))
        return int(round(number)) if d.vartype == "integer" else round(number, 6)

    def reconcile(self, config: dict[str, Any]) -> dict[str, Any]:
        value = dict(config)
        value["max_parallel_workers_per_gather"] = min(value["max_parallel_workers_per_gather"], value["max_parallel_workers"])
        value["max_parallel_maintenance_workers"] = min(value["max_parallel_maintenance_workers"], value["max_parallel_workers"])
        value["max_parallel_workers"] = min(value["max_parallel_workers"], value["max_worker_processes"])
        value["max_wal_senders"] = min(value["max_wal_senders"], value["max_connections"])
        if value["min_wal_size"] >= value["max_wal_size"]:
            value["min_wal_size"] = max(2, int(value["max_wal_size"] * 0.25))
        return value

    def describe(self) -> list[dict[str, Any]]:
        return [d.__dict__ for d in self.dimensions]


def latin_hypercube(n: int, dimensions: int, seed: int) -> list[list[float]]:
    rng = random.Random(seed)
    columns = []
    for _ in range(dimensions):
        values = [(i + rng.random()) / n for i in range(n)]
        rng.shuffle(values)
        columns.append(values)
    return [[columns[j][i] for j in range(dimensions)] for i in range(n)]
