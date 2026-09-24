from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.phase2.runner import TransitionComparison


def main() -> None:
    parser = argparse.ArgumentParser(description="Andromeda vs GP: TPS-only full 60-knob comparison")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--directions", default="tpcc:twitter,twitter:tpcc")
    parser.add_argument("--budget", type=int, default=20)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--terminals", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260921)
    args = parser.parse_args()
    root = Path("artifacts/phase2") / args.experiment_id
    root.mkdir(parents=True, exist_ok=True)
    reports = []
    for offset, item in enumerate(args.directions.split(",")):
        source, target = item.strip().split(":", 1)
        report = TransitionComparison(args.experiment_id, source, target, args.budget, args.duration, args.terminals, args.seed + offset).run()
        reports.append(report)
    summary = {"experiment_id": args.experiment_id, "directions": args.directions, "reports": reports}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "completed", "summary": str(root / "summary.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
