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
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--terminals", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261012)
    parser.add_argument("--baseline-repeats", type=int, default=3)
    parser.add_argument("--drift-percent", type=float, default=25.0)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.source == args.target:
        parser.error("source and target must differ")
    experiment_id = args.experiment_id or "multi-agent-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    runner = MultiAgentRunner(args.root.resolve(), experiment_id, args.source, args.target,
        max_rounds=args.max_rounds, max_measurements=args.max_measurements, duration=args.duration,
        warmup_seconds=args.warmup_seconds, terminals=args.terminals, seed=args.seed,
        baseline_repeats=args.baseline_repeats, drift_percent=args.drift_percent)
    result = runner.run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
