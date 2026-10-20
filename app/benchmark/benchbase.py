from __future__ import annotations

import json
import csv
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
RETRYABLE_SQL_STATES = frozenset({"40P01", "40001"})


def _histogram_count(stdout: str, heading: str) -> int:
    """Return the total count from one BenchBase terminal histogram section."""
    section = re.search(re.escape(heading) + r".*?\n(.*?)\n\n", stdout, re.DOTALL)
    if not section or "<EMPTY>" in section.group(1):
        return 0
    return sum(int(value) for value in re.findall(r"\[\s*(\d+)\]", section.group(1)))


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def summarize_transaction_latencies(raw_files: list[Path], duration: int, terminals: int) -> dict[str, dict[str, float | int | None]]:
    """Summarize BenchBase raw records by transaction type without retaining individual rows."""
    grouped: dict[str, list[float]] = {}
    for path in raw_files:
        with path.open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                name = row.get("Transaction Name")
                latency = row.get("Latency (microseconds)")
                if not name or latency is None:
                    continue
                try:
                    grouped.setdefault(name, []).append(float(latency))
                except ValueError:
                    continue
    capacity_us = max(float(duration * terminals) * 1_000_000.0, 1.0)
    result: dict[str, dict[str, float | int | None]] = {}
    for name, values in grouped.items():
        total = sum(values)
        result[name] = {
            "count": len(values),
            "mean_us": total / len(values),
            "p50_us": _percentile(values, 0.50),
            "p95_us": _percentile(values, 0.95),
            "p99_us": _percentile(values, 0.99),
            "max_us": max(values),
            "total_latency_seconds": total / 1_000_000.0,
            "terminal_capacity_fraction": total / capacity_us,
        }
    return result


