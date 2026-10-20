import csv

from app.benchmark.benchbase import summarize_transaction_latencies
from app.phase2.telemetry import summarize


def test_benchbase_raw_rows_expose_terminal_capacity_by_transaction(tmp_path):
    path = tmp_path / "twitter.raw.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Transaction Name", "Latency (microseconds)"])
        writer.writeheader()
        writer.writerows([
            {"Transaction Name": "GetTweet", "Latency (microseconds)": "100"},
            {"Transaction Name": "InsertTweet", "Latency (microseconds)": "30000000"},
            {"Transaction Name": "InsertTweet", "Latency (microseconds)": "30000000"},
        ])
    result = summarize_transaction_latencies([path], duration=30, terminals=2)
    assert result["InsertTweet"]["terminal_capacity_fraction"] == 1.0
    assert result["InsertTweet"]["p50_us"] == 30_000_000


def test_telemetry_reports_wal_latency_waits_and_host_io_pressure():
    before = {"timestamp": 0, "wal_sync": 0, "wal_sync_time": 0,
              "io_pressure_full_total_us": 0, "track_wal_io_timing_enabled": 1}
    after = {"timestamp": 10, "wal_sync": 10, "wal_sync_time": 200,
             "io_pressure_full_total_us": 1_000_000, "wal_waiting_backends": 1,
             "track_wal_io_timing_enabled": 1}
    result = summarize([before, after])
    assert result["wal_timing"]["sync_average_ms"] == 20
    assert result["counter_window"]["aligned_to"] == "current BenchBase measurement only"
    assert result["counter_window"]["counter_resets"] == []
    assert {"wal_sync_latency", "wal_wait", "host_io_pressure"} <= set(result["anomalies"])


def test_telemetry_marks_counter_reset_instead_of_silently_trusting_delta():
    before = {"timestamp": 0, "xact_commit": 100, "xact_rollback": 200}
    after = {"timestamp": 10, "xact_commit": 5, "xact_rollback": 8}
    result = summarize([before, after])
    assert set(result["counter_window"]["counter_resets"]) >= {"xact_commit", "xact_rollback"}
    assert "counter_reset_detected" in result["anomalies"]
