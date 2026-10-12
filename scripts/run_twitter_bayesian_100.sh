#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

EXPERIMENT_ID="${EXPERIMENT_ID:-bo-twitter-$(date -u +%Y%m%dT%H%M%SZ)}"
BUDGET="${BUDGET:-100}"
INITIAL_POINTS="${INITIAL_POINTS:-8}"
DURATION="${DURATION:-30}"
TERMINALS="${TERMINALS:-2}"
SEED="${SEED:-20261009}"
SOURCE_CONFIG="${SOURCE_CONFIG:-artifacts/v3_v4/chain-group-20261009T020912Z/tpcc_to_twitter/source_config.json}"
COMPARISON_DIR="${COMPARISON_DIR:-artifacts/v3_v4/chain-group-20261009T020912Z/tpcc_to_twitter}"
RUN_DIR="artifacts/bayesian/$EXPERIMENT_ID"

[[ -f "$SOURCE_CONFIG" ]] || { echo "missing source config: $SOURCE_CONFIG" >&2; exit 2; }
mkdir -p "$RUN_DIR"

exec 9>artifacts/bayesian/.optimizer.lock
if ! flock -n 9; then
  echo "another Bayesian optimizer owns the database lock" >&2
  exit 73
fi

echo "$$" > "$RUN_DIR/pid"
echo "$EXPERIMENT_ID" > artifacts/current_bayesian_experiment_id.txt
rm -f "$RUN_DIR/STOP"

python scripts/monitor_bayesian_hourly.py "$$" "$RUN_DIR" \
  > "$RUN_DIR/hourly-monitor.log" 2>&1 &
MONITOR_PID=$!
echo "$MONITOR_PID" > "$RUN_DIR/monitor.pid"
cleanup_monitor() {
  kill "$MONITOR_PID" 2>/dev/null || true
  wait "$MONITOR_PID" 2>/dev/null || true
}
trap cleanup_monitor EXIT

python -m app.phase2.bayesian_baseline \
  --experiment-id "$EXPERIMENT_ID" \
  --budget "$BUDGET" \
  --initial-points "$INITIAL_POINTS" \
  --duration "$DURATION" \
  --terminals "$TERMINALS" \
  --seed "$SEED" \
  --source-config "$SOURCE_CONFIG" \
  --comparison-dir "$COMPARISON_DIR"
