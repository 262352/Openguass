from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_installer_is_aarch64_and_never_resets_existing_password():
    script = read_script("install_postgres_benchbase.sh")
    assert "aarch64|arm64" in script
    assert "ubuntu-ports" in script
    assert "linux/${adoptium_arch}" in script
    assert "ALTER ROLE" not in script
    assert "No password was changed" in script


def test_bootstrap_rejects_empty_or_invalid_snapshots():
    script = read_script("bootstrap_test_environment.sh")
    assert "-size 0 -delete" in script
    assert 'pg_restore --list "$snapshot"' in script
    assert 'test -s "artifacts/snapshots/${workload}.dump"' in script
    assert '-e "$project_dir" pytest' in script


def test_setup_entrypoint_prepares_before_nohup_and_creates_log_directory():
    script = read_script("setup_and_run_multi_agent.sh")
    bootstrap = script.index("bootstrap_test_environment.sh")
    create_artifacts = script.index("mkdir -p artifacts")
    nohup = script.index("nohup env")
    assert bootstrap < create_artifacts < nohup
    assert "--prepare-only" in script
