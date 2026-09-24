import json
from pathlib import Path
import pytest
from app.config.settings import load_settings
from app.recommendation.catalog import KnobCatalog
from app.retrieval.local import LocalKnowledgeRetriever
from app.telemetry.analyzer import DeterministicTelemetryAnalyzer
from app.verification.verifier import ThresholdVerifier

ROOT = Path(__file__).resolve().parents[1]


def test_missing_key_is_clear(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY is missing"):
        load_settings(tmp_path / "missing.env")


def test_config_loads_key_without_exposing_it(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=sk-test-secret\n", encoding="utf-8")
    settings = load_settings(env)
    assert settings.api_key == "sk-test-secret"
    assert settings.api_key not in settings.masked_key


def test_retrieval_returns_buffer_pool_source():
    retriever = LocalKnowledgeRetriever(ROOT / "knowledge/mysql_official.jsonl")
    result = retriever.search("buffer pool physical reads")
    assert result[0].source_id == "mysql80-innodb-buffer-pool-size"


def test_analyzer_uses_window_deltas():
    before = {"Questions": 100, "Innodb_buffer_pool_read_requests": 1000, "Innodb_buffer_pool_reads": 10}
    after = {"Questions": 200, "Innodb_buffer_pool_read_requests": 2000, "Innodb_buffer_pool_reads": 110}
    result = DeterministicTelemetryAnalyzer().analyze(before, after, 10)
    assert result.metrics["Questions_per_second"] == 10
    assert result.metrics["buffer_pool_hit_ratio"] == pytest.approx(0.9)
    assert "buffer_pool_hit_ratio_low" in result.anomalies


def test_catalog_blocks_durability_changes():
    catalog = KnobCatalog(ROOT / "knowledge/knob_catalog.json")
    with pytest.raises(ValueError, match="not safe"):
        catalog.validate_change("sync_binlog", 0)


def test_verifier_rejects_benchmark_errors():
    result = ThresholdVerifier().verify(
        {"errors": 0, "transactions": 10},
        {"errors": 1, "transactions": 10},
        "physical_reads_per_second",
    )
    assert result.verdict == "experiment_failed"
