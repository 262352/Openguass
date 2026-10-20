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
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || { echo "Missing $PYTHON_BIN; run bootstrap_test_environment.sh first." >&2; exit 1; }
[[ -s artifacts/snapshots/twitter.dump ]] || { echo "Missing artifacts/snapshots/twitter.dump; run bootstrap_test_environment.sh first." >&2; exit 1; }
snapshot_profile_matches "artifacts/snapshots/workload_profile.sha256" || {
  echo "Snapshots do not match the current workload XML files." >&2
  echo "Run: sudo bash scripts/bootstrap_test_environment.sh --force-reload" >&2
  exit 1
}
[[ -s .benchbase/target/benchbase-postgres/benchbase.jar ]] || { echo "BenchBase is missing; run bootstrap_test_environment.sh first." >&2; exit 1; }

EXPERIMENT_ID="${EXPERIMENT_ID:-twitter-continuous-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p artifacts/twitter_continuous
printf '%s\n' "$EXPERIMENT_ID" > artifacts/current_twitter_continuous_experiment_id.txt

exec "$PYTHON_BIN" -m app.continuous.cli \
  --experiment-id "$EXPERIMENT_ID" \
  --workload twitter \
  --max-rounds "${MAX_ROUNDS:-12}" \
  --max-measurements "${MAX_MEASUREMENTS:-60}" \
  --max-wall-seconds "${MAX_WALL_SECONDS:-7200}" \
  --max-model-calls "${MAX_MODEL_CALLS:-36}" \
  --duration "${DURATION:-120}" \
  --warmup-seconds "${WARMUP_SECONDS:-30}" \
  --terminals "${TWITTER_TERMINALS:-8}" \
  --seed "${BENCHMARK_SEED:-20261015}" \
  --baseline-repeats "${BASELINE_REPEATS:-3}" \
  --drift-percent "${DRIFT_PERCENT:-25}" \
  --minimum-tps-gain-percent "${MIN_TPS_GAIN_PERCENT:-5}"
