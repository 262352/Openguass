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

ensure_postgresql_ready() {
  local host="${PGHOST:-127.0.0.1}"
  local port="${PGPORT:-5432}"
  local version="${PG_CLUSTER_VERSION:-14}"
  local cluster="${PG_CLUSTER_NAME:-main}"
  if pg_isready -h "$host" -p "$port" >/dev/null 2>&1; then
    return 0
  fi
  echo "[tpcc-only] PostgreSQL is not responding at ${host}:${port}; attempting ${version}/${cluster} startup"
  if [[ "$host" == "127.0.0.1" || "$host" == "localhost" ]] && command -v pg_ctlcluster >/dev/null; then
    pg_ctlcluster "$version" "$cluster" start >/dev/null 2>&1 || true
    for _ in {1..30}; do
      pg_isready -h "$host" -p "$port" >/dev/null 2>&1 && return 0
      sleep 1
    done
  fi
  echo "[tpcc-only] PostgreSQL preflight failed: no response at ${host}:${port}" >&2
  command -v pg_lsclusters >/dev/null && pg_lsclusters >&2 || true
  echo "Check: systemctl status postgresql --no-pager; ss -ltnp | grep postgres" >&2
  return 1
}

ensure_postgresql_ready

SUITE_ID="${SUITE_ID:-tpcc-only-$(date -u +%Y%m%dT%H%M%SZ)}"
METHOD_EXPERIMENT_ID="${METHOD_EXPERIMENT_ID:-${SUITE_ID}-diagnosis}"
mkdir -p artifacts
printf '%s\n' "$SUITE_ID" > artifacts/current_tpcc_only_suite_id.txt

# A stale snapshot from another scale factor cannot be compared with current SF=5
# workload. Rebuild only TPC-C, without executing a smoke benchmark.
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/snapshot_profile.sh"
if ! snapshot_workload_profile_matches tpcc "artifacts/snapshots/workload_profile.sha256"; then
  echo "[tpcc-only] TPC-C snapshot profile is absent or stale; rebuilding TPC-C only"
  bash scripts/rebuild_tpcc_snapshot.sh
fi

echo "[tpcc-only] stage 1/3: 100-trial Bayesian optimization over the shared catalog"
env \
  STAGE=bo \
  SUITE_ID="$SUITE_ID" \
  BO_WORKLOADS=tpcc \
  TPCC_BO_BUDGET="${TPCC_BO_BUDGET:-100}" \
  BO_INITIAL_POINTS="${BO_INITIAL_POINTS:-10}" \
  BO_RETEST_REPEATS="${BO_RETEST_REPEATS:-3}" \
  DURATION="${DURATION:-120}" \
  WARMUP_SECONDS="${WARMUP_SECONDS:-30}" \
  TPCC_TERMINALS="${TPCC_TERMINALS:-3}" \
  BENCHMARK_SEED="${BENCHMARK_SEED:-20261015}" \
  bash scripts/run_bo_reference_then_switch.sh

BO_REFERENCE="artifacts/workload_switch/$SUITE_ID/oracle/bo_references/tpcc/reference.json"
[[ -s "$BO_REFERENCE" ]] || { echo "Missing completed TPC-C BO reference: $BO_REFERENCE" >&2; exit 1; }

