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
PostgreSQL role is dropped and recreated on each run with a fixed password
(test environments only).

Options:
  --check-only       Validate without installing or changing data.
  --force-reload     Reload all workloads and recreate all snapshots.
  --skip-smoke       Prepare snapshots without the final reuse-data smoke run.
  --mirror NAME      tsinghua, aliyun, or official (default).
  -h, --help         Show this help.

Environment:
  CURL_RETRIES       Retry count for download commands (default: 5).
  CURL_TIMEOUT       Connect timeout seconds (default: 20).
  CURL_MAX_TIME      Max transfer seconds (default: 1800).
  DOWNLOAD_PROXY     Optional HTTP(S) proxy for downloads, e.g. http://host:port
  BENCHBASE_COMMIT   Pinned BenchBase commit (default: 33c00473807ebd49304d114a6d769d2d2b2bbb34)
  BENCHBASE_MIRRORS  Space-separated list of clone URLs to try, in order.
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

# ---------------------------------------------------------------------------
# Fixed test credentials and helpers
# ---------------------------------------------------------------------------
PG_ROLE="${PGUSER:-andromeda_bench}"
PG_DATABASE="${PGDATABASE:-andromeda_bench}"
PG_HOST_ADDR="127.0.0.1"
PG_PORT_NUM="5432"
# Fixed password for test environments only.
BENCH_DB_PASSWORD="AndromedaBench2026"

# BenchBase requires one PostgreSQL database per workload. They are managed
# alongside the main database: all four are dropped and recreated on every
# run, so they always start empty and owned by the freshly-created role.
WORKLOAD_DATABASES="tpcc twitter ycsb"

# Pinned BenchBase commit (must match install_postgres_benchbase.sh).
BENCHBASE_COMMIT="${BENCHBASE_COMMIT:-33c00473807ebd49304d114a6d769d2d2b2bbb34}"

# Mirror list, tried in order.
BENCHBASE_MIRRORS="${BENCHBASE_MIRRORS:-https://ghfast.top/https://github.com/cmu-db/benchbase.git https://gh-proxy.com/https://github.com/cmu-db/benchbase.git https://github.com/cmu-db/benchbase.git}"

LEGACY_MIRROR_NAMES="pom.xml settings.xml src config data docker lib mvnw mvnw.cmd \
.devcontainer .editorconfig .git-blame-ignore-revs .github .pylintrc .run .vscode \
CONTRIBUTING.md CONTRIBUTORS.md LICENSE"

# Run a command as the postgres OS user.
#
# We explicitly unset all PG* client env vars before delegating. install_*
# is called with PGDATABASE exported so it can talk to andromeda_bench, but
# our own admin psql calls must always connect to the default 'postgres'
# database.
run_as_postgres() {
  if command -v sudo >/dev/null 2>&1; then
    ( cd / && env -u PGDATABASE -u PGHOST -u PGPORT -u PGUSER -u PGPASSWORD \
        sudo -u postgres "$@" )
  elif command -v runuser >/dev/null 2>&1; then
    ( cd / && env -u PGDATABASE -u PGHOST -u PGPORT -u PGUSER -u PGPASSWORD \
        runuser -u postgres -- "$@" )
  else
    ( cd / && env -u PGDATABASE -u PGHOST -u PGPORT -u PGUSER -u PGPASSWORD \
        su -s /bin/bash postgres -c "$(printf '%q ' "$@")" )
  fi
}

write_pg_env() {
  BENCH_DB_PASSWORD="$BENCH_DB_PASSWORD" PG_ROLE="$PG_ROLE" PG_DATABASE="$PG_DATABASE" \
  PG_HOST_ADDR="$PG_HOST_ADDR" PG_PORT_NUM="$PG_PORT_NUM" \
  python3 - "$project_dir/.env" <<'PY'
from pathlib import Path
import os, sys

path = Path(sys.argv[1])
updates = {
    "PGHOST": os.environ["PG_HOST_ADDR"],
    "PGPORT": os.environ["PG_PORT_NUM"],
    "PGUSER": os.environ["PG_ROLE"],
    "PGDATABASE": os.environ["PG_DATABASE"],
    "PGPASSWORD": os.environ["BENCH_DB_PASSWORD"],
    "POSTGRES_BENCH_PASSWORD": os.environ["BENCH_DB_PASSWORD"],
}
lines = path.read_text(encoding="utf-8").splitlines()
out, seen = [], set()
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
path.chmod(0o600)
print("PostgreSQL credentials written to .env")
PY
}

