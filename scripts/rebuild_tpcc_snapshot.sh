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
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
[[ -x "$PYTHON_BIN" ]] || { echo "Missing $PYTHON_BIN" >&2; exit 1; }
[[ -s .benchbase/target/benchbase-postgres/benchbase.jar ]] || { echo "BenchBase is missing" >&2; exit 1; }

expected=$("$PYTHON_BIN" - <<'PY'
from pathlib import Path
from xml.etree import ElementTree

value = float(ElementTree.parse(Path("benchmarks/postgres/tpcc.xml")).findtext("scalefactor", ""))
if value <= 0 or not value.is_integer():
    raise SystemExit(f"TPC-C scalefactor must be a positive integer, got {value!r}")
print(int(value))
PY
)

echo "[tpcc-prepare] loading scale-factor $expected without benchmark execution"
"$PYTHON_BIN" - <<'PY'
from app.benchmark.benchbase import BenchBaseRunner
print(BenchBaseRunner().load_only("tpcc", terminals=1, random_seed=20261015))
PY

actual=$(PGPASSWORD="${PGPASSWORD:-${POSTGRES_BENCH_PASSWORD:-}}" psql -XAt \
  -h "${PGHOST:-127.0.0.1}" -p "${PGPORT:-5432}" \
  -U "${PGUSER:-andromeda_bench}" -d tpcc -c "select count(*) from warehouse")
[[ "$actual" == "$expected" ]] || { echo "Expected $expected warehouses after load, found $actual" >&2; exit 1; }

echo "[tpcc-prepare] creating validated immutable snapshot"
mkdir -p artifacts/snapshots
rm -f artifacts/snapshots/tpcc.dump artifacts/snapshots/tpcc.dump.tmp
"$PYTHON_BIN" -m app.lab.cli workload prepare tpcc --reload
test -s artifacts/snapshots/tpcc.dump
pg_restore --list artifacts/snapshots/tpcc.dump >/dev/null

profile=artifacts/snapshots/workload_profile.sha256
temporary="${profile}.tmp"
if [[ -f "$profile" ]]; then
  awk '$2 != "benchmarks/postgres/tpcc.xml"' "$profile" > "$temporary"
else
  : > "$temporary"
fi
sha256sum benchmarks/postgres/tpcc.xml >> "$temporary"
mv "$temporary" "$profile"
echo "[tpcc-prepare] complete: warehouses=$actual snapshot=artifacts/snapshots/tpcc.dump"
