from __future__ import annotations
from app.schemas.models import TelemetrySummary


class DeterministicTelemetryAnalyzer:
    COUNTERS = {
        "Questions", "Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads",
        "Innodb_data_reads", "Innodb_data_writes", "Innodb_row_lock_waits",
        "Innodb_row_lock_time", "Innodb_log_waits", "Created_tmp_tables",
        "Created_tmp_disk_tables", "Opened_tables", "Connections",
    }

    def analyze(self, before: dict[str, float], after: dict[str, float], seconds: float) -> TelemetrySummary:
        if seconds <= 0:
            raise ValueError("telemetry window must be positive")
        delta = {key: max(0.0, after.get(key, 0.0) - before.get(key, 0.0)) for key in self.COUNTERS}
        rates = {f"{key}_per_second": value / seconds for key, value in delta.items()}
        requests = delta["Innodb_buffer_pool_read_requests"]
        reads = delta["Innodb_buffer_pool_reads"]
        tmp = delta["Created_tmp_tables"]
        disk_tmp = delta["Created_tmp_disk_tables"]
        questions = max(delta["Questions"], 1.0)
        metrics = {
            **rates,
            "buffer_pool_hit_ratio": 1.0 - reads / max(requests, 1.0),
            "disk_tmp_table_ratio": disk_tmp / max(tmp, 1.0),
            "physical_reads_per_question": reads / questions,
            "lock_waits_per_question": delta["Innodb_row_lock_waits"] / questions,
        }
        anomalies = []
        if metrics["buffer_pool_hit_ratio"] < 0.99 and reads > 10:
            anomalies.append("buffer_pool_hit_ratio_low")
        if metrics["disk_tmp_table_ratio"] > 0.25 and disk_tmp > 5:
            anomalies.append("disk_tmp_table_ratio_high")
        if metrics["lock_waits_per_question"] > 0.01:
            anomalies.append("row_lock_waits_high")
        return TelemetrySummary(metrics=metrics, anomalies=anomalies, samples={"delta": delta})
