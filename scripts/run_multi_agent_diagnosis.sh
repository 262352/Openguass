#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${DEEPSEEK_API_KEY:?Set DEEPSEEK_API_KEY in .env or the environment}"
export DEEPSEEK_TIMEOUT_SECONDS="${DEEPSEEK_TIMEOUT_SECONDS:-300}"
export DEEPSEEK_MAX_RETRIES="${DEEPSEEK_MAX_RETRIES:-2}"

SOURCE_WORKLOAD="${SOURCE_WORKLOAD:-tpcc}"
TARGET_WORKLOAD="${TARGET_WORKLOAD:-twitter}"
EXPERIMENT_ID="${EXPERIMENT_ID:-multi-agent-$(date -u +%Y%m%dT%H%M%SZ)}"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

[[ -x "$PYTHON_BIN" ]] || {
  echo "Missing ${PYTHON_BIN}; run scripts/setup_and_run_multi_agent.sh first." >&2
  exit 1
}
[[ -s "artifacts/snapshots/${TARGET_WORKLOAD}.dump" ]] || {
  echo "Missing or empty target snapshot artifacts/snapshots/${TARGET_WORKLOAD}.dump." >&2
  echo "Run scripts/setup_and_run_multi_agent.sh first." >&2
  exit 1
}
[[ -s ".benchbase/target/benchbase-postgres/benchbase.jar" ]] || {
  echo "BenchBase is not prepared; run scripts/setup_and_run_multi_agent.sh first." >&2
  exit 1
}

mkdir -p artifacts/multi_agent
printf '%s\n' "$EXPERIMENT_ID" > artifacts/current_multi_agent_experiment_id.txt

extra_args=()
if [[ -n "${SOURCE_CONFIG:-}" ]]; then
  extra_args+=(--source-config "$SOURCE_CONFIG")
fi

case "$TARGET_WORKLOAD" in
  tpcc) default_terminals=5 ;;
  twitter) default_terminals=8 ;;
  *) default_terminals=2 ;;
esac
case "$SOURCE_WORKLOAD" in
  tpcc) default_source_terminals=5 ;;
  twitter) default_source_terminals=8 ;;
  *) default_source_terminals=2 ;;
esac

exec "$PYTHON_BIN" -m app.multi_agent.cli \
  --source "$SOURCE_WORKLOAD" \
  --target "$TARGET_WORKLOAD" \
  --experiment-id "$EXPERIMENT_ID" \
  --max-rounds "${MAX_ROUNDS:-12}" \
  --max-measurements "${MAX_MEASUREMENTS:-60}" \
  --duration "${DURATION:-120}" \
  --warmup-seconds "${WARMUP_SECONDS:-30}" \
  --terminals "${TERMINALS:-$default_terminals}" \
  --seed "${BENCHMARK_SEED:-20261012}" \
  --baseline-repeats "${BASELINE_REPEATS:-3}" \
  --drift-percent "${DRIFT_PERCENT:-25}" \
  --source-profile-repeats "${SOURCE_PROFILE_REPEATS:-0}" \
  --source-profile-terminals "${SOURCE_PROFILE_TERMINALS:-$default_source_terminals}" \
  --max-observation-rounds "${MAX_OBSERVATION_ROUNDS:-2}" \
  --communication-topology "${COMMUNICATION_TOPOLOGY:-blackboard}" \
  --expert-roles "${EXPERT_ROLES:-query_execution,memory_storage,concurrency_maintenance}" \
  "${extra_args[@]}"
