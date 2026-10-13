from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .workflow import WorkflowOptions, WorkloadSwitchWorkflow


def directions(value: str) -> tuple[tuple[str, str], ...]:
    result = []
    for item in value.split(","):
        source, target = item.split(":", 1)
        if source == target or {source, target} != {"tpcc", "twitter"}:
            raise argparse.ArgumentTypeError(f"invalid direction: {item}")
        result.append((source, target))
    return tuple(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="BO references followed by isolated evidence-graph diagnosis")
    parser.add_argument("stage", choices=("bo", "diagnose", "all"))
    parser.add_argument("--suite-id", default="bo-switch-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    parser.add_argument("--tpcc-bo-budget", type=int, default=100)
    parser.add_argument("--twitter-bo-budget", type=int, default=100)
    parser.add_argument("--bo-initial-points", type=int, default=10)
    parser.add_argument("--bo-retest-repeats", type=int, default=3)
    parser.add_argument("--directions", type=directions, default=directions("tpcc:twitter,twitter:tpcc"))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--diagnosis-rounds", type=int, default=12)
    parser.add_argument("--diagnosis-measurements", type=int, default=60)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--terminals", type=int, default=2)
    parser.add_argument("--baseline-repeats", type=int, default=3)
    parser.add_argument("--source-profile-repeats", type=int, default=1)
    parser.add_argument("--max-observation-rounds", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261012)
    parser.add_argument("--drift-percent", type=float, default=25)
    parser.add_argument("--communication-topology", choices=("blackboard", "coordinator_star"), default="blackboard")
    parser.add_argument("--expert-roles", default="query_execution,memory_storage,concurrency_maintenance")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if min(args.tpcc_bo_budget, args.twitter_bo_budget) < 2:
        parser.error("each BO budget must be at least 2")
    options = WorkflowOptions(suite_id=args.suite_id, tpcc_bo_budget=args.tpcc_bo_budget,
        twitter_bo_budget=args.twitter_bo_budget, bo_initial_points=args.bo_initial_points,
        bo_retest_repeats=args.bo_retest_repeats, directions=args.directions, repetitions=args.repetitions,
        diagnosis_rounds=args.diagnosis_rounds, diagnosis_measurements=args.diagnosis_measurements,
        duration=args.duration, warmup_seconds=args.warmup_seconds, terminals=args.terminals,
        baseline_repeats=args.baseline_repeats, source_profile_repeats=args.source_profile_repeats,
        max_observation_rounds=args.max_observation_rounds, seed=args.seed, drift_percent=args.drift_percent,
        communication_topology=args.communication_topology,
        expert_roles=tuple(item.strip() for item in args.expert_roles.split(",") if item.strip()))
    workflow = WorkloadSwitchWorkflow(args.root.resolve(), options)
    output = {}
    if args.stage in {"bo", "all"}: output["bo"] = workflow.run_bo()
    if args.stage in {"diagnose", "all"}: output["diagnosis"] = workflow.run_diagnosis()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
