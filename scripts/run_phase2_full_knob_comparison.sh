#!/usr/bin/env bash
set -Eeuo pipefail

export LANG=C
export LC_ALL=C
export PYTHONUNBUFFERED=1
unset LANGUAGE LC_CTYPE || true

cd "$(dirname "$0")/.."
PYTHON_BIN="${PYTHON:-python3}"
[[ -x .venv/bin/python ]] && PYTHON_BIN=.venv/bin/python

EXPERIMENT_ID="${EXPERIMENT_ID:-phase2-full-$(date -u +%Y%m%dT%H%M%SZ)}"
DIRECTIONS="${DIRECTIONS:-tpcc:twitter,twitter:tpcc}"
BUDGET="${BUDGET:-20}"
DURATION="${DURATION:-30}"
TERMINALS="${TERMINALS:-2}"
SEED="${SEED:-20260921}"

for workload in $(printf '%s' "$DIRECTIONS" | tr ',:' ' '); do
  test -s "artifacts/snapshots/${workload}.dump" || {
    printf 'missing snapshot: artifacts/snapshots/%s.dump\n' "$workload" >&2
    exit 2
  }
done

mkdir -p "artifacts/phase2/$EXPERIMENT_ID"
cat > "artifacts/phase2/$EXPERIMENT_ID/run_config.json" <<EOF
{
  "experiment_id": "$EXPERIMENT_ID",
  "directions": "$DIRECTIONS",
  "budget_per_method": $BUDGET,
  "duration_seconds": $DURATION,
  "terminals": $TERMINALS,
  "seed": $SEED,
  "objective": "maximize target workload TPS only",
  "knob_count": 60
}
EOF

printf '[phase2] id=%s directions=%s budget=%s duration=%ss terminals=%s\n' "$EXPERIMENT_ID" "$DIRECTIONS" "$BUDGET" "$DURATION" "$TERMINALS"
printf '[phase2] live reports: artifacts/phase2/%s/*/LIVE_PROGRESS.md\n' "$EXPERIMENT_ID"

exec "$PYTHON_BIN" -m app.phase2.cli \
  --experiment-id "$EXPERIMENT_ID" \
  --directions "$DIRECTIONS" \
  --budget "$BUDGET" \
  --duration "$DURATION" \
  --terminals "$TERMINALS" \
  --seed "$SEED"
