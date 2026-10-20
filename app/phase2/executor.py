from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from app.phase2.space import FullKnobSpace


class PostgreSQLKnobExecutor:
    """Apply all catalogued GUC classes and restore postgresql.auto.conf exactly."""

    def __init__(self, artifact_dir: Path, database: str = "postgres"):
        self.dir = artifact_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.env = os.environ.copy()
        self.env.update({"LANG": "C", "LC_ALL": "C"})
        self.env.pop("LANGUAGE", None)
        self.env.pop("LC_CTYPE", None)
        self.version = os.getenv("PG_CLUSTER_VERSION", "14")
        self.cluster = os.getenv("PG_CLUSTER_NAME", "main")
        self.original: dict[str, str] = {}
        self.applied: dict[str, Any] = {}
        self.auto_conf: Path | None = None
        self.backup = self.dir / "postgresql.auto.conf.before"
        self._captured = False
        self.diagnostic_instrumentation: dict[str, Any] = {}

    def _psql(self, sql: str, *, timeout: float | None = None) -> str:
        command = ["runuser", "-u", "postgres", "--", "psql", "-XAt", "-h", "/var/run/postgresql", "-p", os.getenv("PGPORT", "5432"), "-U", "postgres", "-d", self.database, "-v", "ON_ERROR_STOP=1", "-c", sql]
        local_env = dict(self.env)
        for name in ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"):
            local_env.pop(name, None)
        effective_timeout = timeout if timeout is not None else float(os.getenv("PSQL_TIMEOUT_SECONDS", "30"))
        if effective_timeout <= 0:
            raise ValueError("psql timeout must be positive")
        try:
            result = subprocess.run(command, env=local_env, text=True, capture_output=True,
                                    timeout=effective_timeout)
        except subprocess.TimeoutExpired as error:
            operation = " ".join(sql.strip().split())[:160]
            raise TimeoutError(
                f"psql command timed out after {effective_timeout:g}s: {operation}"
            ) from error
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def capture(self) -> dict[str, str]:
        catalog = FullKnobSpace()
        allowed = ",".join("'%s'" % name for name in catalog.names)
        rows = self._psql(f"select name,setting from pg_settings where name in ({allowed}) order by name")
        self.original = dict(line.split("|", 1) for line in rows.splitlines() if "|" in line)
        if len(self.original) != len(catalog):
            raise RuntimeError(
                f"expected {len(catalog)} settings from pg_settings, got {len(self.original)}")
        data_dir = Path(self._psql("show data_directory"))
        self.auto_conf = data_dir / "postgresql.auto.conf"
        if self.auto_conf.exists():
            shutil.copy2(self.auto_conf, self.backup)
        else:
            self.backup.write_text("# empty before phase2\n", encoding="utf-8")
        (self.dir / "start_config.json").write_text(json.dumps(self.original, indent=2), encoding="utf-8")
        self._captured = True
        self.applied = dict(self.original)
        return dict(self.original)

    def capture_catalog(self) -> list[dict[str, Any]]:
        """Capture provenance and boot/reset values for every tunable GUC."""
        catalog = FullKnobSpace()
        allowed = ",".join("'%s'" % name for name in catalog.names)
        rows = self._psql(
            "select coalesce(json_agg(row_to_json(x)),'[]'::json)::text from ("
            "select name,setting,boot_val,reset_val,unit,context,source,"
            "coalesce(sourcefile,'') sourcefile,coalesce(sourceline,0) sourceline,"
            "pending_restart from pg_settings where name in (" + allowed + ") order by name) x"
        )
        value = json.loads(rows or "[]")
        if len(value) != len(catalog):
            raise RuntimeError(
                f"expected {len(catalog)} pg_settings metadata rows, got {len(value)}")
        (self.dir / "pg_settings_catalog.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return value

    def enable_diagnostic_timing(self) -> dict[str, Any]:
        """Enable measurement-only timing GUCs after the rollback snapshot is saved."""
        if not self._captured:
            raise RuntimeError("capture() must run before enable_diagnostic_timing()")
        available_rows = self._psql(
            "select name,setting from pg_settings where name in "
            "('track_io_timing','track_wal_io_timing') order by name"
        )
        before = dict(line.split("|", 1) for line in available_rows.splitlines() if "|" in line)
        enabled: list[str] = []
        for name in before:
            if before[name] != "on":
                self._psql(f"alter system set {name} = 'on'")
                enabled.append(name)
        if enabled:
            self._psql("select pg_reload_conf()")
        after_rows = self._psql(
            "select name,setting from pg_settings where name in "
            "('track_io_timing','track_wal_io_timing') order by name"
        )
        after = dict(line.split("|", 1) for line in after_rows.splitlines() if "|" in line)
        result = {"before": before, "after": after, "changed": enabled,
                  "restored_with_postgresql_auto_conf": True}
        self.diagnostic_instrumentation = result
        (self.dir / "diagnostic_instrumentation.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8")
        return result

    @staticmethod
    def _literal(value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def _restart(self) -> float:
        started = time.monotonic()
        result = subprocess.run(["pg_ctlcluster", self.version, self.cluster, "restart"], env=self.env, text=True, capture_output=True, timeout=180)
        if result.returncode:
            raise RuntimeError(f"PostgreSQL restart failed: {result.stderr.strip() or result.stdout.strip()}")
        return time.monotonic() - started

    def apply(self, config: dict[str, Any], space: FullKnobSpace) -> dict[str, Any]:
        if not self._captured:
            raise RuntimeError("capture() must run before apply()")
        config = space.reconcile(config)
        session, global_values = {}, {}
        by_name = {d.name: d for d in space.dimensions}
        for name, value in config.items():
            if by_name[name].execution_class == "session":
                session[name] = str(value)
            else:
                global_values[name] = value
        changed = []
        requires_restart = False
        requires_reload = False
        started = time.monotonic()
        try:
            changed_values = {
                name: value for name, value in global_values.items()
                if str(self.applied.get(name)).lower() != str(value).lower()
            }
            for name, value in changed_values.items():
                self._psql(f"alter system set {name} = {self._literal(value)}")
                changed.append(name)
                if by_name[name].execution_class == "restart":
                    requires_restart = True
                else:
                    requires_reload = True
            restart_seconds = self._restart() if requires_restart else 0.0
            if requires_reload and not requires_restart:
                self._psql("select pg_reload_conf()")
            observed_rows = self._psql(
                "select name,setting,pending_restart from pg_settings where name in ("
                + ",".join("'%s'" % name for name in global_values)
                + ") order by name"
            )
            observed = {parts[0]: {"setting": parts[1], "pending_restart": parts[2]} for line in observed_rows.splitlines() if len(parts := line.split("|")) == 3}
            pending = [name for name, item in observed.items() if item["pending_restart"] == "t"]
            if pending:
                raise RuntimeError(f"settings still pending restart: {pending}")
            self.applied.update(global_values)
            return {
                "session_settings": session,
                "global_settings": global_values,
                "changed_global_count": len(changed),
                "reload": requires_reload,
                "restart": requires_restart,
                "restart_seconds": round(restart_seconds, 3),
                "apply_seconds": round(time.monotonic() - started, 3),
                "observed": observed,
            }
        except Exception:
            self.restore()
            raise

    def quiesce(self) -> dict[str, Any]:
        """Flush restore/warmup writeback before a timed run and use one protocol everywhere."""
        settle_seconds = max(0.0, float(os.getenv("SNAPSHOT_SETTLE_SECONDS", "5")))
        checkpoint_timeout = float(os.getenv("CHECKPOINT_TIMEOUT_SECONDS", "300"))
        if checkpoint_timeout <= 0:
            raise ValueError("CHECKPOINT_TIMEOUT_SECONDS must be positive")
        started = time.monotonic()
        bgwriter_before = self._bgwriter_snapshot()
        self._psql("checkpoint", timeout=checkpoint_timeout)
        if settle_seconds:
            time.sleep(settle_seconds)
        storage_quiet = self._wait_for_storage_quiet()
        bgwriter_after = self._bgwriter_snapshot()
        bgwriter_delta = {
            key: bgwriter_after[key] - bgwriter_before.get(key, 0.0)
            for key in bgwriter_after
        }
        return {"checkpoint_completed": True, "settle_seconds": settle_seconds,
                "checkpoint_timeout_seconds": checkpoint_timeout,
                "storage_quiet": storage_quiet,
                "pg_stat_bgwriter_before": bgwriter_before,
                "pg_stat_bgwriter_after": bgwriter_after,
                "pg_stat_bgwriter_delta": bgwriter_delta,
                "elapsed_seconds": round(time.monotonic() - started, 3)}

    def _bgwriter_snapshot(self) -> dict[str, float]:
        names = ("checkpoint_write_time", "checkpoint_sync_time", "buffers_checkpoint",
                 "buffers_clean", "buffers_backend", "buffers_backend_fsync")
        row = self._psql(
            "select checkpoint_write_time,checkpoint_sync_time,buffers_checkpoint,"
            "buffers_clean,buffers_backend,buffers_backend_fsync from pg_stat_bgwriter"
        )
        parts = row.split("|")
        if len(parts) != len(names):
            raise RuntimeError(f"unexpected pg_stat_bgwriter row: {row!r}")
        return {name: float(value) for name, value in zip(names, parts)}

    @staticmethod
    def _io_pressure_full_avg10() -> float | None:
        try:
            for line in Path("/proc/pressure/io").read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if parts and parts[0] == "full":
                    values = dict(item.split("=", 1) for item in parts[1:] if "=" in item)
                    return float(values["avg10"])
        except (OSError, KeyError, ValueError):
            return None
        return None

    def _wait_for_storage_quiet(self) -> dict[str, Any]:
        threshold = float(os.getenv("STORAGE_PSI_FULL_AVG10_THRESHOLD", "30"))
        max_wait = max(0.0, float(os.getenv("STORAGE_QUIET_MAX_WAIT_SECONDS", "120")))
        interval = max(0.1, float(os.getenv("STORAGE_QUIET_SAMPLE_SECONDS", "2")))
        required = max(1, int(os.getenv("STORAGE_QUIET_CONSECUTIVE_SAMPLES", "2")))
        started, consecutive, samples = time.monotonic(), 0, []
        while True:
            value = self._io_pressure_full_avg10()
            samples.append({"elapsed_seconds": round(time.monotonic() - started, 3),
                            "psi_io_full_avg10_percent": value})
            # PSI is unavailable on some kernels. The completed checkpoint remains
            # the portable quiescence barrier in that case.
            if value is None:
                return {"passed": True, "reason": "psi_io_unavailable_after_checkpoint",
                        "threshold_percent": threshold, "samples": samples}
            consecutive = consecutive + 1 if value <= threshold else 0
            if consecutive >= required:
                return {"passed": True, "reason": "psi_io_below_threshold",
                        "threshold_percent": threshold, "required_consecutive_samples": required,
                        "wait_seconds": round(time.monotonic() - started, 3), "samples": samples}
            elapsed = time.monotonic() - started
            if elapsed >= max_wait:
                raise TimeoutError(
                    f"storage did not become quiet within {max_wait:g}s: "
                    f"PSI I/O full avg10={value:g}% threshold={threshold:g}%"
                )
            time.sleep(min(interval, max_wait - elapsed))

    def restore(self) -> dict[str, Any]:
        if not self._captured or self.auto_conf is None:
            return {"restored": False, "reason": "not captured"}
        started = time.monotonic()
        shutil.copy2(self.backup, self.auto_conf)
        restart_seconds = self._restart()
        self.applied = dict(self.original)
        result = {"restored": True, "elapsed_seconds": round(time.monotonic() - started, 3), "restart_seconds": round(restart_seconds, 3)}
        (self.dir / "rollback.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
