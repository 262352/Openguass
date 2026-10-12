#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
benchbase_commit=33c00473807ebd49304d114a6d769d2d2b2bbb34
jdk_dir="${HOME}/.local/jdks/jdk-23.0.2+7"
mirror="${MIRROR:-official}"
apt_options=(-o Acquire::Retries=5)
maven_options=()
temporary_files=()

cleanup() {
  if ((${#temporary_files[@]})); then
    rm -f "${temporary_files[@]}"
  fi
}
trap cleanup EXIT

[[ $(id -u) -eq 0 ]] || { echo "Run this installer as root." >&2; exit 1; }
[[ -f "${project_dir}/.env" ]] || { echo "Missing ${project_dir}/.env" >&2; exit 1; }

architecture=$(uname -m)
case "$architecture" in
  aarch64|arm64) adoptium_arch=aarch64 ;;
  *) echo "This deployment script supports aarch64 only; found ${architecture}." >&2; exit 1 ;;
esac

case "$mirror" in
  official) ;;
  tsinghua|aliyun)
    . /etc/os-release
    codename=${VERSION_CODENAME:?Unable to determine Ubuntu codename}
    apt_sources=$(mktemp); temporary_files+=("$apt_sources")
    if [[ $mirror == tsinghua ]]; then
      ubuntu_base=https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports
    else
      ubuntu_base=https://mirrors.aliyun.com/ubuntu-ports
    fi
    maven_url=https://maven.aliyun.com/repository/public
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
    maven_settings=$(mktemp); temporary_files+=("$maven_settings")
    cat >"$maven_settings" <<EOF
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors><mirror><id>andromeda-$mirror</id><name>Andromeda mirror</name>
  <url>$maven_url</url><mirrorOf>*</mirrorOf></mirror></mirrors>
</settings>
EOF
    maven_options=(-s "$maven_settings")
    ;;
  *) echo "MIRROR must be tsinghua, aliyun, or official." >&2; exit 2 ;;
esac

echo "Using package mirror: $mirror"
apt-get "${apt_options[@]}" update
for attempt in 1 2 3; do
  if DEBIAN_FRONTEND=noninteractive apt-get "${apt_options[@]}" install -y \
      postgresql-14 postgresql-contrib-14 curl git ca-certificates python3 python3-venv; then
    break
  fi
  (( attempt < 3 )) || { echo "apt installation failed after 3 attempts." >&2; exit 1; }
  apt-get "${apt_options[@]}" update
done

pg_ctlcluster 14 main start || true
pg_isready -h /var/run/postgresql -p 5432

set -a
# shellcheck disable=SC1091
source "${project_dir}/.env"
set +a
bench_user=${PGUSER:-andromeda_bench}
bench_host=${PGHOST:-127.0.0.1}
bench_port=${PGPORT:-5432}
[[ $bench_user =~ ^[a-z_][a-z0-9_]*$ ]] || { echo "Unsafe PGUSER: $bench_user" >&2; exit 1; }

role_exists=$(runuser -u postgres -- sh -c 'cd / && exec psql -XAtqc "$1"' sh \
  "SELECT 1 FROM pg_roles WHERE rolname='${bench_user}'")
bench_password=${POSTGRES_BENCH_PASSWORD:-}

if [[ -n $role_exists && -z $bench_password ]]; then
  if [[ -t 0 ]]; then
    read -rsp "Existing PostgreSQL password for ${bench_user}: " bench_password
    echo
  else
    echo "${bench_user} already exists, but POSTGRES_BENCH_PASSWORD is empty." >&2
    echo "Put its current password in .env; the installer will not reset it." >&2
    exit 1
  fi
fi
if [[ -z $role_exists && -z $bench_password ]]; then
  bench_password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
fi
: "${bench_password:?POSTGRES_BENCH_PASSWORD is required}"

if [[ -z ${POSTGRES_BENCH_PASSWORD:-} ]]; then
  ENV_FILE="${project_dir}/.env" ENV_VALUE="$bench_password" python3 - <<'PY'
from pathlib import Path
import os

path = Path(os.environ["ENV_FILE"])
value = os.environ["ENV_VALUE"]
lines = path.read_text(encoding="utf-8").splitlines()
updated, found = [], False
for line in lines:
    if line.startswith("POSTGRES_BENCH_PASSWORD="):
        updated.append("POSTGRES_BENCH_PASSWORD=" + value)
        found = True
    else:
        updated.append(line)
if not found:
    updated.append("POSTGRES_BENCH_PASSWORD=" + value)
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
fi

if [[ -z $role_exists ]]; then
  runuser -u postgres -- sh -c 'cd / && exec psql -v ON_ERROR_STOP=1 -v role_name="$1" -v role_password="$2"' sh \
    "$bench_user" "$bench_password" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role_name', :'role_password') \gexec
SQL
else
  if ! PGPASSWORD="$bench_password" psql -XAt -h "$bench_host" -p "$bench_port" \
      -U "$bench_user" -d postgres -v ON_ERROR_STOP=1 -c 'select 1' >/dev/null; then
    echo "The password in .env does not match existing role ${bench_user}." >&2
    echo "No password was changed. Restore the existing password in .env and rerun." >&2
    exit 1
  fi
fi

for database in tpcc twitter ycsb; do
  if ! runuser -u postgres -- sh -c 'cd / && exec psql -XAtqc "$1"' sh \
      "SELECT 1 FROM pg_database WHERE datname='${database}'" | grep -q 1; then
    runuser -u postgres -- sh -c 'cd / && exec createdb --owner="$1" "$2"' sh "$bench_user" "$database"
  fi
done

if [[ ! -x "${jdk_dir}/bin/java" ]]; then
  mkdir -p "$(dirname "${jdk_dir}")"
  archive=$(mktemp); temporary_files+=("$archive")
  curl -fL --retry 3 -o "$archive" \
    "https://api.adoptium.net/v3/binary/version/jdk-23.0.2%2B7/linux/${adoptium_arch}/jdk/hotspot/normal/eclipse"
  tar -xzf "$archive" -C "$(dirname "${jdk_dir}")"
fi

if [[ ! -d "${project_dir}/.benchbase/.git" ]]; then
  rm -rf "${project_dir}/.benchbase"
  git init "${project_dir}/.benchbase"
  git -C "${project_dir}/.benchbase" fetch --depth 1 https://github.com/cmu-db/benchbase.git "$benchbase_commit"
  git -C "${project_dir}/.benchbase" checkout --detach FETCH_HEAD
fi
[[ $(git -C "${project_dir}/.benchbase" rev-parse HEAD) == "$benchbase_commit" ]] || {
  echo "BenchBase checkout differs from pinned commit ${benchbase_commit}." >&2; exit 1;
}
JAVA_HOME="$jdk_dir" PATH="$jdk_dir/bin:$PATH" \
  "${project_dir}/.benchbase/mvnw" "${maven_options[@]}" -q clean package -P postgres -DskipTests
rm -rf "${project_dir}/.benchbase/target/benchbase-postgres"
tar -xzf "${project_dir}/.benchbase/target/benchbase-postgres.tgz" -C "${project_dir}/.benchbase/target"
test -s "${project_dir}/.benchbase/target/benchbase-postgres/benchbase.jar"
echo "PostgreSQL and BenchBase are ready; existing credentials were preserved."
