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

    def _psql(self, sql: str) -> str:
        command = ["runuser", "-u", "postgres", "--", "psql", "-XAt", "-h", "/var/run/postgresql", "-p", os.getenv("PGPORT", "5432"), "-U", "postgres", "-d", self.database, "-v", "ON_ERROR_STOP=1", "-c", sql]
        local_env = dict(self.env)
        for name in ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"):
            local_env.pop(name, None)
        result = subprocess.run(command, env=local_env, text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def capture(self) -> dict[str, str]:
        catalog = FullKnobSpace()
        allowed = ",".join("'%s'" % name for name in catalog.names)
        rows = self._psql(f"select name,setting from pg_settings where name in ({allowed}) order by name")
        self.original = dict(line.split("|", 1) for line in rows.splitlines() if "|" in line)
        if len(self.original) != 60:
            raise RuntimeError(f"expected 60 settings from pg_settings, got {len(self.original)}")
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
        started = time.monotonic()
        self._psql("checkpoint")
        if settle_seconds:
            time.sleep(settle_seconds)
        return {"checkpoint_completed": True, "settle_seconds": settle_seconds,
                "elapsed_seconds": round(time.monotonic() - started, 3)}

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
