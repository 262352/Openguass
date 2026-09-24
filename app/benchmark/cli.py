from __future__ import annotations
import argparse
import json
from pathlib import Path
from .benchbase import BenchBaseRunner, SUPPORTED


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real BenchBase workloads against local PostgreSQL")
    parser.add_argument("workload", choices=(*SUPPORTED, "all"))
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--terminals", type=int, default=2)
    parser.add_argument("--reuse-data", action="store_true", help="execute without clearing, creating, or loading tables")
    args = parser.parse_args()
    runner = BenchBaseRunner()
    workloads = SUPPORTED if args.workload == "all" else (args.workload,)
    results = [runner.run(name, args.duration, args.terminals, not args.reuse_data) for name in workloads]
    combined = Path("artifacts/runs/latest-benchbase-results.json")
    combined.parent.mkdir(parents=True, exist_ok=True)
    combined.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
