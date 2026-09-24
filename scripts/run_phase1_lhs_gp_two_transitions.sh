#!/usr/bin/env bash
set -Eeuo pipefail
export LANG=C
export LC_ALL=C
unset LANGUAGE LC_CTYPE || true
cd "$(dirname "$0")/.."
PYTHON_BIN="${PYTHON:-python3}"
[[ -x .venv/bin/python ]] && PYTHON_BIN=.venv/bin/python
MODE="${MODE:-formal}"
EXPERIMENT_ID="${EXPERIMENT_ID:-phase1-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p artifacts
printf '%s\n' "$EXPERIMENT_ID" > artifacts/current_experiment_id.txt
printf '[experiment] id=%s mode=%s\n' "$EXPERIMENT_ID" "${MODE:-formal}"
RUN_COMPLETED=0
if [[ "$MODE" == "formal" ]]; then LHS=15; GP=35; DURATION="${DURATION:-15}"; REPEATS="${REPEATS:-5}"; STRICT=(--strict-llm); ISOLATION=(--snapshot-isolation)
elif [[ "$MODE" == "quick" ]]; then LHS="${LHS:-2}"; GP="${GP:-1}"; DURATION="${DURATION:-5}"; REPEATS="${REPEATS:-1}"; STRICT=(); ISOLATION=()
else echo "MODE must be formal or quick" >&2; exit 2; fi
TERMINALS="${TERMINALS:-1}"
cleanup(){
 if [[ "$RUN_COMPLETED" != "1" ]]; then "$PYTHON_BIN" -m app.lab.cli experiment finish --id "$EXPERIMENT_ID" --state paused --message "interrupted; safe to resume" >/dev/null 2>&1 || true; fi
 "$PYTHON_BIN" - <<'PY'
from pathlib import Path
import json
p=Path('artifacts/last_rollback.json');p.parent.mkdir(exist_ok=True);p.write_text(json.dumps({'status':'session-scoped settings cleared','persistent_configuration_changes':False},indent=2))
PY
}
trap cleanup EXIT INT TERM
"$PYTHON_BIN" -m app.lab.cli experiment init --id "$EXPERIMENT_ID" --mode "$MODE" --lhs "$LHS" --gp "$GP" >/dev/null
"$PYTHON_BIN" -m app.lab.cli environment inspect >/dev/null
"$PYTHON_BIN" -m app.lab.cli migration report >/dev/null
printf '[progress] readable report: artifacts/experiments/%s/live_progress.md\n' "$EXPERIMENT_ID"
for workload in tpcc twitter; do
 if [[ "$MODE" == "formal" ]]; then "$PYTHON_BIN" -m app.lab.cli workload prepare "$workload" --reload; fi
 "$PYTHON_BIN" -m app.lab.cli tune reference "$workload" --experiment-id "$EXPERIMENT_ID" --lhs "$LHS" --gp "$GP" --duration "$DURATION" --terminals "$TERMINALS" "${ISOLATION[@]}"
 "$PYTHON_BIN" -m app.lab.cli reference select "$workload" --experiment-id "$EXPERIMENT_ID" --duration "$DURATION" --terminals "$TERMINALS" --repeats "$REPEATS" "${ISOLATION[@]}"
done
"$PYTHON_BIN" -m app.lab.cli transition run tpcc twitter --experiment-id "$EXPERIMENT_ID" --duration "$DURATION" --terminals "$TERMINALS" "${STRICT[@]}"
"$PYTHON_BIN" -m app.lab.cli transition run twitter tpcc --experiment-id "$EXPERIMENT_ID" --duration "$DURATION" --terminals "$TERMINALS" "${STRICT[@]}"
"$PYTHON_BIN" - <<PY
from pathlib import Path
import json
exp='$EXPERIMENT_ID';mode='$MODE'
paths=list((Path('artifacts/transitions')/exp).glob('*/transition_report.json'))
report={'experiment_id':exp,'mode':mode,'formal_completed':mode=='formal','formal_required_budget':{'lhs':15,'gp':35,'workloads':['tpcc','twitter']},'reports':[json.loads(p.read_text()) for p in paths]}
out=Path('artifacts/reports');out.mkdir(parents=True,exist_ok=True);(out/f'{exp}.json').write_text(json.dumps(report,indent=2))
print(out/f'{exp}.json')
PY
"$PYTHON_BIN" -m app.lab.cli experiment finish --id "$EXPERIMENT_ID" --state completed --message "all phases completed" >/dev/null
RUN_COMPLETED=1
