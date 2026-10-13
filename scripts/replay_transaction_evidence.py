#!/usr/bin/env python3
"""Rebuild transaction-level client evidence from an existing episode's raw CSV files."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from app.benchmark.benchbase import summarize_transaction_latencies
from app.multi_agent.executor import ExperimentExecutor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("episode", type=Path)
    args = parser.parse_args()
    episode = args.episode.resolve()
    root = Path(__file__).resolve().parents[1]
    rows = []
    details = []
    for measurement_path in sorted((episode / "measurements").glob("*.json")):
        payload = json.loads(measurement_path.read_text(encoding="utf-8"))
        measurement = payload.get("measurement", payload)
        run_id = measurement.get("run_id")
        verified_path = root / "artifacts" / "runs" / str(run_id) / "verified-result.json"
        if not run_id or not verified_path.is_file():
            continue
        verified = json.loads(verified_path.read_text(encoding="utf-8"))
        raw_files = [Path(item) for item in verified.get("raw_files", []) if Path(item).is_file()]
        if not raw_files:
            raw_files = sorted(verified_path.parent.glob("*raw.csv"))
        transactions = summarize_transaction_latencies(
            raw_files, int(verified["duration_seconds"]), int(verified["terminals"]))
        latency = verified.get("latency", {})
        parsed = {"transaction_latency": transactions,
                  "p95_us": float(latency.get("95th Percentile Latency (microseconds)", 0) or 0)}
        bottleneck = ExperimentExecutor._client_bottleneck(parsed)
        record = {"measurement_file": measurement_path.name, "label": measurement.get("label"),
                  "run_id": run_id, "throughput_rps": measurement.get("throughput_rps"),
                  "bottleneck_summary": bottleneck, "transaction_latency": transactions}
        details.append(record)
        rows.append({"measurement": measurement_path.name, "label": measurement.get("label"),
                     "throughput_rps": measurement.get("throughput_rps"),
                     "critical_transaction": bottleneck.get("critical_transaction"),
                     "terminal_capacity_fraction": bottleneck.get("terminal_capacity_fraction"),
                     "transaction_p50_us": bottleneck.get("transaction_p50_us"),
                     "transaction_p99_us": bottleneck.get("transaction_p99_us"),
                     "overall_p95_us": bottleneck.get("overall_p95_us"),
                     "tail_masked": bottleneck.get("tail_masked_by_overall_p95")})
    (episode / "transaction_evidence_replay.json").write_text(
        json.dumps({"episode": str(episode), "measurements": details}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    fields = list(rows[0]) if rows else ["measurement", "label"]
    with (episode / "transaction_evidence_replay.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"episode": str(episode), "replayed_measurements": len(rows),
                      "json": str(episode / "transaction_evidence_replay.json"),
                      "csv": str(episode / "transaction_evidence_replay.csv")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
