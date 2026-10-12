from __future__ import annotations

import json
import os
import subprocess
from typing import Any


REPRESENTATIVE_SQL = {
    "tpcc": "SELECT * FROM warehouse WHERE w_id = 1",
    "twitter": "SELECT * FROM tweets WHERE id = 1",
    "ycsb": "SELECT * FROM usertable WHERE ycsb_key = 1",
}


def _walk(plan: dict[str, Any]) -> list[dict[str, Any]]:
    result = [plan]
    for child in plan.get("Plans", []):
        result.extend(_walk(child))
    return result


def collect_plan_evidence(database: str) -> dict[str, Any]:
    """Collect a non-executing EXPLAIN plan for a stable, representative key lookup."""
    sql = REPRESENTATIVE_SQL[database]
    env = os.environ.copy()
    env.update({"LANG": "C", "LC_ALL": "C"})
    if not env.get("PGPASSWORD") and env.get("POSTGRES_BENCH_PASSWORD"):
        env["PGPASSWORD"] = env["POSTGRES_BENCH_PASSWORD"]
    command = ["psql", "-XAt", "-h", env.get("PGHOST", "127.0.0.1"), "-p", env.get("PGPORT", "5432"),
               "-U", env.get("PGUSER", "andromeda_bench"), "-d", database,
               "-c", f"EXPLAIN (FORMAT JSON, COSTS TRUE) {sql}"]
    try:
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)
        if result.returncode:
            return {"status": "unavailable", "sql": sql, "error": result.stderr.strip()[:500]}
        payload = json.loads(result.stdout.strip())
        root = payload[0]["Plan"]
        nodes = _walk(root)
        node_types = [item.get("Node Type") for item in nodes]
        return {"status": "observed", "sql": sql, "root_node_type": root.get("Node Type"),
                "node_types": node_types, "uses_index": any("Index" in str(item) for item in node_types),
                "estimated_rows": float(root.get("Plan Rows", 0)), "estimated_total_cost": float(root.get("Total Cost", 0)),
                "source": "EXPLAIN FORMAT JSON (not EXPLAIN ANALYZE)"}
    except Exception as error:
        return {"status": "unavailable", "sql": sql, "error_type": type(error).__name__, "error": str(error)[:500]}
