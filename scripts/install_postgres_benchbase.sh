#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
benchbase_commit=33c00473807ebd49304d114a6d769d2d2b2bbb34
jdk_dir="${HOME}/.local/jdks/jdk-23.0.2+7"
mirror="${MIRROR:-official}"
apt_options=(-o Acquire::Retries=5)
maven_options=()

case "$mirror" in
  official)
    ;;
  tsinghua|aliyun)
    . /etc/os-release
    codename=${VERSION_CODENAME:?Unable to determine Ubuntu codename}
    apt_sources=$(mktemp)
    if [[ $mirror == tsinghua ]]; then
      ubuntu_base=https://mirrors.tuna.tsinghua.edu.cn/ubuntu
      # TUNA currently provides Ubuntu/PyPI but no Maven Central mirror.
      maven_url=https://maven.aliyun.com/repository/public
    else
      ubuntu_base=https://mirrors.aliyun.com/ubuntu
      maven_url=https://maven.aliyun.com/repository/public
    fi
    cat >"$apt_sources" <<EOF
deb $ubuntu_base $codename main restricted universe multiverse
deb $ubuntu_base $codename-updates main restricted universe multiverse
deb $ubuntu_base $codename-backports main restricted universe multiverse
deb $ubuntu_base $codename-security main restricted universe multiverse
EOF
    apt_options+=(
      -o "Dir::Etc::sourcelist=$apt_sources"
      -o "Dir::Etc::sourceparts=-"
      -o "APT::Get::List-Cleanup=0"
    )
    maven_settings=$(mktemp)
    cat >"$maven_settings" <<EOF
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors>
    <mirror>
      <id>andromeda-$mirror</id>
      <name>Andromeda selected Maven mirror</name>
      <url>$maven_url</url>
      <mirrorOf>*</mirrorOf>
    </mirror>
  </mirrors>
</settings>
EOF
    maven_options=(-s "$maven_settings")
    ;;
  *)
    echo "MIRROR must be tsinghua, aliyun, or official." >&2
    exit 2
    ;;
esac

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

echo "Using package mirror: $mirror"
if [[ $mirror != official ]]; then
  echo "Ubuntu mirror: $ubuntu_base"
  echo "Maven mirror:  $maven_url"
fi
apt-get "${apt_options[@]}" update
for attempt in 1 2 3; do
  if DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" install -y \
      postgresql-14 postgresql-contrib-14 curl git ca-certificates python3 python3-venv; then
    break
  fi
  if [[ $attempt == 3 ]]; then
    echo "apt installation failed after ${attempt} attempts." >&2
    exit 1
  fi
  echo "apt download failed; retrying (${attempt}/3)..." >&2
  apt-get "${apt_options[@]}" update
done
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
  "${project_dir}/.benchbase/mvnw" "${maven_options[@]}" -q clean package -P postgres -DskipTests
rm -rf "${project_dir}/.benchbase/target/benchbase-postgres"
tar -xzf "${project_dir}/.benchbase/target/benchbase-postgres.tgz" -C "${project_dir}/.benchbase/target"
echo "PostgreSQL and BenchBase are ready."
