#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

EXPERIMENT_ID="${EXPERIMENT_ID:-bo-twitter-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="artifacts/bayesian/$EXPERIMENT_ID"
mkdir -p "$RUN_DIR"

nohup env EXPERIMENT_ID="$EXPERIMENT_ID" \
  bash scripts/run_twitter_bayesian_100.sh \
  > "$RUN_DIR/nohup.log" 2>&1 < /dev/null &
OPTIMIZER_PID=$!
echo "$OPTIMIZER_PID" > "$RUN_DIR/launcher.pid"

printf 'experiment_id=%s\noptimizer_pid=%s\nlog=%s/nohup.log\nprogress=%s/twitter/LIVE_PROGRESS.md\n' \
  "$EXPERIMENT_ID" "$OPTIMIZER_PID" "$RUN_DIR" "$RUN_DIR"
