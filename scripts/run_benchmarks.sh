#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python_bin=${PYTHON:-python3}
[[ -x "${project_dir}/.venv/bin/python" ]] && python_bin="${project_dir}/.venv/bin/python"
mode=${1:-run}
duration=${2:-30}
terminals=${3:-2}
cd "${project_dir}"
case "${mode}" in
  prepare)
    exec "${python_bin}" -m app.benchmark.cli all --duration "${duration}" --terminals "${terminals}"
    ;;
  run)
    exec "${python_bin}" -m app.benchmark.cli all --duration "${duration}" --terminals "${terminals}" --reuse-data
    ;;
  *)
    echo "Usage: $0 {prepare|run} [duration_seconds] [terminals]" >&2
    exit 2
    ;;
esac
