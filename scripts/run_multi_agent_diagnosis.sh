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
export DEEPSEEK_TIMEOUT_SECONDS="${DEEPSEEK_TIMEOUT_SECONDS:-120}"
export DEEPSEEK_MAX_RETRIES="${DEEPSEEK_MAX_RETRIES:-1}"

SOURCE_WORKLOAD="${SOURCE_WORKLOAD:-tpcc}"
TARGET_WORKLOAD="${TARGET_WORKLOAD:-twitter}"
EXPERIMENT_ID="${EXPERIMENT_ID:-multi-agent-$(date -u +%Y%m%dT%H%M%SZ)}"

mkdir -p artifacts/multi_agent
printf '%s\n' "$EXPERIMENT_ID" > artifacts/current_multi_agent_experiment_id.txt

exec python3 -m app.multi_agent.cli \
  --source "$SOURCE_WORKLOAD" \
  --target "$TARGET_WORKLOAD" \
  --experiment-id "$EXPERIMENT_ID" \
  --max-rounds "${MAX_ROUNDS:-12}" \
  --max-measurements "${MAX_MEASUREMENTS:-60}" \
  --duration "${DURATION:-30}" \
  --warmup-seconds "${WARMUP_SECONDS:-30}" \
  --terminals "${TERMINALS:-2}" \
  --seed "${BENCHMARK_SEED:-20261012}" \
  --baseline-repeats "${BASELINE_REPEATS:-3}" \
  --drift-percent "${DRIFT_PERCENT:-25}"
