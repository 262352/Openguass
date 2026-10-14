from pathlib import Path
import xml.etree.ElementTree as ET

from app.benchmark.benchbase import BenchBaseRunner, BENCHBASE_COMMIT, SUPPORTED, classify_sql_errors
from app.lab.workloads import PostgresBenchBaseAdapter
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_postgres_templates_cover_required_workloads():
    assert SUPPORTED == ("tpcc", "twitter", "ycsb")
    for workload in SUPPORTED:
        root = ET.parse(ROOT / f"benchmarks/postgres/{workload}.xml").getroot()
        assert root.findtext("type") == "POSTGRES"
        assert root.findtext("password") == "runtime"
        assert int(root.findtext("terminals")) >= 1


def test_formal_tpcc_and_twitter_profiles_are_unlimited_and_sized():
    expected = {"tpcc": (20, 10), "twitter": (100, 16)}
    for workload, (scale, terminals) in expected.items():
        root = ET.parse(ROOT / f"benchmarks/postgres/{workload}.xml").getroot()
        assert root.findtext("isolation") == "TRANSACTION_SERIALIZABLE"
        assert int(root.findtext("scalefactor")) == scale
        assert int(root.findtext("terminals")) == terminals
        assert root.findtext("./works/work/time") == "120"
        assert root.findtext("./works/work/rate") == "unlimited"


def test_runtime_config_reads_password_only_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_BENCH_PASSWORD", "test-only-password")
    runner = BenchBaseRunner(benchbase_root=tmp_path, artifacts_root=tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = runner._config("ycsb", 5, 1, run_dir)
    text = config.read_text(encoding="utf-8")
    assert "test-only-password" in text
    assert "test-only-password" not in (ROOT / "benchmarks/postgres/ycsb.xml").read_text(encoding="utf-8")
    assert len(BENCHBASE_COMMIT) == 40


def test_empty_snapshot_is_never_restored(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_BENCH_PASSWORD", "test-only-password")
    adapter = PostgresBenchBaseAdapter("twitter", runner=BenchBaseRunner(tmp_path, tmp_path))
    adapter.snapshot = tmp_path / "twitter.dump"
    adapter.snapshot.touch()
    with pytest.raises(RuntimeError, match="missing or empty snapshot"):
        adapter.restore_snapshot()


def test_rare_deadlock_is_a_bounded_retryable_abort():
    stdout = """
Results(state=EXIT, nanoSeconds=1, measuredRequests=22107) = 736 requests/sec
SQLException occurred and will not be retried... sql state [40P01], error code [0].
Unexpected SQL Errors:
NewOrder/01 [   1]

"""
    result = classify_sql_errors(stdout)
    assert result["accepted"]
    assert result["retryable_sql_errors"] == 1
    assert result["nonretryable_sql_errors"] == 0
    assert result["unexpected_error_ratio"] == pytest.approx(1 / 22107)


def test_excessive_retryable_errors_or_nonretryable_state_are_rejected():
    excessive = """
Results(state=EXIT, nanoSeconds=1, measuredRequests=1000) = 100 requests/sec
SQLException occurred and will not be retried... sql state [40001], error code [0].
Unexpected SQL Errors:
NewOrder/01 [   2]

"""
    assert not classify_sql_errors(excessive)["accepted"]
    fatal = excessive.replace("40001", "23505").replace("[   2]", "[   1]")
    result = classify_sql_errors(fatal)
    assert not result["accepted"]
    assert result["nonretryable_sql_errors"] == 1


def test_resource_error_keeps_postgres_message_and_hint():
    stdout = """
Results(state=EXIT, nanoSeconds=1, measuredRequests=1000) = 100 requests/sec
org.postgresql.util.PSQLException: ERROR: out of shared memory
  Hint: You might need to increase "max_pred_locks_per_transaction".
SQLException occurred and will not be retried... sql state [53200], error code [0].
Unexpected SQL Errors:
GetUserTweets/04 [  10]

""".replace("Hint:", "HINT:")
    result = classify_sql_errors(stdout)
    assert not result["accepted"]
    assert result["sql_states"] == ["53200"]
    assert result["messages"] == ["out of shared memory"]
    assert "max_pred_locks_per_transaction" in result["hints"][0]


def test_no_unexpected_sql_errors_is_accepted():
    result = classify_sql_errors("Unexpected SQL Errors:\n<EMPTY>\n\n")
    assert result["accepted"] and result["unexpected_sql_errors"] == 0
