from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable


STAT_FIELDS = (
    "calls", "total_exec_time", "mean_exec_time", "max_exec_time", "stddev_exec_time",
    "rows", "shared_blks_hit", "shared_blks_read", "shared_blks_dirtied",
    "shared_blks_written", "temp_blks_read", "temp_blks_written", "wal_records",
    "wal_fpi", "wal_bytes",
)


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class QueryWindow:
    before: dict[str, dict[str, Any]]
    wait_sampler: "QueryWaitSampler"
    started_at: float
    status: dict[str, Any]


class QueryWaitSampler:
    """Low-overhead queryid/wait-event sampling during the benchmark window."""

    def __init__(self, psql: Callable[[str], str], interval: float = 1.0):
        self.psql, self.interval = psql, interval
        self.counts: Counter[tuple[str, str, str, str, str]] = Counter()
        self.samples = 0
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _once(self) -> None:
        sql = """
select coalesce(json_agg(row_to_json(x)), '[]'::json)::text
from (
  select datid::text, usesysid::text, coalesce(query_id::text,'unknown') queryid,
         coalesce(wait_event_type,'CPU') wait_event_type,
         coalesce(wait_event,'running') wait_event, count(*) sessions
  from pg_stat_activity
  where datname=current_database() and pid <> pg_backend_pid()
  group by datid,usesysid,query_id,wait_event_type,wait_event
) x
"""
        try:
            rows = json.loads(self.psql(sql) or "[]")
            self.samples += 1
            for row in rows:
                key = (str(row["datid"]), str(row["usesysid"]), str(row["queryid"]),
                       str(row["wait_event_type"]), str(row["wait_event"]))
                self.counts[key] += int(row.get("sessions") or 0)
        except Exception as error:  # evidence failure must not kill the workload
            if len(self.errors) < 5:
                self.errors.append(f"{type(error).__name__}: {error}"[:500])

    def start(self) -> "QueryWaitSampler":
        self._once()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="query-wait-sampler")
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._once()

    def finish(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(5)
        self._once()
        rows = [
            {"dbid": key[0], "userid": key[1], "queryid": key[2],
             "wait_event_type": key[3], "wait_event": key[4], "sampled_sessions": count}
            for key, count in self.counts.most_common()
        ]
        return {"status": "available" if self.samples else "missing", "sample_count": self.samples,
                "sampling_interval_seconds": self.interval, "rows": rows, "errors": self.errors}


class PgStatStatementsWindow:
    """Version-aware pg_stat_statements window deltas plus query wait samples."""

    def __init__(self, psql: Callable[[str], str], *, top_n: int = 10,
                 query_text_limit: int = 1000, wait_interval: float = 1.0):
        self.psql = psql
        self.top_n = max(1, top_n)
        self.query_text_limit = max(80, query_text_limit)
        self.wait_interval = max(.2, wait_interval)
        self.columns: set[str] = set()
        self.field_sources: dict[str, str | None] = {}
        self.status: dict[str, Any] = {"status": "unprepared"}

    def prepare(self) -> dict[str, Any]:
        try:
            version = self.psql("show server_version_num")
            relation = self.psql("select coalesce(to_regclass('pg_stat_statements')::text,'')")
            if not relation:
                monitor = self.psql("select coalesce(to_regclass('pg_stat_monitor')::text,'')")
                auto_explain = self.psql(
                    "select position('auto_explain' in current_setting('shared_preload_libraries')) > 0")
                fallback = ("pg_stat_monitor_plus_pg_stat_activity" if monitor else
                            "auto_explain_plus_pg_stat_activity" if auto_explain == "t" else
                            "pg_stat_activity_sampling_only")
                self.status = {"status": "fallback", "reason": (
                    "pg_stat_statements view is unavailable; aggregate query deltas are disabled"),
                    "fallback_mode": fallback,
                    "fallback_limit": "wait sampling remains available; statement aggregates may be absent",
                    "postgresql_version_num": version}
                return dict(self.status)
            raw = self.psql(
                "select coalesce(json_agg(column_name order by ordinal_position),'[]'::json)::text "
                "from information_schema.columns where table_name='pg_stat_statements'"
            )
            self.columns = set(json.loads(raw or "[]"))
            required = {"dbid", "userid", "queryid", "query", "calls"}
            if not required <= self.columns:
                self.status = {"status": "unsupported", "reason": "required columns are absent",
                               "missing_columns": sorted(required - self.columns),
                               "postgresql_version_num": version}
                return dict(self.status)
            self.status = {"status": "available", "postgresql_version_num": version,
                           "columns": sorted(self.columns), "mode": "window_snapshot_delta"}
        except Exception as error:
            self.status = {"status": "disabled", "reason": f"{type(error).__name__}: {error}"[:1000]}
        return dict(self.status)

    def _snapshot(self) -> dict[str, dict[str, Any]]:
        if self.status.get("status") != "available":
            return {}
        candidates = {
            "calls": ("calls",), "total_exec_time": ("total_exec_time", "total_time"),
            "mean_exec_time": ("mean_exec_time", "mean_time"),
            "max_exec_time": ("max_exec_time", "max_time"),
            "stddev_exec_time": ("stddev_exec_time", "stddev_time"),
            "rows": "rows", "shared_blks_hit": "shared_blks_hit", "shared_blks_read": "shared_blks_read",
            "shared_blks_dirtied": "shared_blks_dirtied", "shared_blks_written": "shared_blks_written",
            "temp_blks_read": "temp_blks_read", "temp_blks_written": "temp_blks_written",
            "wal_records": "wal_records", "wal_fpi": "wal_fpi", "wal_bytes": "wal_bytes",
        }
        expressions = {alias: (next((name for name in ((sources,) if isinstance(sources, str) else sources)
                                            if name in self.columns), None))
                       for alias, sources in candidates.items()}
        self.field_sources = expressions
        select = ["dbid::text dbid", "userid::text userid", "queryid::text queryid",
                  f"left(query,{self.query_text_limit}) query"]
        for alias, source in expressions.items():
            select.append(f"{source}::{ 'bigint' if alias in {'calls','rows'} else 'numeric'} {alias}"
                          if source else f"null::numeric {alias}")
        sql = ("select coalesce(json_agg(row_to_json(x)),'[]'::json)::text from (select "
               + ",".join(select) + " from pg_stat_statements where dbid=(select oid from pg_database "
               "where datname=current_database())) x")
        rows = json.loads(self.psql(sql) or "[]")
        return {f"{r['dbid']}:{r['userid']}:{r['queryid']}": r for r in rows}

    def begin(self) -> QueryWindow:
        if self.status.get("status") == "unprepared":
            self.prepare()
        before = self._snapshot()
        waits = QueryWaitSampler(self.psql, self.wait_interval).start()
        return QueryWindow(before, waits, time.monotonic(), dict(self.status))

    def finish(self, window: QueryWindow) -> dict[str, Any]:
        waits = window.wait_sampler.finish()
        if window.status.get("status") != "available":
            return {"status": window.status.get("status"), "reason": window.status.get("reason"),
                    "fallback_mode": window.status.get("fallback_mode"),
                    "statistics": window.status, "wait_samples": waits, "leaderboards": {},
                    "window_seconds": time.monotonic() - window.started_at}
        try:
            after = self._snapshot()
        except Exception as error:
            return {"status": "missing", "reason": f"after snapshot failed: {error}"[:1000],
                    "statistics": window.status, "wait_samples": waits, "leaderboards": {},
                    "window_seconds": time.monotonic() - window.started_at}
        rows, reset, disappeared = [], [], sorted(set(window.before) - set(after))
        for key, current in after.items():
            previous = window.before.get(key)
            calls_now, calls_before = _number(current.get("calls")), _number((previous or {}).get("calls"))
            if previous and calls_now < calls_before:
                reset.append(key)
                previous = None
                calls_before = 0
            calls = calls_now - calls_before
            if calls <= 0:
                continue
            row = {"template_id": key, "dbid": current["dbid"], "userid": current["userid"],
                   "queryid": current["queryid"], "query": current.get("query", ""), "calls": int(calls),
                   "transaction_type": None,
                   "transaction_association_status": "unavailable_without_client/session correlation id"}
            for field in STAT_FIELDS:
                if field == "calls":
                    continue
                if not self.field_sources.get(field):
                    row[field] = None
                else:
                    value = _number(current.get(field)) - _number((previous or {}).get(field))
                    row[field] = max(0.0, value)
            row["window_mean_exec_time"] = row["total_exec_time"] / max(calls, 1) \
                if row["total_exec_time"] is not None else None
            row["shared_block_io"] = sum(_number(row.get(name)) for name in ("shared_blks_read", "shared_blks_written")) \
                if any(row.get(name) is not None for name in ("shared_blks_read", "shared_blks_written")) else None
            row["temp_block_io"] = sum(_number(row.get(name)) for name in ("temp_blks_read", "temp_blks_written")) \
                if any(row.get(name) is not None for name in ("temp_blks_read", "temp_blks_written")) else None
            rows.append(row)
        def top(field: str) -> list[dict[str, Any]]:
            return sorted(rows, key=lambda x: (_number(x.get(field)), x["calls"]), reverse=True)[:self.top_n]
        boards = {
            "total_exec_time": top("total_exec_time"),
            "mean_exec_time": top("window_mean_exec_time"),
            "shared_block_io": top("shared_block_io"),
            "temp_block_io": top("temp_block_io"),
            "wal_bytes": top("wal_bytes"),
        }
        unique = {item["template_id"]: item for values in boards.values() for item in values}
        wait_by_query: dict[str, list[dict[str, Any]]] = {}
        for item in waits["rows"]:
            wait_by_query.setdefault(item["queryid"], []).append(item)
        for row in unique.values():
            row["wait_events"] = wait_by_query.get(row["queryid"], [])
        return {"status": "available", "window_seconds": time.monotonic() - window.started_at,
                "statistics": window.status, "reset_detected": bool(reset), "reset_template_ids": reset,
                "disappeared_template_ids": disappeared, "sample_sufficient": len(rows) > 0,
                "template_count": len(rows), "leaderboards": boards,
                "templates": list(unique.values()), "wait_samples": waits,
                "missing_fields": sorted(field for field in STAT_FIELDS if not self.field_sources.get(field)),
                "slow_query_log": {"status": "not_collected",
                    "reason": "pg_stat_statements is the bounded primary source; log correlation requires an explicit separate instrumentation mode"},
                "percentiles_note": "query percentiles are not inferred from cumulative pg_stat_statements"}
