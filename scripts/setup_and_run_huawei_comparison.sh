#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mirror=${MIRROR:-tsinghua}
skip_bootstrap=0

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/setup_and_run_huawei_comparison.sh [options]

Prepare an Ubuntu 22.04 aarch64 Huawei/SCOW node and then start one complete
TPC-C -> Twitter comparison in the background:
  1. 100-trial TPC-C BO source reference;
  2. 100-trial Twitter BO comparison reference and repeated verification;
  3. evidence-graph diagnosis from TPC-C configuration to Twitter;
  4. offline final comparison against the hidden Twitter BO reference.

Options:
  --mirror NAME       tsinghua, aliyun, or official (default: tsinghua)
  --skip-bootstrap    Reuse an already prepared environment and snapshots
  -h, --help          Show this help

The DeepSeek key is loaded from .env or DEEPSEEK_API_KEY. On a fresh interactive
installation the script asks once and writes it to .env with mode 0600.
EOF
}

while (($#)); do
  case "$1" in
    --mirror) shift; (($#)) || { echo "--mirror requires a value" >&2; exit 2; }; mirror=$1 ;;
    --mirror=*) mirror=${1#*=} ;;
    --skip-bootstrap) skip_bootstrap=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ $(id -u) -eq 0 ]] || { echo "Run as root with sudo." >&2; exit 1; }
cd "$project_dir"
# shellcheck disable=SC1091
source "$project_dir/scripts/snapshot_profile.sh"
[[ -f .env ]] || cp .env.example .env
chmod 600 .env

if [[ -n ${DEEPSEEK_API_KEY:-} ]]; then
  ENV_VALUE="$DEEPSEEK_API_KEY" python3 - <<'PY'
from pathlib import Path
import os
path = Path('.env')
lines = path.read_text(encoding='utf-8').splitlines()
value = os.environ['ENV_VALUE']
out, found = [], False
for line in lines:
    if line.startswith('DEEPSEEK_API_KEY='):
        out.append('DEEPSEEK_API_KEY=' + value); found = True
    else:
        out.append(line)
if not found: out.append('DEEPSEEK_API_KEY=' + value)
path.write_text('\n'.join(out) + '\n', encoding='utf-8')
path.chmod(0o600)
PY
fi

configured_key=$(sed -n 's/^DEEPSEEK_API_KEY=//p' .env | head -n 1)
if [[ -z $configured_key ]]; then
  [[ -t 0 ]] || { echo "DEEPSEEK_API_KEY is empty; set it in .env or the environment." >&2; exit 1; }
  read -rsp "DeepSeek API Key: " deepseek_key
  echo
  [[ -n $deepseek_key ]] || { echo "API Key cannot be empty." >&2; exit 1; }
  ENV_VALUE="$deepseek_key" python3 - <<'PY'
from pathlib import Path
import os
path = Path('.env')
lines = path.read_text(encoding='utf-8').splitlines()
value = os.environ['ENV_VALUE']
out, found = [], False
for line in lines:
    if line.startswith('DEEPSEEK_API_KEY='):
        out.append('DEEPSEEK_API_KEY=' + value); found = True
    else:
        out.append(line)
if not found: out.append('DEEPSEEK_API_KEY=' + value)
path.write_text('\n'.join(out) + '\n', encoding='utf-8')
path.chmod(0o600)
PY
  unset deepseek_key
fi
unset configured_key

if ((skip_bootstrap == 0)); then
  MIRROR="$mirror" bash scripts/bootstrap_test_environment.sh
else
  [[ -x .venv/bin/python ]] || { echo "Missing .venv; run without --skip-bootstrap." >&2; exit 1; }
  [[ -s .benchbase/target/benchbase-postgres/benchbase.jar ]] || {
    echo "Missing BenchBase; run without --skip-bootstrap." >&2; exit 1;
  }
  for workload in tpcc twitter; do
    snapshot="artifacts/snapshots/${workload}.dump"
    [[ -s $snapshot ]] || { echo "Missing or empty snapshot: $snapshot" >&2; exit 1; }
    pg_restore --list "$snapshot" >/dev/null
  done
  profile_file="artifacts/snapshots/workload_profile.sha256"
  snapshot_profile_matches "$profile_file" || {
    echo "Snapshots were built for an older workload profile; run without --skip-bootstrap." >&2; exit 1;
  }
  pg_isready -h 127.0.0.1 -p 5432 >/dev/null
  .venv/bin/python - <<'PY'
from app.phase2.space import FullKnobSpace
assert len(FullKnobSpace().names) == 62
print("Prepared TPC-C/Twitter environment is valid")
PY
fi

mkdir -p artifacts
suite_id=${SUITE_ID:-huawei-tpcc-twitter-$(date -u +%Y%m%dT%H%M%SZ)}
log_file="artifacts/${suite_id}.nohup.log"

nohup env \
  PATH="${project_dir}/.venv/bin:${PATH}" \
  STAGE=all \
  SUITE_ID="$suite_id" \
  TPCC_BO_BUDGET="${TPCC_BO_BUDGET:-100}" \
  TWITTER_BO_BUDGET="${TWITTER_BO_BUDGET:-100}" \
  BO_INITIAL_POINTS="${BO_INITIAL_POINTS:-10}" \
  BO_RETEST_REPEATS="${BO_RETEST_REPEATS:-3}" \
  DIRECTIONS="tpcc:twitter" \
  REPETITIONS="${REPETITIONS:-1}" \
  DIAGNOSIS_ROUNDS="${DIAGNOSIS_ROUNDS:-12}" \
  DIAGNOSIS_MEASUREMENTS="${DIAGNOSIS_MEASUREMENTS:-60}" \
  DURATION="${DURATION:-120}" \
  WARMUP_SECONDS="${WARMUP_SECONDS:-30}" \
  TPCC_TERMINALS="${TPCC_TERMINALS:-3}" \
  TWITTER_TERMINALS="${TWITTER_TERMINALS:-5}" \
  BASELINE_REPEATS="${BASELINE_REPEATS:-3}" \
  SOURCE_PROFILE_REPEATS="${SOURCE_PROFILE_REPEATS:-1}" \
  MAX_OBSERVATION_ROUNDS="${MAX_OBSERVATION_ROUNDS:-2}" \
  SNAPSHOT_SETTLE_SECONDS="${SNAPSHOT_SETTLE_SECONDS:-5}" \
  BENCHMARK_SEED="${BENCHMARK_SEED:-20261012}" \
  bash scripts/run_bo_reference_then_switch.sh >"$log_file" 2>&1 &

pid=$!
printf '%s\n' "$pid" > artifacts/current_workload_switch_pid.txt
printf '%s\n' "$suite_id" > artifacts/current_workload_switch_suite_id.txt

cat <<EOF
[huawei-comparison] started
PID=$pid
SUITE_ID=$suite_id
LOG=$project_dir/$log_file

Progress:
  tail -f "$project_dir/$log_file"

Twitter BO reference after BO finishes:
  cat "$project_dir/artifacts/workload_switch/$suite_id/REFERENCE_STANDARD.md"

Final comparison after all stages finish:
  cat "$project_dir/artifacts/workload_switch/$suite_id/FINAL_COMPARISON.md"
EOF