def classify_sql_errors(stdout: str, *, maximum_retryable_ratio: float = 0.001) -> dict[str, Any]:
    """Classify BenchBase SQL errors without turning rare concurrency aborts into run failures."""
    if not 0 <= maximum_retryable_ratio <= 1:
        raise ValueError("maximum_retryable_ratio must be between 0 and 1")
    section = re.search(r"Unexpected SQL Errors:.*?\n(.*?)\n\n", stdout, re.DOTALL)
    body = section.group(1) if section else ""
    counts = [int(value) for value in re.findall(r"\[\s*(\d+)\]", body)]
    unexpected = sum(counts) if "<EMPTY>" not in body else 0
    states = sorted(set(re.findall(
        r"will not be retried[^\n]*sql state \[([0-9A-Z]{5})\]", stdout, re.IGNORECASE)))
    states = [value.upper() for value in states]
    messages = list(dict.fromkeys(re.findall(r"(?:PSQLException:\s*)?ERROR:\s*([^\r\n]+)", stdout)))
    details = list(dict.fromkeys(re.findall(r"DETAIL:\s*([^\r\n]+)", stdout)))
    hints = list(dict.fromkeys(re.findall(r"HINT:\s*([^\r\n]+)", stdout)))
    measured = re.search(r"measuredRequests=(\d+)", stdout)
    measured_requests = int(measured.group(1)) if measured else 0
    completed_transactions = _histogram_count(stdout, "Completed Transactions:")
    aborted_transactions = _histogram_count(stdout, "Aborted Transactions:")
    server_retry_transactions = _histogram_count(stdout, "Rejected Transactions (Server Retry):")
    reconnect_retry_transactions = _histogram_count(stdout, "Rejected Transactions (Retry Different):")
    transaction_attempts = (completed_transactions + aborted_transactions
                            + server_retry_transactions + reconnect_retry_transactions)
    server_retry_ratio = server_retry_transactions / max(1, transaction_attempts)
    ratio = unexpected / measured_requests if measured_requests else (1.0 if unexpected else 0.0)
    retryable = unexpected if unexpected and states and set(states) <= RETRYABLE_SQL_STATES else 0
    nonretryable = unexpected - retryable
    accepted = nonretryable == 0 and ratio <= maximum_retryable_ratio
    return {
        "unexpected_sql_errors": unexpected,
        "retryable_sql_errors": retryable,
        "nonretryable_sql_errors": nonretryable,
        "sql_states": states,
        "messages": messages[:20],
        "details": details[:20],
        "hints": hints[:20],
        "measured_requests": measured_requests,
        "completed_transactions": completed_transactions,
        "aborted_transactions": aborted_transactions,
        "server_retry_transactions": server_retry_transactions,
        "reconnect_retry_transactions": reconnect_retry_transactions,
        "transaction_attempts": transaction_attempts,
        "server_retry_ratio": server_retry_ratio,
        "server_retries_per_completed": server_retry_transactions / max(1, completed_transactions),
        "unexpected_error_ratio": ratio,
        "maximum_retryable_error_ratio": maximum_retryable_ratio,
        "accepted": accepted,
        "policy": ("unexpected 40P01/40001 errors are accepted only within the fixed error ratio; "
                   "BenchBase internal server retries are reported separately and excluded from goodput"),
    }


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

    def _config(self, workload: str, duration: int, terminals: int, run_dir: Path, session_settings: dict[str, str] | None = None,
                random_seed: int | None = None) -> Path:
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
        if random_seed is not None:
            seed_node = root.find("randomSeed")
            if seed_node is None:
                seed_node = ET.SubElement(root, "randomSeed")
            seed_node.text = str(int(random_seed))
        for work in root.findall("./works/work"):
            time_node = work.find("time")
            if time_node is not None:
                time_node.text = str(duration)
        target = run_dir / f"{workload}.xml"
        tree.write(target, encoding="utf-8", xml_declaration=True)
        return target

    def run(self, workload: str, duration: int = 15, terminals: int = 2, reload_data: bool = True,
            session_settings: dict[str, str] | None = None, verbose: bool = True, random_seed: int | None = None) -> dict[str, Any]:
        if workload not in SUPPORTED:
            raise ValueError(f"unsupported workload: {workload}")
        if duration < 5 or terminals < 1:
            raise ValueError("duration must be >= 5 and terminals must be >= 1")
        self.health()
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-{workload}"
        run_dir = self.artifacts / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        config = self._config(workload, duration, terminals, run_dir, session_settings, random_seed)
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
        if return_code != 0 or not summaries:
            raise RuntimeError(f"{workload} failed; inspect {run_dir / 'stdout.log'}")
        summary = json.loads(summaries[-1].read_text(encoding="utf-8"))
        maximum_retryable_ratio = float(os.getenv("BENCHBASE_MAX_TRANSIENT_ERROR_RATIO", "0.001"))
        sql_errors = classify_sql_errors(stdout, maximum_retryable_ratio=maximum_retryable_ratio)
        if not sql_errors["accepted"]:
            diagnostic = "; ".join([*sql_errors["messages"], *sql_errors["hints"]])
            raise RuntimeError(
                f"{workload} failed SQL error policy: unexpected={sql_errors['unexpected_sql_errors']} "
                f"ratio={sql_errors['unexpected_error_ratio']:.6f} states={sql_errors['sql_states']}; "
                f"diagnostic={diagnostic or 'see stdout'}; "
                f"inspect {run_dir / 'stdout.log'}")
        raw_files = sorted(run_dir.glob("*raw.csv"))
        reported_throughput = summary.get("Throughput (requests/second)")
        successful_throughput = summary.get("Goodput (requests/second)")
        if successful_throughput is None:
            successful_throughput = reported_throughput
        result = {
            "run_id": run_id,
            "workload": workload,
            "database": workload,
            "benchbase_commit": BENCHBASE_COMMIT,
            "duration_seconds": duration,
            "terminals": terminals,
            "data_reloaded": reload_data,
            "session_settings": session_settings or {},
            "random_seed": random_seed,
            "elapsed_seconds": round(elapsed, 3),
            "throughput_rps": successful_throughput,
            "goodput_rps": successful_throughput,
            "benchbase_reported_throughput_rps": reported_throughput,
            "objective_definition": "successful completed transactions per second (BenchBase Goodput)",
            "sql_error_policy": sql_errors,
            "latency": summary.get("Latency Distribution", {}),
            "transaction_latency": summarize_transaction_latencies(raw_files, duration, terminals),
            "summary_file": str(summaries[-1]),
            "raw_files": [str(path) for path in raw_files],
            "status": ("passed_with_server_retries" if sql_errors["server_retry_transactions"]
                       else "passed_with_retryable_aborts" if sql_errors["retryable_sql_errors"] else "passed"),
        }
        (run_dir / "verified-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    def load_only(self, workload: str, terminals: int = 1, random_seed: int | None = None) -> dict[str, Any]:
        """Clear/create/load a dataset without executing a benchmark measurement."""
        if workload not in SUPPORTED:
            raise ValueError(f"unsupported workload: {workload}")
        self.health()
        load_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-{workload}-load"
        run_dir = ROOT / "artifacts" / "dataset_loads" / load_id
        run_dir.mkdir(parents=True, exist_ok=False)
        config = self._config(workload, 5, terminals, run_dir, random_seed=random_seed)
        command = [str(java_binary()), "-jar", "benchbase.jar", "-b", workload,
            "-c", str(config), "--clear=true", "--create=true", "--load=true",
            "--execute=false", "-d", str(run_dir)]
        started = time.monotonic()
        with (run_dir / "stdout.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(command, cwd=self.root, stdout=log,
                                     stderr=subprocess.STDOUT, text=True)
        tree = ET.parse(config)
        password = tree.getroot().find("password")
        if password is not None:
            password.text = "***REDACTED***"
            tree.write(config, encoding="utf-8", xml_declaration=True)
        result = {"load_id": load_id, "workload": workload, "execute": False,
                  "return_code": process.returncode,
                  "elapsed_seconds": round(time.monotonic() - started, 3),
                  "stdout": str(run_dir / "stdout.log")}
        (run_dir / "load-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if process.returncode:
            raise RuntimeError(f"{workload} load failed; inspect {run_dir / 'stdout.log'}")
        return result