# ---------------------------------------------------------------------------
# Recreate role + databases from scratch.
#
# All four business databases (andromeda_bench + tpcc + twitter + ycsb) are
# dropped and recreated empty. This keeps the environment fully reproducible
# and avoids the "DROP ROLE fails because it still owns objects" trap.
#
# Because the workload databases are wiped, the corresponding workload
# snapshots (artifacts/snapshots/*.dump) become stale, so we delete them too.
# The next step then automatically takes the --force-reload path and
# re-populates everything.
# ---------------------------------------------------------------------------
recreate_pg_role_and_db() {
  pg_isready -h "$PG_HOST_ADDR" -p "$PG_PORT_NUM" >/dev/null 2>&1 || return 0

  local all_databases="$PG_DATABASE $WORKLOAD_DATABASES"
  local db

  # 1) Drop every business database.
  for db in $all_databases; do
    # terminate connections to this db
    run_as_postgres psql -v ON_ERROR_STOP=1 <<SQL || true
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = '${db}' AND pid <> pg_backend_pid();
SQL
    if run_as_postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
      echo "Dropping database ${db}"
      run_as_postgres dropdb --if-exists "${db}"
    fi
  done

  # 2) Strip any role remnants in the postgres system database. Since all
  #    business DBs were dropped above, this is mostly a no-op but it
  #    guarantees DROP ROLE below cannot fail.
  run_as_postgres psql -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    BEGIN
        EXECUTE 'REASSIGN OWNED BY ${PG_ROLE} TO postgres';
    EXCEPTION WHEN undefined_object THEN NULL;
    END;
    BEGIN
        EXECUTE 'DROP OWNED BY ${PG_ROLE} CASCADE';
    EXCEPTION WHEN undefined_object THEN NULL;
    END;
END
\$\$;
SQL

  # 3) Drop the role.
  run_as_postgres psql -v ON_ERROR_STOP=1 <<SQL
DROP ROLE IF EXISTS ${PG_ROLE};
SQL

  # 4) Create a fresh role.
  run_as_postgres psql -v ON_ERROR_STOP=1 <<SQL
CREATE ROLE ${PG_ROLE} LOGIN CREATEDB CREATEROLE PASSWORD '${BENCH_DB_PASSWORD}';
SQL

  # 5) Recreate every business database owned by the fresh role.
  for db in $all_databases; do
    run_as_postgres createdb --owner="${PG_ROLE}" "${db}"
    run_as_postgres psql -v ON_ERROR_STOP=1 -c \
      "GRANT ALL PRIVILEGES ON DATABASE ${db} TO ${PG_ROLE};" >/dev/null
  done

  # 6) Ensure pg_hba.conf allows password auth from 127.0.0.1.
  local pg_hba
  pg_hba=$(run_as_postgres psql -tAc "SHOW hba_file;")
  if ! grep -Eq '^host\s+all\s+all\s+127\.0\.0\.1/32\s+(md5|scram-sha-256)' "$pg_hba"; then
    echo "host    all    all    127.0.0.1/32    scram-sha-256" | tee -a "$pg_hba" >/dev/null
    run_as_postgres psql -c "SELECT pg_reload_conf();" >/dev/null
  fi

  # 7) Verify TCP login with the fixed password to every database.
  for db in $all_databases; do
    PGPASSWORD="$BENCH_DB_PASSWORD" psql \
      -h "$PG_HOST_ADDR" -p "$PG_PORT_NUM" \
      -U "$PG_ROLE" -d "$db" \
      -c "SELECT 1;" >/dev/null
  done

  # 8) Invalidate snapshots: the workload databases were just recreated empty,
  #    so the next step must reload them from scratch.
  if compgen -G "$project_dir/artifacts/snapshots/*.dump" >/dev/null; then
    echo "Removing stale workload snapshots"
    rm -f "$project_dir"/artifacts/snapshots/*.dump
  fi
}

# ---------------------------------------------------------------------------
# curl / wget wrappers
# ---------------------------------------------------------------------------
setup_curl_wrapper() {
  local wrapper_dir="${project_dir}/.cache/wrappers"
  mkdir -p "$wrapper_dir"

  local retries="${CURL_RETRIES:-5}"
  local connect_timeout="${CURL_TIMEOUT:-20}"
  local max_time="${CURL_MAX_TIME:-1800}"
  local proxy_opt=""
  if [[ -n "${DOWNLOAD_PROXY:-}" ]]; then
    proxy_opt="--proxy ${DOWNLOAD_PROXY}"
  fi

  cat > "${wrapper_dir}/curl" <<SH
#!/usr/bin/env bash
exec /usr/bin/curl \\
  --retry ${retries} \\
  --retry-delay 5 \\
  --retry-all-errors \\
  --retry-connrefused \\
  --connect-timeout ${connect_timeout} \\
  --max-time ${max_time} \\
  --speed-time 60 --speed-limit 1024 \\
  ${proxy_opt} \\
  "\$@"
SH
  chmod +x "${wrapper_dir}/curl"

  cat > "${wrapper_dir}/wget" <<SH
#!/usr/bin/env bash
exec /usr/bin/wget \\
  --tries=${retries} \\
  --timeout=${connect_timeout} \\
  --waitretry=5 \\
  --read-timeout=${max_time} \\
  ${proxy_opt} \\
  "\$@"
SH
  chmod +x "${wrapper_dir}/wget"

  export PATH="${wrapper_dir}:${PATH}"
  echo "curl/wget wrappers enabled: retries=${retries}, connect_timeout=${connect_timeout}s, max_time=${max_time}s"
}

# ---------------------------------------------------------------------------
# BenchBase pre-clone
# ---------------------------------------------------------------------------
prepare_benchbase_checkout() {
  local benchbase_dir="${project_dir}/.benchbase"
  local target="$BENCHBASE_COMMIT"

  if [[ -d "$benchbase_dir/.git" ]]; then
    if git -C "$benchbase_dir" rev-parse --verify HEAD >/dev/null 2>&1; then
      local current
      current=$(git -C "$benchbase_dir" rev-parse HEAD)
      if [[ "$current" == "$target" ]]; then
        echo ".benchbase already at ${target:0:12}"
        return 0
      fi
    fi
    echo "Removing broken .benchbase (HEAD does not match ${target:0:12})"
    rm -rf "$benchbase_dir"
  fi

  local mirror_url
  for mirror_url in $BENCHBASE_MIRRORS; do
    echo "Trying BenchBase mirror: $mirror_url"
    rm -rf "$benchbase_dir"
    mkdir -p "$benchbase_dir"

    if ! git -C "$benchbase_dir" init -q 2>/dev/null; then
      echo "  git init failed in $benchbase_dir"
      continue
    fi
    git -C "$benchbase_dir" remote add origin "$mirror_url"

    echo "  fetching commit ${target:0:12} (depth 1) ..."
    if ! git -C "$benchbase_dir" \
         -c http.lowSpeedLimit=1000 \
         -c http.lowSpeedTime=60 \
         -c http.postBuffer=524288000 \
         fetch --depth 1 origin "$target"; then
      echo "  fetch failed from $mirror_url"
      continue
    fi

    echo "  checking out FETCH_HEAD ..."
    if ! git -C "$benchbase_dir" checkout -q FETCH_HEAD; then
      echo "  checkout failed from $mirror_url"
      continue
    fi

    local head
    head=$(git -C "$benchbase_dir" rev-parse HEAD)
    if [[ "$head" == "$target" ]]; then
      echo "BenchBase ready at ${target:0:12}"
      return 0
    fi
    echo "  HEAD $head != $target"
  done

  echo "Failed to prepare BenchBase from any mirror. Tried:" >&2
  echo "  $BENCHBASE_MIRRORS" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Wrap .benchbase/mvnw so Maven always runs with cwd = .benchbase.
# ---------------------------------------------------------------------------
mirror_benchbase_build_files() {
  local benchbase_dir="${project_dir}/.benchbase"

  if [[ ! -d "$benchbase_dir" ]]; then
    echo "Warning: $benchbase_dir does not exist; nothing to wrap." >&2
    return 0
  fi

  local p
  for p in "$project_dir/mvnw" "$benchbase_dir/mvnw" \
           "$benchbase_dir/mvnw.orig" "$benchbase_dir/mvnw.real"; do
    if [[ -L "$p" ]]; then
      echo "Removing symlink $p"
      rm -f "$p"
    fi
  done

  if [[ -d "$benchbase_dir/.git" ]]; then
    git -C "$benchbase_dir" checkout -- mvnw 2>/dev/null || true
  fi

  rm -f "$benchbase_dir/mvnw.real" "$benchbase_dir/mvnw.orig"

  if [[ ! -f "$benchbase_dir/mvnw" ]]; then
    echo "ERROR: $benchbase_dir/mvnw missing after git restore" >&2
    return 1
  fi
  if grep -q 'mvnw\.orig\|mvnw\.real' "$benchbase_dir/mvnw"; then
    echo "ERROR: $benchbase_dir/mvnw still looks like a wrapper; git restore failed." >&2
    return 1
  fi

  cp -a "$benchbase_dir/mvnw" "$benchbase_dir/mvnw.orig"

  rm -f "$benchbase_dir/mvnw"
  cat > "$benchbase_dir/mvnw" <<SH
#!/usr/bin/env bash
set -Eeuo pipefail
cd "${benchbase_dir}"
exec "${benchbase_dir}/mvnw.orig" "\$@"
SH
  chmod +x "$benchbase_dir/mvnw"
  echo "Wrapped .benchbase/mvnw -> mvnw.orig (absolute paths)"

  rm -f "$project_dir/mvnw"
  cat > "$project_dir/mvnw" <<SH
#!/usr/bin/env bash
set -Eeuo pipefail
cd "${benchbase_dir}"
exec "${benchbase_dir}/mvnw.orig" "\$@"
SH
  chmod +x "$project_dir/mvnw"
  echo "Created $project_dir/mvnw delegating to .benchbase"

  local name
  for name in $LEGACY_MIRROR_NAMES; do
    if [[ -L "$project_dir/$name" ]]; then
      rm -f "$project_dir/$name"
      echo "Removed stale symlink $project_dir/$name"
    fi
  done

  if [[ -d "$project_dir/target" && ! -L "$project_dir/target" ]]; then
    echo "Removing stale $project_dir/target"
    rm -rf "$project_dir/target"
  fi

  mkdir -p "$benchbase_dir/target"
}

# ---------------------------------------------------------------------------
# Run pytest with a clean environment.
# ---------------------------------------------------------------------------
run_pytest_isolated() {
  local python_bin="$1"
  local env_path="$project_dir/.env"
  local backup=""
  local rc=0

  if [[ -f "$env_path" ]]; then
    backup="$env_path.pytest-bak.$$"
    mv "$env_path" "$backup"
  fi

  set +e
  env -u POSTGRES_BENCH_PASSWORD \
      -u PGPASSWORD -u PGHOST -u PGPORT -u PGUSER -u PGDATABASE \
      -u DEEPSEEK_API_KEY \
      "$python_bin" -m pytest -q
  rc=$?
  set -e

  if [[ -n "$backup" && -f "$backup" ]]; then
    mv "$backup" "$env_path"
  fi

  return "$rc"
}

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

  step "enable resilient downloads (curl/wget wrappers)"
  setup_curl_wrapper

  step "pre-align PostgreSQL role (before install)"
  recreate_pg_role_and_db
  write_pg_env
  echo "Pre-align complete (or skipped if PostgreSQL not yet running)"

  step "prepare BenchBase checkout (pinned commit ${BENCHBASE_COMMIT:0:12})"
  prepare_benchbase_checkout

  step "wrap mvnw so it always runs from .benchbase"
  mirror_benchbase_build_files

  step "install PostgreSQL 14, aarch64 Java 23 and pinned BenchBase"
  export PGHOST="$PG_HOST_ADDR"
  export PGPORT="$PG_PORT_NUM"
  export PGUSER="$PG_ROLE"
  export PGDATABASE="$PG_DATABASE"
  export PGPASSWORD="$BENCH_DB_PASSWORD"
  export POSTGRES_BENCH_PASSWORD="$BENCH_DB_PASSWORD"
  "$project_dir/scripts/install_postgres_benchbase.sh"

  step "recreate PostgreSQL role and database"
  recreate_pg_role_and_db
  write_pg_env
  echo "PostgreSQL role ${PG_ROLE} recreated and authenticated successfully"

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

step "run unit and contract tests"
run_pytest_isolated "$python_bin"

step "load project .env for runtime verification"
set -a
# shellcheck disable=SC1091
source "$project_dir/.env"
set +a

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
BenchBase:    ${BENCHBASE_COMMIT}
Snapshots:    $(find artifacts/snapshots -maxdepth 1 -name '*.dump' -type f -size +0c 2>/dev/null | wc -l)/3
DeepSeek key: ${deepseek_state}
Mirror:       ${mirror}
EOF