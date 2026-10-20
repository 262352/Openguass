from __future__ import annotations

import json

from app.continuous.baseline import OBSERVATION_KNOBS, SAFETY_OVERRIDES, build_default_baseline
from app.continuous.query_evidence import PgStatStatementsWindow, QueryWindow
from app.continuous.search import AdaptiveSearchScheduler, GROUPS
from app.phase2.space import FullKnobSpace


class FinishedWaits:
    def finish(self):
        return {"status": "available", "sample_count": 2, "sampling_interval_seconds": 1,
                "rows": [], "errors": []}


def test_default_baseline_uses_boot_values_and_declares_every_exception():
    space = FullKnobSpace()
    current = space.current_config()
    catalog = [{"name": name, "boot_val": current[name], "setting": current[name],
                "source": "configuration file", "unit": None, "context": "sighup",
                "sourcefile": "/tmp/postgresql.conf", "pending_restart": False}
               for name in space.names]
    next(row for row in catalog if row["name"] == "random_page_cost")["boot_val"] = 5.0
    current["random_page_cost"] = 1.1
    plan = build_default_baseline(catalog, space, current)
    assert plan.config["random_page_cost"] == 5.0
    assert plan.classifications["random_page_cost"] == "tuning_space_boot_default"
    assert set(plan.exceptions) == (set(OBSERVATION_KNOBS) | set(SAFETY_OVERRIDES)) & set(space.names)
    grouped = set().union(*(set(values) for values in GROUPS.values()))
    assert set(plan.tunable_knobs) <= grouped


def test_query_window_detects_reset_disappearance_and_keeps_missing_fields_null(monkeypatch):
    collector = PgStatStatementsWindow(lambda _: "", top_n=2)
    collector.status = {"status": "available", "postgresql_version_num": "140000"}
    collector.columns = {"dbid", "userid", "queryid", "query", "calls", "total_exec_time", "rows"}
    collector.field_sources = {field: (field if field in collector.columns else None)
                               for field in ("calls", "total_exec_time", "mean_exec_time", "max_exec_time",
                                             "stddev_exec_time", "rows", "shared_blks_hit", "shared_blks_read",
                                             "shared_blks_dirtied", "shared_blks_written", "temp_blks_read",
                                             "temp_blks_written", "wal_records", "wal_fpi", "wal_bytes")}
    before = {
        "1:2:3": {"dbid": "1", "userid": "2", "queryid": "3", "query": "select 1",
                  "calls": 10, "total_exec_time": 100, "rows": 10},
        "1:2:gone": {"dbid": "1", "userid": "2", "queryid": "gone", "calls": 1},
    }
    after = {"1:2:3": {"dbid": "1", "userid": "2", "queryid": "3", "query": "select 1",
                         "calls": 2, "total_exec_time": 12, "rows": 2}}
    monkeypatch.setattr(collector, "_snapshot", lambda: after)
    result = collector.finish(QueryWindow(before, FinishedWaits(), 0.0, dict(collector.status)))
    assert result["reset_detected"] is True
    assert result["disappeared_template_ids"] == ["1:2:gone"]
    assert "wal_bytes" in result["missing_fields"]
    assert result["templates"][0]["wal_bytes"] is None
    assert result["percentiles_note"].startswith("query percentiles are not inferred")


def test_healthy_evidence_still_yields_candidate_and_dormancy_reactivates(tmp_path):
    space = FullKnobSpace()
    scheduler = AdaptiveSearchScheduler(space, tuple(space.names), tmp_path,
        seed=1, dormancy_failures=1, reactivate_after=2)
    group, _ = scheduler.choose_group(1, [], [])
    candidate, changes = scheduler.candidate(space.current_config(), group, 1)
    assert changes and candidate != space.current_config()
    scheduler.update(group, 1, False)
    assert scheduler.state[group].status == "dormant"
    scheduler.choose_group(3, [], [])
    assert scheduler.state[group].status == "active"
    saved = json.loads((tmp_path / "search_state.json").read_text())
    assert saved["algorithm"] == "deterministic_local_and_cross_group"


def test_continuous_entry_has_no_bo_reference_dependency():
    from app.continuous import runner
    source = open(runner.__file__, encoding="utf-8").read().lower()
    assert "bo_references" not in source
    assert "source_configs" not in source
    assert "create index" not in source
    assert "alter table" not in source
