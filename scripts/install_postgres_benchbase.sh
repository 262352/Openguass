#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
benchbase_commit=33c00473807ebd49304d114a6d769d2d2b2bbb34
jdk_dir="${HOME}/.local/jdks/jdk-23.0.2+7"

if [[ $(id -u) -ne 0 ]]; then
  echo "Run this installer as root (or with sudo)." >&2
  exit 1
fi
if [[ ! -f "${project_dir}/.env" ]]; then
  echo "Create ${project_dir}/.env from .env.example first." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source "${project_dir}/.env"
set +a
: "${POSTGRES_BENCH_PASSWORD:?POSTGRES_BENCH_PASSWORD must be set in .env}"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql-14 postgresql-contrib-14 curl git ca-certificates
pg_ctlcluster 14 main start || true
pg_isready

runuser -u postgres -- psql -v ON_ERROR_STOP=1 -v bench_password="${POSTGRES_BENCH_PASSWORD}" <<'SQL'
SELECT format('CREATE ROLE andromeda_bench LOGIN PASSWORD %L', :'bench_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'andromeda_bench') \gexec
SELECT format('ALTER ROLE andromeda_bench PASSWORD %L', :'bench_password') \gexec
SQL
for database in tpcc twitter ycsb; do
  if ! runuser -u postgres -- psql -Atqc "SELECT 1 FROM pg_database WHERE datname='${database}'" | grep -q 1; then
    runuser -u postgres -- createdb --owner=andromeda_bench "${database}"
  fi
done

if [[ ! -x "${jdk_dir}/bin/java" ]]; then
  mkdir -p "$(dirname "${jdk_dir}")"
  archive=$(mktemp)
  curl -fL --retry 3 -o "${archive}" https://api.adoptium.net/v3/binary/version/jdk-23.0.2%2B7/linux/x64/jdk/hotspot/normal/eclipse
  tar -xzf "${archive}" -C "$(dirname "${jdk_dir}")"
  rm -f "${archive}"
fi

if [[ ! -d "${project_dir}/.benchbase/.git" ]]; then
  git init "${project_dir}/.benchbase"
  git -C "${project_dir}/.benchbase" fetch --depth 1 https://github.com/cmu-db/benchbase.git "${benchbase_commit}"
  git -C "${project_dir}/.benchbase" checkout --detach FETCH_HEAD
fi
if [[ $(git -C "${project_dir}/.benchbase" rev-parse HEAD) != "${benchbase_commit}" ]]; then
  echo "BenchBase checkout differs from pinned commit ${benchbase_commit}." >&2
  exit 1
fi
JAVA_HOME="${jdk_dir}" PATH="${jdk_dir}/bin:${PATH}" \
  "${project_dir}/.benchbase/mvnw" -q clean package -P postgres -DskipTests
rm -rf "${project_dir}/.benchbase/target/benchbase-postgres"
tar -xzf "${project_dir}/.benchbase/target/benchbase-postgres.tgz" -C "${project_dir}/.benchbase/target"
echo "PostgreSQL and BenchBase are ready."
