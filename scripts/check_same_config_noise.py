#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.lab.workloads import PostgresBenchBaseAdapter


ROOT = Path(__file__).resolve().parents[1]
P95 = "95th Percentile Latency (microseconds)"


def pg_settings() -> dict[str, str]:
    names = [item["name"] for item in json.loads((ROOT / "knowledge/postgres_60_knobs.json").read_text())["knobs"]]
    query = "select name,setting from pg_settings where name in (" + ",".join("'%s'" % name for name in names) + ") order by name"
    env = os.environ.copy()
    for key in ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        env.pop(key, None)
    result = subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-XAt", "-h", "/var/run/postgresql", "-U", "postgres", "-d", "postgres", "-c", query],
        cwd="/tmp", env=env, text=True, capture_output=True, check=True,
    )
    values = dict(line.split("|", 1) for line in result.stdout.splitlines() if "|" in line)
    if len(values) != 60:
        raise RuntimeError(f"expected 60 PostgreSQL settings, got {len(values)}")
    return values


def fingerprint(config: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def distribution(values: list[float]) -> dict[str, float]:
    mean = statistics.mean(values)
    median = statistics.median(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "count": len(values), "mean": mean, "median": median, "stdev": stdev,
        "coefficient_of_variation": stdev / mean if mean else 0.0,
        "min": min(values), "max": max(values),
        "range_over_median": (max(values) - min(values)) / median if median else 0.0,
        "max_absolute_deviation_from_median": max(abs(value / median - 1) for value in values) if median else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Repeat BenchBase under one unchanged PostgreSQL configuration")
    parser.add_argument("--workloads", default="twitter,tpcc")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--terminals", type=int, default=2)
    parser.add_argument("--experiment-id", default=datetime.now(timezone.utc).strftime("same-config-noise-%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()
    workloads = [value.strip() for value in args.workloads.split(",") if value.strip()]
    if args.repeats < 2 or args.duration < 5 or args.terminals < 1:
        parser.error("repeats >= 2, duration >= 5, and terminals >= 1 are required")
    if any(value not in {"tpcc", "twitter", "ycsb"} for value in workloads):
        parser.error("workloads must contain tpcc, twitter, or ycsb")

    out = ROOT / "artifacts/noise" / args.experiment_id
    out.mkdir(parents=True, exist_ok=False)
    start_config = pg_settings()
    config_hash = fingerprint(start_config)
    (out / "fixed_config.json").write_text(json.dumps({"sha256": config_hash, "settings": start_config}, indent=2))
    adapters = {name: PostgresBenchBaseAdapter(name) for name in workloads}
    rows: list[dict[str, Any]] = []

    for repeat in range(1, args.repeats + 1):
        for workload in workloads:
            adapter = adapters[workload]
            restored = adapter.restore_snapshot()
            before = pg_settings()
            if fingerprint(before) != config_hash:
                raise RuntimeError(f"configuration changed before {workload} repeat {repeat}")
            raw = adapter.run(args.duration, args.terminals, None)
            after = pg_settings()
            if after != before:
                raise RuntimeError(f"configuration changed during {workload} repeat {repeat}")
            row = {
                "workload": workload, "repeat": repeat,
                "throughput_rps": float(raw["throughput_rps"]),
                "p95_us": float(raw["latency"][P95]),
                "elapsed_seconds": raw["elapsed_seconds"],
                "snapshot_restore_seconds": restored["elapsed_seconds"],
                "config_sha256": config_hash,
                "artifact": str(Path(raw["summary_file"]).parent),
            }
            rows.append(row)
            with (out / "trials.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")
            print(f"[noise] {workload} repeat={repeat}/{args.repeats} TPS={row['throughput_rps']:.2f} p95={row['p95_us']:.0f}us", flush=True)

    final_config = pg_settings()
    if final_config != start_config:
        raise RuntimeError("configuration changed across the experiment")
    summaries = {}
    for workload in workloads:
        selected = [row for row in rows if row["workload"] == workload]
        tps = distribution([row["throughput_rps"] for row in selected])
        p95 = distribution([row["p95_us"] for row in selected])
        severe = tps["coefficient_of_variation"] >= 0.10 or tps["range_over_median"] >= 0.25
        summaries[workload] = {"throughput_rps": tps, "p95_us": p95, "severe_tps_noise": severe}
    report = {
        "experiment_id": args.experiment_id,
        "protocol": {"workloads": workloads, "repeats": args.repeats, "duration_seconds": args.duration, "terminals": args.terminals, "restore_snapshot_before_every_run": True, "restart_between_runs": False},
        "fixed_config_sha256": config_hash,
        "config_unchanged": True,
        "severity_rule": "TPS CV >= 10% or (max-min)/median >= 25%",
        "workloads": summaries,
        "trials": str(out / "trials.jsonl"),
    }
    (out / "summary.json").write_text(json.dumps(report, indent=2))
    lines = [
        f"# Same-config noise check: {args.experiment_id}", "",
        f"Protocol: {args.repeats} repeats, {args.duration}s, {args.terminals} terminals; snapshot restored before every run; no PostgreSQL restart.", "",
        f"Fixed 60-knob configuration SHA-256: `{config_hash}`", "",
        "| Workload | TPS mean | TPS median | TPS min–max | TPS CV | Range/median | Severe |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for workload in workloads:
        stats = summaries[workload]["throughput_rps"]
        lines.append(
            f"| {workload} | {stats['mean']:.2f} | {stats['median']:.2f} | {stats['min']:.2f}–{stats['max']:.2f} | "
            f"{100*stats['coefficient_of_variation']:.2f}% | {100*stats['range_over_median']:.2f}% | "
            f"{'yes' if summaries[workload]['severe_tps_noise'] else 'no'} |"
        )
    lines += ["", "Severe means TPS CV >= 10% or (max-min)/median >= 25%. A single best observation must not be treated as a tuning improvement; repeat shortlisted configurations and compare robust medians.", ""]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
