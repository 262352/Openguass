from __future__ import annotations

import json
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from .loop import ChainRunOptions, ChainValidationLoop, create_manifest, new_experiment_id
from .preflight import chain_preflight
from app.phase2.executor import PostgreSQLKnobExecutor
from app.phase2.space import FullKnobSpace
from .loop import _hash


def capture_source_config(root: Path, workload: str, output: Path | None = None) -> dict[str, Any]:
    """Capture the currently active 60-knob configuration as a named source input."""
    output = output or root / "artifacts" / "source_configs" / f"{workload}.json"
    if not output.is_absolute():
        output = root / output
    executor = PostgreSQLKnobExecutor(root / "artifacts" / "source_config_capture" / workload)
    raw = executor.capture()
    config = FullKnobSpace(raw).current_config()
    value = {"workload": workload, "config": config, "config_hash": _hash(config),
             "note": "captured from the active PostgreSQL cluster; load the intended source reference before capture"}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"output": str(output), **value}


def parse_directions(raw: str) -> list[tuple[str, str]]:
    result = []
    for item in raw.split(","):
        parts = item.strip().split(":")
        if len(parts) != 2 or parts[0] == parts[1] or any(x not in {"tpcc", "twitter", "ycsb"} for x in parts):
            raise ValueError(f"invalid direction: {item}")
        result.append((parts[0], parts[1]))
    if not result:
        raise ValueError("at least one direction is required")
    return result


def run_directions(directions: list[tuple[str, str]], options: ChainRunOptions, experiment_id: str | None = None,
                   root: Path = Path("."), require_preflight: bool = True) -> dict[str, Any]:
    if require_preflight:
        preflight = chain_preflight(root)
        if not preflight["ok"]:
            return {"status": "preflight_failed", "preflight": preflight}
    experiment_id = experiment_id or new_experiment_id()
    base = create_manifest(root, experiment_id, directions, options)
    previous_handlers = {}
    def request_stop(signum, frame):
        (base / "STOP").write_text(f"signal={signum}\n", encoding="utf-8")
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, request_stop)
    reports = []
    (base / "SUMMARY.md").write_text(
        f"# 实验 {experiment_id}\n\n状态：**运行中**\n\n"
        "各方向的易读性能表位于 `<direction>/RESULTS.md`。详细 JSON、图和模型调用保留用于审计。\n",
        encoding="utf-8")
    try:
        for source, target in directions:
            if (base / "STOP").exists():
                break
            reports.append(ChainValidationLoop(experiment_id, source, target, options, root=root).run())
            lines = [f"# 实验 {experiment_id}", "", "| 方向 | 状态 | 干预数 | 停止原因 | 回滚 | 易读结果 |", "|---|---|---:|---|---|---|"]
            for item in reports:
                direction = item["direction"]
                state = "完成" if not item.get("failure") else "阻塞"
                lines.append(f"| {direction} | {state} | {item['experiments']} | {item['stop_reason']} | {'成功' if item['start_config_restored'] else '失败'} | [{direction}/RESULTS.md]({direction}/RESULTS.md) |")
            (base / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
            if reports[-1].get("failure"):
                break
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    source_hashes = {item["direction"]: item.get("source_config_hash") for item in reports}
    same_source_config = len(source_hashes) > 1 and len(set(source_hashes.values())) == 1
    aggregate = {"experiment_id": experiment_id, "status": "completed", "directions": reports,
                 "source_config_audit": {"hashes": source_hashes, "same_hash_across_directions": same_source_config,
                    "warning": "source workload configurations are identical; transition attribution is limited" if same_source_config else None},
                 "artifact_dir": str(base)}
    (base / "report.json").write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")
    return aggregate


def status(root: Path, experiment_id: str) -> dict[str, Any]:
    base = root / "artifacts" / "v3_v4" / experiment_id
    if not base.is_dir():
        raise FileNotFoundError(experiment_id)
    values = {}
    for path in base.glob("*_to_*/status.json"):
        values[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
    return {"experiment_id": experiment_id, "directions": values, "stop_requested": (base / "STOP").exists()}


def stop(root: Path, experiment_id: str) -> dict[str, Any]:
    base = root / "artifacts" / "v3_v4" / experiment_id
    if not base.is_dir():
        raise FileNotFoundError(experiment_id)
    (base / "STOP").write_text("requested\n", encoding="utf-8")
    return {"experiment_id": experiment_id, "stop_requested": True}


def resume(root: Path, experiment_id: str) -> dict[str, Any]:
    base = root / "artifacts" / "v3_v4" / experiment_id
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    (base / "STOP").unlink(missing_ok=True)
    pending = []
    for direction in manifest["directions"]:
        source, target = direction.split("_to_", 1)
        report = base / direction / "chain_report.json"
        report_failed = False
        if report.exists():
            report_failed = bool(json.loads(report.read_text(encoding="utf-8")).get("failure"))
        if not report.exists() or report_failed:
            interrupted = base / direction
            backup = interrupted / "system" / "postgresql.auto.conf.before"
            start_config = interrupted / "system" / "start_config.json"
            if backup.is_file() and start_config.is_file():
                executor = PostgreSQLKnobExecutor(interrupted / "system")
                executor.original = json.loads(start_config.read_text(encoding="utf-8"))
                executor.auto_conf = Path(executor._psql("show data_directory")) / "postgresql.auto.conf"
                executor.backup = backup
                executor._captured = True
                recovery = executor.restore()
                if not recovery.get("restored"):
                    raise RuntimeError(f"cannot restore safe checkpoint for {direction}")
            archive = base / f"{direction}.interrupted-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
            if interrupted.exists():
                interrupted.rename(archive)
            pending.append((source, target))
    if not pending:
        return {"experiment_id": experiment_id, "status": "already_complete"}
    options = ChainRunOptions(**manifest["options"])
    reports = [ChainValidationLoop(experiment_id, a, b, options, root=root).run() for a, b in pending]
    return {"experiment_id": experiment_id, "status": "completed", "directions": reports}


def report(root: Path, experiment_id: str) -> dict[str, Any]:
    base = root / "artifacts" / "v3_v4" / experiment_id
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(base.glob("*_to_*/chain_report.json"))]
    return {"experiment_id": experiment_id, "direction_reports": reports, "artifact_dir": str(base)}
