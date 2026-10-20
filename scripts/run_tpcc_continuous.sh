#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/snapshot_profile.sh"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
: "${DEEPSEEK_API_KEY:?Set DEEPSEEK_API_KEY in .env or the environment}"
export PSQL_TIMEOUT_SECONDS="${PSQL_TIMEOUT_SECONDS:-30}"
export CHECKPOINT_TIMEOUT_SECONDS="${CHECKPOINT_TIMEOUT_SECONDS:-300}"
export STORAGE_PSI_FULL_AVG10_THRESHOLD="${STORAGE_PSI_FULL_AVG10_THRESHOLD:-30}"
export STORAGE_QUIET_MAX_WAIT_SECONDS="${STORAGE_QUIET_MAX_WAIT_SECONDS:-120}"
export STORAGE_QUIET_SAMPLE_SECONDS="${STORAGE_QUIET_SAMPLE_SECONDS:-2}"
export STORAGE_QUIET_CONSECUTIVE_SAMPLES="${STORAGE_QUIET_CONSECUTIVE_SAMPLES:-2}"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || { echo "Missing $PYTHON_BIN; prepare the environment first." >&2; exit 1; }
[[ -s artifacts/snapshots/tpcc.dump ]] || { echo "Missing artifacts/snapshots/tpcc.dump." >&2; exit 1; }
snapshot_workload_profile_matches tpcc "artifacts/snapshots/workload_profile.sha256" || {
  echo "TPC-C snapshot does not match benchmarks/postgres/tpcc.xml." >&2
  echo "Run: sudo bash scripts/bootstrap_test_environment.sh --force-reload" >&2
  exit 1
}
[[ -s .benchbase/target/benchbase-postgres/benchbase.jar ]] || { echo "BenchBase is missing." >&2; exit 1; }

EXPERIMENT_ID="${EXPERIMENT_ID:-tpcc-continuous-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p artifacts/tpcc_continuous
printf '%s\n' "$EXPERIMENT_ID" > artifacts/current_tpcc_continuous_experiment_id.txt
BASELINE_ARGS=()
if [[ -n "${BASELINE_CONFIG:-}" ]]; then
  BASELINE_ARGS=(--baseline-config "$BASELINE_CONFIG")
fi

exec "$PYTHON_BIN" -m app.continuous.cli \
  --experiment-id "$EXPERIMENT_ID" \
  --workload tpcc \
  --max-rounds "${MAX_ROUNDS:-15}" \
  --search-rounds "${SEARCH_ROUNDS:-15}" \
  --max-measurements "${MAX_MEASUREMENTS:-123}" \
  --max-wall-seconds "${MAX_WALL_SECONDS:-43200}" \
  --max-model-calls "${MAX_MODEL_CALLS:-45}" \
  --duration "${DURATION:-120}" \
  --warmup-seconds "${WARMUP_SECONDS:-30}" \
  --terminals "${TPCC_TERMINALS:-5}" \
  --seed "${BENCHMARK_SEED:-20261015}" \
  --baseline-repeats "${BASELINE_REPEATS:-3}" \
  --baseline-stability-retries "${BASELINE_STABILITY_RETRIES:-1}" \
  "${BASELINE_ARGS[@]}" \
  --drift-percent "${DRIFT_PERCENT:-25}" \
  --minimum-tps-gain-percent "${MIN_TPS_GAIN_PERCENT:-5}"
