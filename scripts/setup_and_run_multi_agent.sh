#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mirror=${MIRROR:-tsinghua}
prepare_only=0

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/setup_and_run_multi_agent.sh [options]

Options:
  --mirror NAME    tsinghua, aliyun, or official (default: tsinghua)
  --prepare-only   Install and validate everything without starting an experiment
  -h, --help       Show this help

Experiment settings are environment variables: SOURCE_WORKLOAD, TARGET_WORKLOAD,
MAX_ROUNDS, MAX_MEASUREMENTS, DURATION, WARMUP_SECONDS, TERMINALS,
BASELINE_REPEATS, DRIFT_PERCENT and BENCHMARK_SEED.
EOF
}

while (($#)); do
  case "$1" in
    --mirror) shift; (($#)) || { echo "--mirror requires a value" >&2; exit 2; }; mirror=$1 ;;
    --mirror=*) mirror=${1#*=} ;;
    --prepare-only) prepare_only=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ $(id -u) -eq 0 ]] || { echo "Run as root with sudo." >&2; exit 1; }
cd "$project_dir"
[[ -f .env ]] || cp .env.example .env
chmod 600 .env

configured_key=$(python3 - <<'PY'
from pathlib import Path
for line in Path('.env').read_text(encoding='utf-8').splitlines():
    if line.startswith('DEEPSEEK_API_KEY=') and line.split('=', 1)[1].strip():
        print('yes')
        break
PY
)
if [[ $configured_key != yes ]]; then
  [[ -t 0 ]] || { echo "DEEPSEEK_API_KEY is empty in .env and no terminal is available." >&2; exit 1; }
  read -rsp "DeepSeek API Key: " deepseek_key
  echo
  [[ -n $deepseek_key ]] || { echo "API Key cannot be empty." >&2; exit 1; }
  ENV_VALUE="$deepseek_key" python3 - <<'PY'
from pathlib import Path
import os

path = Path('.env')
lines = path.read_text(encoding='utf-8').splitlines()
value = os.environ['ENV_VALUE']
updated, found = [], False
for line in lines:
    if line.startswith('DEEPSEEK_API_KEY='):
        updated.append('DEEPSEEK_API_KEY=' + value)
        found = True
    else:
        updated.append(line)
if not found:
    updated.append('DEEPSEEK_API_KEY=' + value)
path.write_text('\n'.join(updated) + '\n', encoding='utf-8')
path.chmod(0o600)
PY
  unset deepseek_key
fi

MIRROR="$mirror" bash scripts/bootstrap_test_environment.sh
((prepare_only == 0)) || exit 0

mkdir -p artifacts
experiment_id=${EXPERIMENT_ID:-multi-agent-$(date -u +%Y%m%dT%H%M%SZ)}
log_file="artifacts/${experiment_id}.nohup.log"

nohup env \
  PATH="${project_dir}/.venv/bin:${PATH}" \
  EXPERIMENT_ID="$experiment_id" \
  SOURCE_WORKLOAD="${SOURCE_WORKLOAD:-tpcc}" \
  TARGET_WORKLOAD="${TARGET_WORKLOAD:-twitter}" \
  MAX_ROUNDS="${MAX_ROUNDS:-12}" \
  MAX_MEASUREMENTS="${MAX_MEASUREMENTS:-60}" \
  DURATION="${DURATION:-120}" \
  WARMUP_SECONDS="${WARMUP_SECONDS:-30}" \
  TERMINALS="${TERMINALS:-}" \
  BASELINE_REPEATS="${BASELINE_REPEATS:-3}" \
  DRIFT_PERCENT="${DRIFT_PERCENT:-25}" \
  BENCHMARK_SEED="${BENCHMARK_SEED:-20261012}" \
  bash scripts/run_multi_agent_diagnosis.sh >"$log_file" 2>&1 &

pid=$!
printf '%s\n' "$pid" > artifacts/current_multi_agent_pid.txt
printf '%s\n' "$experiment_id" > artifacts/current_multi_agent_experiment_id.txt

cat <<EOF
[setup-and-run] experiment started
PID=$pid
EXPERIMENT_ID=$experiment_id
LOG=$project_dir/$log_file

Follow progress:
  tail -f "$project_dir/$log_file"
EOF