echo "[tpcc-only] stage 2/3: diagnosis and health coverage from the exact common baseline"
COMMON_BASELINE="artifacts/workload_switch/$SUITE_ID/oracle/runs/${SUITE_ID}-tpcc/tpcc/baseline.json"
[[ -s "$COMMON_BASELINE" ]] || {
  echo "Missing effective BO baseline used by TPC-C: $COMMON_BASELINE" >&2
  exit 1
}
env \
  EXPERIMENT_ID="$METHOD_EXPERIMENT_ID" \
  MAX_ROUNDS="${DIAGNOSIS_ROUNDS:-15}" \
  SEARCH_ROUNDS="${SEARCH_ROUNDS:-0}" \
  HEALTH_ROUNDS="${HEALTH_ROUNDS:-15}" \
  MAX_MEASUREMENTS="${DIAGNOSIS_MEASUREMENTS:-186}" \
  MAX_WALL_SECONDS="${MAX_WALL_SECONDS:-43200}" \
  MAX_MODEL_CALLS="${MAX_MODEL_CALLS:-45}" \
  DURATION="${DURATION:-120}" \
  WARMUP_SECONDS="${WARMUP_SECONDS:-30}" \
  TPCC_TERMINALS="${TPCC_TERMINALS:-3}" \
  BASELINE_REPEATS="${BASELINE_REPEATS:-3}" \
  BASELINE_STABILITY_RETRIES="${BASELINE_STABILITY_RETRIES:-1}" \
  BASELINE_CONFIG="$COMMON_BASELINE" \
  PAIRED_BLOCKS="${PAIRED_BLOCKS:-3}" \
  PAIRED_DELTA_MIN="${PAIRED_DELTA_MIN:-0.03}" \
  DRIFT_PERCENT="${DRIFT_PERCENT:-25}" \
  MIN_TPS_GAIN_PERCENT="${MIN_TPS_GAIN_PERCENT:-5}" \
  BENCHMARK_SEED="${BENCHMARK_SEED:-20261015}" \
  bash scripts/run_tpcc_continuous.sh

METHOD_DIR="artifacts/tpcc_continuous/$METHOD_EXPERIMENT_ID"
echo "[tpcc-only] stage 3/3: offline comparison"
BO_REFERENCE="$BO_REFERENCE" METHOD_DIR="$METHOD_DIR" SUITE_ID="$SUITE_ID" \
DIAGNOSIS_ROUNDS="${DIAGNOSIS_ROUNDS:-15}" SEARCH_ROUNDS="${SEARCH_ROUNDS:-0}" \
HEALTH_ROUNDS="${HEALTH_ROUNDS:-15}" \
  "$ROOT_DIR/.venv/bin/python" - <<'PY'
import json, os
from pathlib import Path

bo_path = Path(os.environ["BO_REFERENCE"])
method_dir = Path(os.environ["METHOD_DIR"])
bo = json.loads(bo_path.read_text(encoding="utf-8"))
report = json.loads((method_dir / "report.json").read_text(encoding="utf-8"))
champion_path = method_dir / "champion.json"
incumbent = json.loads(champion_path.read_text(encoding="utf-8")) if champion_path.exists() else json.loads(
    (method_dir / "incumbent.json").read_text(encoding="utf-8"))
initial = report.get("episode_initial_tps")
method_median = incumbent["verified_performance"].get(
    "median_throughput_rps", incumbent["verified_performance"].get("throughput_rps"))
bo_median = bo["metrics"]["throughput_rps_median"]
payload = {
    "schema_version": "andromeda.tpcc-only-comparison/v1",
    "suite_id": os.environ["SUITE_ID"],
    "measurement_protocol": bo["measurement_protocol"],
    "bo": {
        "search_trials": bo["search_budget"]["total"],
        "selected": bo["selection"],
        "retest_tps": bo["metrics"]["throughput_rps_values"],
        "retest_median_tps": bo_median,
        "reference": str(bo_path),
    },
    "diagnosis": {
        "configured_diagnosis_rounds": int(os.environ.get("DIAGNOSIS_ROUNDS", "15")),
        "configured_search_rounds": int(os.environ.get("SEARCH_ROUNDS", "15")),
        "configured_health_rounds": int(os.environ.get("HEALTH_ROUNDS", "15")),
        "completed_rounds": report.get("completed_rounds"),
        "completed_search_rounds": report.get("completed_search_rounds"),
        "initial_tps": initial,
        "final_incumbent_median_tps": method_median,
        "stop_reason": report.get("stop_reason"),
        "result_directory": str(method_dir),
    },
    "method_vs_bo_percent": ((method_median / bo_median) - 1) * 100 if bo_median else None,
}
(method_dir / "TPCC_BO_VS_DIAGNOSIS.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY

echo "[tpcc-only] complete"
echo "SUITE_ID=$SUITE_ID"
echo "BO_REFERENCE=$BO_REFERENCE"
echo "METHOD_DIR=$METHOD_DIR"
echo "COMPARISON=$METHOD_DIR/TPCC_BO_VS_DIAGNOSIS.json"
