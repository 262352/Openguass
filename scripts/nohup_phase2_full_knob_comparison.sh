#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

EXPERIMENT_ID="${EXPERIMENT_ID:-phase2-full-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="artifacts/phase2/$EXPERIMENT_ID"
mkdir -p "$RUN_DIR"

nohup env \
  EXPERIMENT_ID="$EXPERIMENT_ID" \
  DIRECTIONS="${DIRECTIONS:-tpcc:twitter,twitter:tpcc}" \
  BUDGET="${BUDGET:-20}" \
  DURATION="${DURATION:-30}" \
  TERMINALS="${TERMINALS:-2}" \
  SEED="${SEED:-20260921}" \
  ./scripts/run_phase2_full_knob_comparison.sh \
  > "$RUN_DIR/nohup.log" 2>&1 < /dev/null &

PID=$!
printf '%s\n' "$PID" > "$RUN_DIR/pid"
printf '%s\n' "$EXPERIMENT_ID" > artifacts/current_experiment_id.txt

printf 'experiment_id=%s\n' "$EXPERIMENT_ID"
printf 'pid=%s\n' "$PID"
printf 'log=%s/nohup.log\n' "$RUN_DIR"
printf 'progress=%s/<source>_to_<target>/LIVE_PROGRESS.md\n' "$RUN_DIR"
printf 'stop=kill -TERM %s\n' "$PID"

