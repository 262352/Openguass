#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
venv_dir="${VENV_DIR:-${project_dir}/.venv}"
duration="${SMOKE_DURATION:-5}"
terminals="${SMOKE_TERMINALS:-1}"
check_only=0
skip_data=0
skip_smoke=0
force_reload=0
legacy_ml=0
mirror="${MIRROR:-official}"
current_step=preflight
original_args=("$@")

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap_test_environment.sh [options]

Installs and verifies everything needed by the PostgreSQL/BenchBase test suite.

Options:
  --check-only       Do not install or change data; validate the current environment.
  --skip-data        Do not load BenchBase data or create database snapshots.
  --skip-smoke       Do not execute the three five-second BenchBase smoke workloads.
  --force-reload     Reload all three workloads and recreate their snapshots.
  --with-legacy-ml   Also install the large historical ML requirements.txt stack.
  --mirror NAME      Package mirror: tsinghua, aliyun, or official (default).
  -h, --help         Show this help.

Environment overrides:
  DEEPSEEK_API_KEY, POSTGRES_BENCH_PASSWORD, VENV_DIR,
  SMOKE_DURATION (minimum 5), SMOKE_TERMINALS, FORCE_RELOAD=1,
  MIRROR=tsinghua|aliyun|official.
EOF
}

while (( $# )); do
  case "$1" in
    --check-only) check_only=1 ;;
    --skip-data) skip_data=1 ;;
    --skip-smoke) skip_smoke=1 ;;
    --force-reload) force_reload=1 ;;
    --with-legacy-ml) legacy_ml=1 ;;
    --mirror)
      shift
      (( $# )) || { echo "--mirror requires a value" >&2; exit 2; }
      mirror=$1
      ;;
    --mirror=*) mirror=${1#*=} ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ ${FORCE_RELOAD:-0} == 1 ]] && force_reload=1

case "$mirror" in
  official) pip_index="https://pypi.org/simple" ;;
  tsinghua) pip_index="https://pypi.tuna.tsinghua.edu.cn/simple" ;;
  aliyun) pip_index="https://mirrors.aliyun.com/pypi/simple/" ;;
  *) echo "MIRROR must be tsinghua, aliyun, or official." >&2; exit 2 ;;
esac
export MIRROR="$mirror"

if ! [[ "$duration" =~ ^[0-9]+$ ]] || (( duration < 5 )); then
  echo "SMOKE_DURATION must be an integer of at least 5." >&2
  exit 2
fi
if ! [[ "$terminals" =~ ^[0-9]+$ ]] || (( terminals < 1 )); then
  echo "SMOKE_TERMINALS must be a positive integer." >&2
  exit 2
fi

on_error() {
  code=$?
  printf '[bootstrap] FAILED step=%s exit=%s\n' "$current_step" "$code" >&2
  exit "$code"
}
trap on_error ERR

step() {
  current_step=$1
  printf '\n[bootstrap] %s\n' "$current_step"
}

cd "$project_dir"
export LANG=C
export LC_ALL=C
export PYTHONUNBUFFERED=1
unset LANGUAGE LC_CTYPE || true

