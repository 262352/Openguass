#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
venv_dir="${VENV_DIR:-${project_dir}/.venv}"
duration="${SMOKE_DURATION:-5}"
terminals="${SMOKE_TERMINALS:-1}"
mirror="${MIRROR:-official}"
check_only=0
force_reload=0
skip_smoke=0
current_step=preflight

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap_test_environment.sh [options]

Prepare the complete Ubuntu 22.04 aarch64 PostgreSQL/BenchBase environment.
Existing .env values and existing PostgreSQL passwords are never replaced.

Options:
  --check-only       Validate without installing or changing data.
  --force-reload     Reload all workloads and recreate all snapshots.
  --skip-smoke       Prepare snapshots without the final reuse-data smoke run.
  --mirror NAME      tsinghua, aliyun, or official (default).
  -h, --help         Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --check-only) check_only=1 ;;
    --force-reload) force_reload=1 ;;
    --skip-smoke) skip_smoke=1 ;;
    --mirror) shift; (($#)) || { echo "--mirror requires a value" >&2; exit 2; }; mirror=$1 ;;
    --mirror=*) mirror=${1#*=} ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$mirror" in
  official) pip_index=https://pypi.org/simple ;;
  tsinghua) pip_index=https://pypi.tuna.tsinghua.edu.cn/simple ;;
  aliyun) pip_index=https://mirrors.aliyun.com/pypi/simple/ ;;
  *) echo "MIRROR must be tsinghua, aliyun, or official." >&2; exit 2 ;;
esac
[[ $duration =~ ^[0-9]+$ ]] && ((duration >= 5)) || { echo "SMOKE_DURATION must be >= 5." >&2; exit 2; }
[[ $terminals =~ ^[0-9]+$ ]] && ((terminals >= 1)) || { echo "SMOKE_TERMINALS must be >= 1." >&2; exit 2; }

fail() { local code=$?; echo "[bootstrap] FAILED step=${current_step} exit=${code}" >&2; exit "$code"; }
trap fail ERR
step() { current_step=$1; printf '\n[bootstrap] %s\n' "$current_step"; }

cd "$project_dir"
export LANG=C LC_ALL=C PYTHONUNBUFFERED=1 MIRROR="$mirror"
unset LANGUAGE LC_CTYPE || true

if ((check_only == 0)); then
  [[ $(id -u) -eq 0 ]] || { echo "Run as root: sudo bash scripts/bootstrap_test_environment.sh" >&2; exit 1; }

  step "preserve local configuration"
  python3 - "$project_dir/.env" "$project_dir/.env.example" <<'PY'
from pathlib import Path
import os, sys

target, example = map(Path, sys.argv[1:])
if not target.exists():
    target.write_text(example.read_text(encoding="utf-8") if example.exists() else "", encoding="utf-8")
lines = target.read_text(encoding="utf-8").splitlines()
present = {line.split("=", 1)[0].strip() for line in lines if "=" in line and not line.lstrip().startswith("#")}
defaults = {"PGHOST": "127.0.0.1", "PGPORT": "5432", "PGUSER": "andromeda_bench"}
for key, default in defaults.items():
    if key not in present:
        lines.append(f"{key}={default}")
for key in ("DEEPSEEK_API_KEY", "POSTGRES_BENCH_PASSWORD"):
    if key not in present:
        lines.append(f"{key}={os.environ.get(key, '')}")
target.write_text("\n".join(lines) + "\n", encoding="utf-8")
target.chmod(0o600)
print(".env preserved; existing secrets were not changed")
PY

  step "install PostgreSQL 14, aarch64 Java 23 and pinned BenchBase"
  "$project_dir/scripts/install_postgres_benchbase.sh"

  step "create Python environment and install project dependencies"
  python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"Python >=3.10 is required; found {sys.version.split()[0]}")
PY
  [[ -x "$venv_dir/bin/python" ]] || python3 -m venv "$venv_dir"
  "$venv_dir/bin/python" -m pip install --index-url "$pip_index" --upgrade pip setuptools wheel
  "$venv_dir/bin/python" -m pip install --index-url "$pip_index" -e "$project_dir" pytest
fi

python_bin="$venv_dir/bin/python"
[[ -x $python_bin ]] || { echo "Missing Python environment: $python_bin" >&2; exit 1; }
set -a
# shellcheck disable=SC1091
source "$project_dir/.env"
set +a

step "run unit and contract tests"
"$python_bin" -m pytest -q

step "verify PostgreSQL, BenchBase and 60-knob catalog"
pg_isready -h "${PGHOST:-127.0.0.1}" -p "${PGPORT:-5432}"
"$python_bin" - <<'PY'
from app.lab.workloads import PostgresBenchBaseAdapter
from app.phase2.space import FullKnobSpace
assert len(FullKnobSpace().names) == 60
for workload in ("tpcc", "twitter", "ycsb"):
    PostgresBenchBaseAdapter(workload).health_check()
print("PostgreSQL, BenchBase and 60-knob catalog are ready")
PY

if ((check_only)); then
  step "verify non-empty snapshots"
  for workload in tpcc twitter ycsb; do
    snapshot="artifacts/snapshots/${workload}.dump"
    [[ -s $snapshot ]] || { echo "Missing or empty snapshot: $snapshot" >&2; exit 1; }
    pg_restore --list "$snapshot" >/dev/null
  done
else
  mkdir -p artifacts/snapshots
  find artifacts/snapshots -maxdepth 1 -name '*.dump' -type f -size 0 -delete
  snapshots_ready=1
  for workload in tpcc twitter ycsb; do
    [[ -s "artifacts/snapshots/${workload}.dump" ]] || snapshots_ready=0
  done

  if ((force_reload || ! snapshots_ready)); then
    step "load and execute TPC-C, Twitter and YCSB"
    "$python_bin" -m app.benchmark.cli all --duration "$duration" --terminals "$terminals"
    step "create fresh immutable snapshots"
    for workload in tpcc twitter ycsb; do
      rm -f "artifacts/snapshots/${workload}.dump" "artifacts/snapshots/${workload}.dump.tmp"
      "$python_bin" -m app.lab.cli workload prepare "$workload" --reload
      test -s "artifacts/snapshots/${workload}.dump"
      pg_restore --list "artifacts/snapshots/${workload}.dump" >/dev/null
    done
  elif ((skip_smoke == 0)); then
    step "execute smoke workloads from prepared data"
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
then deepseek_state=configured; fi

cat <<EOF

[bootstrap] COMPLETE
Python:       $($python_bin --version 2>&1)
PostgreSQL:   $(psql --version)
Java:         $("${HOME}/.local/jdks/jdk-23.0.2+7/bin/java" -version 2>&1 | head -n 1)
BenchBase:    33c00473807ebd49304d114a6d769d2d2b2bbb34
Snapshots:    $(find artifacts/snapshots -maxdepth 1 -name '*.dump' -type f -size +0c 2>/dev/null | wc -l)/3
DeepSeek key: ${deepseek_state}
Mirror:       ${mirror}
EOF
