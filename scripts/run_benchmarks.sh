#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mode=${1:-run}
duration=${2:-30}
terminals=${3:-2}
cd "${project_dir}"
case "${mode}" in
  prepare)
    exec python3 -m app.benchmark.cli all --duration "${duration}" --terminals "${terminals}"
    ;;
  run)
    exec python3 -m app.benchmark.cli all --duration "${duration}" --terminals "${terminals}" --reuse-data
    ;;
  *)
    echo "Usage: $0 {prepare|run} [duration_seconds] [terminals]" >&2
    exit 2
    ;;
esac
