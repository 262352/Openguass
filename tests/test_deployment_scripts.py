from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_installer_is_aarch64_and_can_repair_legacy_role_password():
    script = read_script("install_postgres_benchbase.sh")
    assert "aarch64|arm64" in script
    assert "ubuntu-ports" in script
    assert "linux/${adoptium_arch}" in script
    assert "ALTER ROLE %I PASSWORD %L" in script
    assert "repairing its password" in script


def test_clean_install_creates_role_databases_and_local_password():
    installer = read_script("install_postgres_benchbase.sh")
    launcher = read_script("setup_and_run_multi_agent.sh")
    assert "secrets.token_urlsafe(24)" in installer
    assert "CREATE ROLE %I LOGIN PASSWORD %L" in installer
    assert "for database in tpcc twitter ycsb" in installer
    assert "createdb --owner" in installer
    assert 'PGPASSWORD="$bench_password" psql' in installer
    assert 'read -rsp "DeepSeek API Key: "' in launcher
    assert "cp .env.example .env" in launcher


def test_bootstrap_rejects_empty_or_invalid_snapshots():
    script = read_script("bootstrap_test_environment.sh")
    assert "-size 0 -delete" in script
    assert 'pg_restore --list "$snapshot"' in script
    assert 'test -s "artifacts/snapshots/${workload}.dump"' in script
    assert '-e "$project_dir" pytest' in script
    assert "workload_profile.sha256" in script
    assert "sha256sum --check --status" in script


def test_setup_entrypoint_prepares_before_nohup_and_creates_log_directory():
    script = read_script("setup_and_run_multi_agent.sh")
    bootstrap = script.index("bootstrap_test_environment.sh")
    create_artifacts = script.index("mkdir -p artifacts")
    nohup = script.index("nohup env")
    assert bootstrap < create_artifacts < nohup
    assert "--prepare-only" in script


def test_huawei_comparison_runs_bo_then_only_tpcc_to_twitter_diagnosis():
    script = read_script("setup_and_run_huawei_comparison.sh")
    assert 'TPCC_BO_BUDGET="${TPCC_BO_BUDGET:-100}"' in script
    assert 'TWITTER_BO_BUDGET="${TWITTER_BO_BUDGET:-100}"' in script
    assert 'DIRECTIONS="tpcc:twitter"' in script
    assert "run_bo_reference_then_switch.sh" in script
    assert "REFERENCE_STANDARD.md" in script
    assert "FINAL_COMPARISON.md" in script
    assert 'DURATION="${DURATION:-120}"' in script
    assert 'TPCC_TERMINALS="${TPCC_TERMINALS:-10}"' in script
    assert 'TWITTER_TERMINALS="${TWITTER_TERMINALS:-16}"' in script
