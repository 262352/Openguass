#!/usr/bin/env python3
"""Audit and optionally remove raw run directories absent from all experiment records."""
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


RUN_ID = re.compile(r"20\d{6}T\d{12}Z-(?:tpcc|twitter|ycsb)")
TEXT_SUFFIXES = {".json", ".jsonl", ".csv", ".md", ".log", ".txt"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    artifacts = args.root / "artifacts"
    runs = artifacts / "runs"
    candidates = {path.name: path for path in runs.iterdir() if path.is_dir()} if runs.exists() else {}
    referenced: set[str] = set()
    scanned = 0
    for path in artifacts.rglob("*"):
        if (not path.is_file() or runs in path.parents or path.name == "run_prune_report.json"
                or path.suffix.lower() not in TEXT_SUFFIXES):
            continue
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        referenced.update(name for name in RUN_ID.findall(text) if name in candidates)
    orphaned = sorted(set(candidates) - referenced)
    records = []
    for name in orphaned:
        path = candidates[name]
        size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        records.append({"run_id": name, "bytes": size, "reason": "not referenced by any experiment artifact"})
        if args.apply:
            shutil.rmtree(path)
    report = {"schema_version": "andromeda.run-prune/v1",
        "created_at": datetime.now(timezone.utc).isoformat(), "mode": "apply" if args.apply else "dry-run",
        "definition": "a run is orphaned only when its exact run id is absent from every text experiment artifact outside artifacts/runs",
        "scanned_artifact_files": scanned, "run_directories_before": len(candidates),
        "referenced_run_directories": len(referenced), "removed_or_would_remove": len(records),
        "bytes_removed_or_would_remove": sum(item["bytes"] for item in records), "runs": records}
    target = artifacts / "run_prune_report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (artifacts / "run_prune_history.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({key: value for key, value in report.items() if key != "runs"},
                                ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "runs"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
