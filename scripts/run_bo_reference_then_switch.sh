#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage: bash scripts/run_bo_reference_then_switch.sh

Stages are selected with STAGE=bo, STAGE=diagnose, or STAGE=all (default).
Required prepared assets: .venv, BenchBase, tpcc.dump and twitter.dump.
Main settings: SUITE_ID, BO_WORKLOADS, TPCC_BO_BUDGET, TWITTER_BO_BUDGET,
BO_INITIAL_POINTS, BO_RETEST_REPEATS, DIRECTIONS, REPETITIONS,
DIAGNOSIS_ROUNDS, DIAGNOSIS_MEASUREMENTS, DURATION, WARMUP_SECONDS,
TPCC_TERMINALS, TWITTER_TERMINALS, BASELINE_REPEATS,
SOURCE_PROFILE_REPEATS and BENCHMARK_SEED. TERMINALS remains a legacy override.
DeepSeek calls default to a 300-second timeout and two SDK retries; override
them with DEEPSEEK_TIMEOUT_SECONDS and DEEPSEEK_MAX_RETRIES in .env.
EOF
  exit 0
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${DEEPSEEK_API_KEY:?Set DEEPSEEK_API_KEY in .env or the environment}"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || { echo "Missing ${PYTHON_BIN}; prepare the environment first." >&2; exit 1; }
for workload in tpcc twitter; do
  [[ -s "artifacts/snapshots/${workload}.dump" ]] || { echo "Missing snapshot for ${workload}." >&2; exit 1; }
done
profile_file="artifacts/snapshots/workload_profile.sha256"
[[ -s "$profile_file" ]] && sha256sum --check --status "$profile_file" || {
  echo "Snapshots do not match the current workload scale/configuration; rebuild them with scripts/bootstrap_test_environment.sh --force-reload." >&2
  exit 1
}
[[ -s .benchbase/target/benchbase-postgres/benchbase.jar ]] || { echo "BenchBase is not prepared." >&2; exit 1; }

exec "$PYTHON_BIN" -m app.experiment.cli "${STAGE:-all}" \
  --suite-id "${SUITE_ID:-bo-switch-$(date -u +%Y%m%dT%H%M%SZ)}" \
  --tpcc-bo-budget "${TPCC_BO_BUDGET:-100}" \
  --twitter-bo-budget "${TWITTER_BO_BUDGET:-100}" \
  --bo-initial-points "${BO_INITIAL_POINTS:-10}" \
  --bo-retest-repeats "${BO_RETEST_REPEATS:-3}" \
  --bo-workloads "${BO_WORKLOADS:-tpcc,twitter}" \
  --directions "${DIRECTIONS:-tpcc:twitter,twitter:tpcc}" \
  --repetitions "${REPETITIONS:-1}" \
  --diagnosis-rounds "${DIAGNOSIS_ROUNDS:-12}" \
  --diagnosis-measurements "${DIAGNOSIS_MEASUREMENTS:-60}" \
  --duration "${DURATION:-120}" \
  --warmup-seconds "${WARMUP_SECONDS:-30}" \
  --tpcc-terminals "${TERMINALS:-${TPCC_TERMINALS:-10}}" \
  --twitter-terminals "${TERMINALS:-${TWITTER_TERMINALS:-16}}" \
  --baseline-repeats "${BASELINE_REPEATS:-3}" \
  --source-profile-repeats "${SOURCE_PROFILE_REPEATS:-1}" \
  --max-observation-rounds "${MAX_OBSERVATION_ROUNDS:-2}" \
  --seed "${BENCHMARK_SEED:-20261012}" \
  --communication-topology "${COMMUNICATION_TOPOLOGY:-blackboard}" \
  --expert-roles "${EXPERT_ROLES:-query_execution,memory_storage,concurrency_maintenance}"
