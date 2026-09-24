from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SUPPORTED = ("tpcc", "twitter", "ycsb")
BENCHBASE_COMMIT = "33c00473807ebd49304d114a6d769d2d2b2bbb34"
JDK23 = Path.home() / ".local/jdks/jdk-23.0.2+7"


@dataclass(frozen=True)
class PostgresTarget:
    host: str = "127.0.0.1"
    port: int = 5432
    user: str = "andromeda_bench"
    password: str = ""


def java_binary() -> Path:
    configured = os.getenv("JAVA_HOME")
    candidates = [Path(configured) if configured else None, JDK23]
    for home in candidates:
        if home and (home / "bin/java").is_file():
            return home / "bin/java"
    found = shutil.which("java")
    if found:
        return Path(found)
    raise RuntimeError("Java 23 is required. Run scripts/install_postgres_benchbase.sh first.")


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"\x27"))


class BenchBaseRunner:
    def __init__(self, benchbase_root: Path | None = None, artifacts_root: Path | None = None):
        _load_dotenv()
        self.root = benchbase_root or ROOT / ".benchbase/target/benchbase-postgres"
        self.artifacts = artifacts_root or ROOT / "artifacts/runs"
        self.target = PostgresTarget(
            host=os.getenv("PGHOST", "127.0.0.1"),
            port=int(os.getenv("PGPORT", "5432")),
            user=os.getenv("PGUSER", "andromeda_bench"),
            password=os.getenv("PGPASSWORD") or os.getenv("POSTGRES_BENCH_PASSWORD", ""),
        )
        if not self.target.password:
            raise RuntimeError("POSTGRES_BENCH_PASSWORD is missing from .env")

    def health(self) -> None:
        command = ["pg_isready", "-h", self.target.host, "-p", str(self.target.port)]
        health_env = os.environ.copy()
        health_env.update({"LANG": "C", "LC_ALL": "C"})
        health_env.pop("LANGUAGE", None)
        health_env.pop("LC_CTYPE", None)
        subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True, env=health_env)
        if not (self.root / "benchbase.jar").is_file():
            raise RuntimeError("BenchBase is not built. Run scripts/install_postgres_benchbase.sh.")

    def _config(self, workload: str, duration: int, terminals: int, run_dir: Path, session_settings: dict[str, str] | None = None) -> Path:
        template = ROOT / f"benchmarks/postgres/{workload}.xml"
        tree = ET.parse(template)
        root = tree.getroot()
        for key, value in (session_settings or {}).items():
            if not re.fullmatch(r"[a-z_]+", key) or not re.fullmatch(r"[A-Za-z0-9.+-]+", str(value)):
                raise ValueError(f"unsafe PostgreSQL session setting: {key}")
        options = "" if not session_settings else "&options=" + quote(" ".join(f"-c {key}={value}" for key, value in session_settings.items()))
        values = {
            "url": f"jdbc:postgresql://{self.target.host}:{self.target.port}/{workload}?sslmode=disable&ApplicationName={workload}&reWriteBatchedInserts=true{options}",
            "username": self.target.user,
            "password": self.target.password,
            "terminals": str(terminals),
        }
        for key, value in values.items():
            node = root.find(key)
            if node is None:
                raise ValueError(f"template misses <{key}>: {template}")
            node.text = value
        for work in root.findall("./works/work"):
            time_node = work.find("time")
            if time_node is not None:
                time_node.text = str(duration)
        target = run_dir / f"{workload}.xml"
        tree.write(target, encoding="utf-8", xml_declaration=True)
        return target

    def run(self, workload: str, duration: int = 15, terminals: int = 2, reload_data: bool = True, session_settings: dict[str, str] | None = None, verbose: bool = True) -> dict[str, Any]:
        if workload not in SUPPORTED:
            raise ValueError(f"unsupported workload: {workload}")
        if duration < 5 or terminals < 1:
            raise ValueError("duration must be >= 5 and terminals must be >= 1")
        self.health()
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-{workload}"
        run_dir = self.artifacts / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        config = self._config(workload, duration, terminals, run_dir, session_settings)
        reload_text = str(reload_data).lower()
        command = [
            str(java_binary()), "-jar", "benchbase.jar", "-b", workload,
            "-c", str(config), f"--clear={reload_text}", f"--create={reload_text}",
            f"--load={reload_text}", "--execute=true", "-d", str(run_dir),
        ]
        started = time.monotonic()
        with (run_dir / "stdout.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            assert process.stdout is not None
            for line in process.stdout:
                if verbose:
                    sys.stdout.write(line)
                log.write(line)
            return_code = process.wait()
        for xml_path in run_dir.glob("*.xml"):
            xml_tree = ET.parse(xml_path)
            password = xml_tree.getroot().find("password")
            if password is not None:
                password.text = "***REDACTED***"
                xml_tree.write(xml_path, encoding="utf-8", xml_declaration=True)
        elapsed = time.monotonic() - started
        summaries = sorted(run_dir.glob("*summary.json"), key=lambda path: path.stat().st_mtime)
        stdout = (run_dir / "stdout.log").read_text(encoding="utf-8", errors="replace")
        errors = re.search(r"Unexpected SQL Errors:.*?\n(.*?)\n\n", stdout, re.DOTALL)
        has_sql_errors = bool(errors and "<EMPTY>" not in errors.group(1))
        if return_code != 0 or not summaries or has_sql_errors:
            raise RuntimeError(f"{workload} failed; inspect {run_dir / 'stdout.log'}")
        summary = json.loads(summaries[-1].read_text(encoding="utf-8"))
        result = {
            "run_id": run_id,
            "workload": workload,
            "database": workload,
            "benchbase_commit": BENCHBASE_COMMIT,
            "duration_seconds": duration,
            "terminals": terminals,
            "data_reloaded": reload_data,
            "session_settings": session_settings or {},
            "elapsed_seconds": round(elapsed, 3),
            "throughput_rps": summary.get("Throughput (requests/second)"),
            "goodput_rps": summary.get("Goodput (requests/second)"),
            "latency": summary.get("Latency Distribution", {}),
            "summary_file": str(summaries[-1]),
            "raw_files": [str(path) for path in sorted(run_dir.glob("*raw.csv"))],
            "status": "passed",
        }
        (run_dir / "verified-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
