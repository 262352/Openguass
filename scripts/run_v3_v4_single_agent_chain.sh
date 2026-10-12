#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="${MODE:-formal}"
DIRECTIONS="${DIRECTIONS:-tpcc:twitter,twitter:tpcc}"
DURATION="${DURATION:-30}"
WARMUP="${WARMUP_SECONDS:-${WARMUP:-30}}"
TERMINALS="${TERMINALS:-2}"
MAX_EXPERIMENTS="${MAX_VALIDATION_EXPERIMENTS:-${MAX_EXPERIMENTS:-12}}"
MAX_RESTARTS="${MAX_RESTARTS:-60}"
MAX_WALL_SECONDS="${MAX_WALL_SECONDS:-7200}"
MIN_TPS_PERCENT="${TPS_MIN_IMPROVEMENT_PCT:-${MIN_TPS_PERCENT:-5}}"
PATIENCE="${NO_IMPROVEMENT_PATIENCE:-${PATIENCE:-3}}"
VALIDATION_MODE="${VALIDATION_MODE:-paired_independent_retest}"
[[ "$VALIDATION_MODE" == "paired_independent_retest" ]] || { echo "unsupported VALIDATION_MODE=$VALIDATION_MODE" >&2; exit 64; }
SOURCE_CONFIG_DIR="${SOURCE_CONFIG_DIR:-artifacts/source_configs}"
MAX_P95_REGRESSION_PERCENT="${MAX_P95_REGRESSION_PERCENT:-10}"
MAX_ROLLBACK_RATIO_INCREASE_PP="${MAX_ROLLBACK_RATIO_INCREASE_PP:-1}"
MIN_SCREENING_GROUPS="${MIN_SCREENING_GROUPS:-4}"
EXPERIMENT_ID="${EXPERIMENT_ID:-chain-$(date -u +%Y%m%dT%H%M%SZ)}"

if [[ "$MODE" == "formal" ]]; then
  python - "$DIRECTIONS" "$SOURCE_CONFIG_DIR" <<'PY'
import os,sys
from pathlib import Path
missing=[]
for raw in sys.argv[1].split(','):
    source=raw.strip().split(':',1)[0]
    explicit=os.getenv(f'ANDROMEDA_SOURCE_CONFIG_{source.upper()}')
    path=Path(explicit) if explicit else Path(sys.argv[2])/f'{source}.json'
    if not path.is_file(): missing.append(str(path))
if missing:
    raise SystemExit('formal run requires workload-specific source configs; missing: '+', '.join(missing))
PY
fi

mkdir -p artifacts/v3_v4
exec 9>artifacts/v3_v4/.single_agent_chain.lock
if ! flock -n 9; then
  echo "[chain] another v3/v4 chain process owns the experiment lock" >&2
  exit 73
fi

RUN_DIR="artifacts/v3_v4/$EXPERIMENT_ID"
mkdir -p "$RUN_DIR"
echo "$$" > "$RUN_DIR/launcher.pid"
echo "$EXPERIMENT_ID" > artifacts/current_experiment_id.txt
echo "$$" > "$RUN_DIR/pid"
echo "$EXPERIMENT_ID" > artifacts/current_v3_v4_experiment_id.txt

child_pid=""
request_stop() {
  echo requested > "$RUN_DIR/STOP"
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" || true
  fi
}
trap request_stop INT TERM
trap 'rm -f "$RUN_DIR/launcher.pid" "$RUN_DIR/pid"' EXIT

python -m app.lab.cli chain preflight > "$RUN_DIR/preflight.json"
if ! python - "$RUN_DIR/preflight.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
passed=', '.join(k for k,v in value.get('checks',{}).items() if v.get('ok'))
failed=', '.join(f"{k}({v.get('error_type','failed')})" for k,v in value.get('checks',{}).items() if not v.get('ok'))
print('[preflight]', value.get('status'), '- passed:', passed, '- failed:', failed or 'none')
raise SystemExit(0 if value.get("ok") else 2)
PY
then
  python - "$RUN_DIR" "$EXPERIMENT_ID" <<'PY'
from pathlib import Path
import shutil,sys
run_dir=Path(sys.argv[1]); experiment_id=sys.argv[2]
artifacts=run_dir.parents[1]
shutil.copy2(run_dir/'preflight.json', artifacts/'latest_preflight_failure.json')
shutil.rmtree(run_dir)
for name in ('current_experiment_id.txt','current_v3_v4_experiment_id.txt'):
    path=artifacts/name
    if path.exists() and path.read_text().strip()==experiment_id:
        path.unlink()
PY
  exit 2
fi

python - <<'PY'
from pathlib import Path
import shutil
path=Path('artifacts/v3_v4/preflight')
if path.exists():
    shutil.rmtree(path)
PY

ANDROMEDA_CONCISE=1 ANDROMEDA_PREFLIGHT_PASSED=1 python -m app.lab.cli chain run-directions \
  --directions "$DIRECTIONS" \
  --mode "$MODE" \
  --experiment-id "$EXPERIMENT_ID" \
  --duration "$DURATION" \
  --warmup "$WARMUP" \
  --terminals "$TERMINALS" \
  --max-experiments "$MAX_EXPERIMENTS" \
  --max-restarts "$MAX_RESTARTS" \
  --max-wall-seconds "$MAX_WALL_SECONDS" \
  --min-tps-percent "$MIN_TPS_PERCENT" \
  --source-config-dir "$SOURCE_CONFIG_DIR" \
  --max-p95-regression-percent "$MAX_P95_REGRESSION_PERCENT" \
  --max-rollback-ratio-increase-pp "$MAX_ROLLBACK_RATIO_INCREASE_PP" \
  --min-screening-groups "$MIN_SCREENING_GROUPS" \
  --patience "$PATIENCE" &
child_pid=$!
wait "$child_pid"

python - "$RUN_DIR" "$DIRECTIONS" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
missing=[]
failed=[]
for raw in sys.argv[2].split(','):
    direction=raw.strip().replace(':','_to_')
    report=root/direction/'chain_report.json'
    rollback=root/direction/'system'/'final_rollback.json'
    if not report.is_file() or not rollback.is_file():
        missing.append(direction)
        continue
    result=json.loads(report.read_text(encoding='utf-8'))
    restored=json.loads(rollback.read_text(encoding='utf-8')).get('restored')
    if result.get('failure') or not restored:
        failed.append({'direction':direction,'failure':result.get('failure'),'restored':restored})
if missing or failed:
    raise SystemExit(f"chain incomplete or unsafe: missing={missing}, failed={failed}")
print('[chain] all requested directions completed and experiment-start configurations were restored')
PY
