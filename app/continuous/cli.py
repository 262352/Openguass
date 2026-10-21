from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import TwitterContinuousRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Single-workload default-baseline continuous diagnostic tuning")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--workload", choices=("tpcc", "twitter"), default="twitter")
    parser.add_argument("--max-rounds", type=int, default=12)
    parser.add_argument("--search-rounds", type=int, default=12)
    parser.add_argument("--health-rounds", type=int, default=0)
    parser.add_argument("--max-measurements", type=int, default=111)
    parser.add_argument("--max-wall-seconds", type=int, default=7200)
    parser.add_argument("--max-model-calls", type=int, default=36)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--terminals", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261015)
    parser.add_argument("--baseline-repeats", type=int, default=3)
    parser.add_argument("--baseline-stability-retries", type=int, default=1,
                        help="Retry the three-repeat baseline once; then use the final median")
    parser.add_argument("--baseline-config", type=Path,
                        help="Canonical config shared with the BO track for a fair comparison")
    parser.add_argument("--drift-percent", type=float, default=25.0)
    parser.add_argument("--minimum-tps-gain-percent", type=float, default=5.0,
                        help="Compatibility fallback when no executor sequential decision is supplied")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    result = TwitterContinuousRunner(root, args.experiment_id,
        max_rounds=args.max_rounds, max_measurements=args.max_measurements,
        max_wall_seconds=args.max_wall_seconds, max_model_calls=args.max_model_calls,
        duration=args.duration, warmup_seconds=args.warmup_seconds,
        terminals=args.terminals, seed=args.seed, baseline_repeats=args.baseline_repeats,
        baseline_stability_retries=args.baseline_stability_retries,
        drift_percent=args.drift_percent,
        minimum_tps_gain_percent=args.minimum_tps_gain_percent,
        workload=args.workload, search_rounds=args.search_rounds,
        health_rounds=args.health_rounds,
        baseline_config=args.baseline_config).run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
