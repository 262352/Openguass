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
[[ -x "$PYTHON_BIN" ]] || { echo "Missing $PYTHON_BIN; run bootstrap_test_environment.sh first." >&2; exit 1; }
[[ -s artifacts/snapshots/twitter.dump ]] || { echo "Missing artifacts/snapshots/twitter.dump; run bootstrap_test_environment.sh first." >&2; exit 1; }
snapshot_workload_profile_matches twitter "artifacts/snapshots/workload_profile.sha256" || {
  echo "Twitter snapshot does not match benchmarks/postgres/twitter.xml." >&2
  echo "Run: sudo bash scripts/bootstrap_test_environment.sh --force-reload" >&2
  exit 1
}
[[ -s .benchbase/target/benchbase-postgres/benchbase.jar ]] || { echo "BenchBase is missing; run bootstrap_test_environment.sh first." >&2; exit 1; }

EXPERIMENT_ID="${EXPERIMENT_ID:-twitter-continuous-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p artifacts/twitter_continuous
printf '%s\n' "$EXPERIMENT_ID" > artifacts/current_twitter_continuous_experiment_id.txt
BASELINE_ARGS=()
if [[ -n "${BASELINE_CONFIG:-}" ]]; then
  BASELINE_ARGS=(--baseline-config "$BASELINE_CONFIG")
fi

exec "$PYTHON_BIN" -m app.continuous.cli \
  --experiment-id "$EXPERIMENT_ID" \
  --workload twitter \
  --max-rounds "${MAX_ROUNDS:-12}" \
  --search-rounds "${SEARCH_ROUNDS:-12}" \
  --health-rounds "${HEALTH_ROUNDS:-0}" \
  --max-measurements "${MAX_MEASUREMENTS:-111}" \
  --max-wall-seconds "${MAX_WALL_SECONDS:-43200}" \
  --max-model-calls "${MAX_MODEL_CALLS:-36}" \
  --duration "${DURATION:-120}" \
  --warmup-seconds "${WARMUP_SECONDS:-30}" \
  --terminals "${TWITTER_TERMINALS:-5}" \
  --seed "${BENCHMARK_SEED:-20261015}" \
  --baseline-repeats "${BASELINE_REPEATS:-3}" \
  --baseline-stability-retries "${BASELINE_STABILITY_RETRIES:-1}" \
  "${BASELINE_ARGS[@]}" \
  --drift-percent "${DRIFT_PERCENT:-25}" \
  --minimum-tps-gain-percent "${MIN_TPS_GAIN_PERCENT:-5}"
