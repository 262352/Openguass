from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .runner import MultiAgentRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Evidence-gated multi-agent PostgreSQL diagnosis")
    parser.add_argument("--source", required=True, choices=("tpcc", "twitter", "ycsb"))
    parser.add_argument("--target", required=True, choices=("tpcc", "twitter", "ycsb"))
    parser.add_argument("--experiment-id", default=None)
    parser.add_argument("--max-rounds", type=int, default=12)
    parser.add_argument("--max-measurements", type=int, default=60)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--terminals", type=int)
    parser.add_argument("--seed", type=int, default=20261012)
    parser.add_argument("--baseline-repeats", type=int, default=3)
    parser.add_argument("--drift-percent", type=float, default=25.0)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--source-profile-repeats", type=int, default=0)
    parser.add_argument("--source-profile-terminals", type=int)
    parser.add_argument("--max-observation-rounds", type=int, default=2)
    parser.add_argument("--communication-topology", choices=("blackboard", "coordinator_star"), default="blackboard")
    parser.add_argument("--expert-roles", default="query_execution,memory_storage,concurrency_maintenance")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    if args.source == args.target:
        parser.error("source and target must differ")
    workload_terminals = {"tpcc": 10, "twitter": 16, "ycsb": 2}
    terminals = args.terminals or workload_terminals[args.target]
    source_profile_terminals = args.source_profile_terminals or workload_terminals[args.source]
    experiment_id = args.experiment_id or "multi-agent-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    expert_roles = tuple(item.strip() for item in args.expert_roles.split(",") if item.strip())
    runner = MultiAgentRunner(args.root.resolve(), experiment_id, args.source, args.target,
        max_rounds=args.max_rounds, max_measurements=args.max_measurements, duration=args.duration,
        warmup_seconds=args.warmup_seconds, terminals=terminals, seed=args.seed,
        baseline_repeats=args.baseline_repeats, drift_percent=args.drift_percent,
        source_config=args.source_config, source_profile_repeats=args.source_profile_repeats,
        source_profile_terminals=source_profile_terminals,
        max_observation_rounds=args.max_observation_rounds,
        communication_topology=args.communication_topology, expert_roles=expert_roles,
        output_root=args.output_root)
    result = runner.run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