if (( ! check_only )) && [[ $(id -u) -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo --preserve-env=DEEPSEEK_API_KEY,POSTGRES_BENCH_PASSWORD,VENV_DIR,SMOKE_DURATION,SMOKE_TERMINALS,FORCE_RELOAD,MIRROR bash "$0" "${original_args[@]}"
  fi
  echo "Run this installer as root; sudo is unavailable." >&2
  exit 1
fi

if (( ! check_only )); then
  step "create local secret configuration"
  python3 - "$project_dir/.env" "$project_dir/.env.example" <<'PY'
from pathlib import Path
import os, secrets, sys

target, example = map(Path, sys.argv[1:])
values = {}
if target.exists():
    for raw in target.read_text(encoding="utf-8").splitlines():
        if raw.strip() and not raw.lstrip().startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
elif example.exists():
    for raw in example.read_text(encoding="utf-8").splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()

for key in ("DEEPSEEK_API_KEY", "POSTGRES_BENCH_PASSWORD"):
    if os.environ.get(key):
        values[key] = os.environ[key]
values.setdefault("PGHOST", "127.0.0.1")
values.setdefault("PGPORT", "5432")
values.setdefault("PGUSER", "andromeda_bench")
if not values.get("POSTGRES_BENCH_PASSWORD"):
    values["POSTGRES_BENCH_PASSWORD"] = secrets.token_urlsafe(24)
order = ["DEEPSEEK_API_KEY", "PGHOST", "PGPORT", "PGUSER", "POSTGRES_BENCH_PASSWORD"]
target.write_text("".join(f"{key}={values.get(key, '')}\n" for key in order), encoding="utf-8")
target.chmod(0o600)
print(".env ready; secrets were not printed")
PY

  step "install PostgreSQL 14, Java 23 and pinned BenchBase"
  "$project_dir/scripts/install_postgres_benchbase.sh"

  step "create Python virtual environment"
  python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"Python >=3.10 is required; found {sys.version.split()[0]}")
PY
  if [[ ! -x "$venv_dir/bin/python" ]]; then
    python3 -m venv "$venv_dir"
  fi
  "$venv_dir/bin/python" -m pip install --index-url "$pip_index" --upgrade pip setuptools wheel
  "$venv_dir/bin/python" -m pip install --index-url "$pip_index" -e "$project_dir" pytest
  if (( legacy_ml )); then
    "$venv_dir/bin/python" -m pip install --index-url "$pip_index" -r "$project_dir/requirements.txt"
  fi
fi

if [[ -x "$venv_dir/bin/python" ]]; then
  python_bin="$venv_dir/bin/python"
else
  python_bin=$(command -v python3)
fi

step "run unit and contract tests"
"$python_bin" -m pytest -q

step "verify PostgreSQL and BenchBase"
pg_isready -h "${PGHOST:-127.0.0.1}" -p "${PGPORT:-5432}"
"$python_bin" - <<'PY'
from app.lab.workloads import PostgresBenchBaseAdapter
from app.phase2.space import FullKnobSpace
assert len(FullKnobSpace().names) == 60
for workload in ("tpcc", "twitter", "ycsb"):
    PostgresBenchBaseAdapter(workload).health_check()
print("BenchBase health passed; PostgreSQL knob catalog contains 60 entries")
PY

if (( check_only )); then
  step "verify prepared snapshots"
  for workload in tpcc twitter ycsb; do
    test -s "artifacts/snapshots/${workload}.dump" || {
      printf 'Missing snapshot: artifacts/snapshots/%s.dump\n' "$workload" >&2
      exit 1
    }
  done
elif (( ! skip_data )); then
  snapshots_ready=1
  for workload in tpcc twitter ycsb; do
    [[ -s "artifacts/snapshots/${workload}.dump" ]] || snapshots_ready=0
  done

  if (( force_reload || ! snapshots_ready )); then
    step "load and execute TPC-C, Twitter and YCSB"
    "$python_bin" -m app.benchmark.cli all --duration "$duration" --terminals "$terminals"
    if (( force_reload )); then
      mkdir -p artifacts/snapshots
      for workload in tpcc twitter ycsb; do
        snapshot="artifacts/snapshots/${workload}.dump"
        [[ -e "$snapshot" ]] && mv "$snapshot" "${snapshot}.before-bootstrap"
      done
    fi
    step "create immutable workload snapshots"
    for workload in tpcc twitter ycsb; do
      "$python_bin" -m app.lab.cli workload prepare "$workload" --reload
    done
  elif (( ! skip_smoke )); then
    step "execute smoke workloads using prepared data"
    "$python_bin" -m app.benchmark.cli all --duration "$duration" --terminals "$terminals" --reuse-data
  fi
fi

step "capture reproducibility metadata"
"$python_bin" -m app.lab.cli environment inspect >/dev/null
"$python_bin" -m app.lab.cli migration report >/dev/null

deepseek_state=missing
if "$python_bin" - <<'PY' >/dev/null 2>&1
from app.config import load_settings
load_settings()
PY
then
  deepseek_state=configured
fi

if [[ -x "$venv_dir/bin/activate" ]]; then
  environment_hint="source \"$venv_dir/bin/activate\""
else
  environment_hint="check-only used system interpreter: $python_bin"
fi

cat <<EOF

[bootstrap] COMPLETE
Python:       $($python_bin --version 2>&1)
PostgreSQL:   $(psql --version)
Java:         $("${HOME}/.local/jdks/jdk-23.0.2+7/bin/java" -version 2>&1 | head -n 1)
BenchBase:    33c00473807ebd49304d114a6d769d2d2b2bbb34
Snapshots:    $(find artifacts/snapshots -maxdepth 1 -name '*.dump' -type f 2>/dev/null | wc -l)/3
DeepSeek key: ${deepseek_state}
Mirror:       ${mirror}

Activate the environment with:
  ${environment_hint}
EOF
