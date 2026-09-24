from pathlib import Path
import xml.etree.ElementTree as ET

from app.benchmark.benchbase import BenchBaseRunner, BENCHBASE_COMMIT, SUPPORTED

ROOT = Path(__file__).resolve().parents[1]


def test_postgres_templates_cover_required_workloads():
    assert SUPPORTED == ("tpcc", "twitter", "ycsb")
    for workload in SUPPORTED:
        root = ET.parse(ROOT / f"benchmarks/postgres/{workload}.xml").getroot()
        assert root.findtext("type") == "POSTGRES"
        assert root.findtext("password") == "runtime"
        assert int(root.findtext("terminals")) >= 1


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
