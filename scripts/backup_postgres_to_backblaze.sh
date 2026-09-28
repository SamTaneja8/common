#!/usr/bin/env bash
# Incremental backups of PostgreSQL tables to Backblaze B2, for a repo whose
# Postgres runs in a container on this host (dealvant on vps2). MySQL backups
# on vps1 stay with setup_mysql's engine; this is the Postgres side only, so
# vps2 needs no setup_mysql checkout. Files, names and per-table state match
# setup_mysql's engine, so one restore procedure covers both.
#
# The calling repo's jobs/run_backup.sh declares WHAT (from its own env file):
#   BACKUP_REPO_NAME                 folder in B2, e.g. dealvant
#   REPO_POSTGRES_BACKUP_TABLE_SPECS database.schema.table:column:datetime|numeric[:retention_days],...
#   BACKUP_POSTGRES_CONTAINER        the running Postgres container
#   BACKUP_RETENTION                 days to keep in B2; 0 (default) keeps every increment
# WHERE comes from common/.env.host (host-only; containers never load it):
#   BACKBLAZE_B2_ACCOUNT_ID, BACKBLAZE_B2_APPLICATION_KEY, BACKBLAZE_B2_BUCKET,
#   BACKBLAZE_B2_REMOTE_NAME (default dealb2), BACKBLAZE_B2_ROOT_PATH (e.g. vps2)
#
# Each run dumps, per table, the rows whose watermark column is above the last
# run's maximum, as CSV with a header, uploads them, and only then records the
# new maximum. psql runs inside the container as its own POSTGRES_USER, so no
# database password is needed. It never starts or changes a container.
#
# Usage: backup_postgres_to_backblaze.sh           run the backup
#        backup_postgres_to_backblaze.sh --check   settings, container and B2 only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=load_env.sh
source "${SCRIPT_DIR}/load_env.sh"
load_env_file "${COMMON_DIR}/.env.host"

log() { printf '[%s] %-5s %s\n' "$(date -u +'%Y-%m-%d %H:%M:%S')" "$1" "$2"; }
die() { log ERROR "$1"; exit 1; }

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}"
}

for cmd in docker gzip rclone; do
  command -v "${cmd}" >/dev/null 2>&1 || die "${cmd} is not installed."
done
for var in BACKUP_REPO_NAME REPO_POSTGRES_BACKUP_TABLE_SPECS BACKUP_POSTGRES_CONTAINER \
           BACKBLAZE_B2_ACCOUNT_ID BACKBLAZE_B2_APPLICATION_KEY BACKBLAZE_B2_BUCKET; do
  [[ -n "${!var:-}" ]] || die "${var} is not set (repo settings come from its run_backup.sh, B2 settings from ${COMMON_DIR}/.env.host)."
done

REPO_NAME="$(trim "${BACKUP_REPO_NAME}")"
CONTAINER="$(trim "${BACKUP_POSTGRES_CONTAINER}")"
DEFAULT_RETENTION="$(trim "${BACKUP_RETENTION:-0}")"
# Local copies are only a staging area (B2 keeps every increment); pruned
# after this many days so the disk doesn't fill up.
LOCAL_KEEP_DAYS="$(trim "${BACKUP_LOCAL_KEEP_DAYS:-14}")"
RUN_DATE="$(date -u +'%y%m%d')"
RUN_TIMESTAMP="$(date -u +'%y%m%d-%H%M%S')"
LOCAL_ROOT="${COMMON_DIR}/backups/postgres/${REPO_NAME}/files"
STATE_ROOT="${COMMON_DIR}/backups/postgres/${REPO_NAME}/state"

# rclone reads its B2 remote from the environment; no rclone.conf needed.
REMOTE_NAME="${BACKBLAZE_B2_REMOTE_NAME:-dealb2}"
remote_key="$(printf '%s' "${REMOTE_NAME}" | tr '[:lower:]-.' '[:upper:]__')"
remote_key="${remote_key//[^A-Z0-9_]/_}"
export "RCLONE_CONFIG_${remote_key}_TYPE=b2"
export "RCLONE_CONFIG_${remote_key}_ACCOUNT=${BACKBLAZE_B2_ACCOUNT_ID}"
export "RCLONE_CONFIG_${remote_key}_KEY=${BACKBLAZE_B2_APPLICATION_KEY}"
root_path="${BACKBLAZE_B2_ROOT_PATH:-}"
root_path="${root_path#/}"
root_path="${root_path%/}"
REMOTE_PREFIX="${REMOTE_NAME}:${BACKBLAZE_B2_BUCKET}${root_path:+/${root_path}}"
REMOTE_ROOT="${REMOTE_PREFIX}/${REPO_NAME}"

# psql inside the container, as the container's own POSTGRES_USER.
pg() {
  local database="$1"
  shift
  docker exec -i "${CONTAINER}" sh -c 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$0" "$@"' "${database}" "$@"
}

pg_scalar() { trim "$(pg "$1" -tA -c "$2")"; }

quote_ident() { local v="${1//\"/\"\"}"; printf '"%s"' "${v}"; }
quote_literal() { local v="${1//\'/\'\'}"; printf "'%s'" "${v}"; }

where_clause() {
  local column prev="$2" next="$3" type="$4"
  column="$(quote_ident "$1")"
  if [[ "${type}" == numeric ]]; then
    [[ -n "${prev}" ]] && printf '%s > %s AND %s <= %s' "${column}" "${prev}" "${column}" "${next}" \
      || printf '%s <= %s' "${column}" "${next}"
  else
    [[ -n "${prev}" ]] && printf '%s > %s AND %s <= %s' "${column}" "$(quote_literal "${prev}")" "${column}" "$(quote_literal "${next}")" \
      || printf '%s <= %s' "${column}" "$(quote_literal "${next}")"
  fi
}

container_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || true)" == true ]]
}

backup_table() {
  local spec="$1" ref column type retention database schema table
  IFS=':' read -r ref column type retention <<< "${spec}"
  ref="$(trim "${ref}")"
  column="$(trim "${column:-}")"
  type="$(trim "${type:-numeric}")"
  retention="$(trim "${retention:-${DEFAULT_RETENTION}}")"
  IFS='.' read -r database schema table <<< "${ref}"
  [[ -n "${database}" && -n "${schema:-}" && -n "${table:-}" && -n "${column}" ]] \
    || die "Invalid spec (want database.schema.table:column:type): ${spec}"

  local name="${database}.${schema}.${table}"
  local state_file="${STATE_ROOT}/postgres-$(printf '%s' "${name}.${column}" | tr './:' '___').state"
  local prev="" next count
  [[ -f "${state_file}" ]] && prev="$(<"${state_file}")"

  next="$(pg_scalar "${database}" "SELECT MAX($(quote_ident "${column}")) FROM $(quote_ident "${schema}").$(quote_ident "${table}");")"
  if [[ -z "${next}" ]]; then
    log INFO "Skipping ${name}: no watermark values yet."
    return 0
  fi
  if [[ "${prev}" == "${next}" ]]; then
    log INFO "Skipping ${name}: watermark unchanged at ${next}."
    return 0
  fi

  local where
  where="$(where_clause "${column}" "${prev}" "${next}" "${type}")"
  count="$(pg_scalar "${database}" "SELECT COUNT(*) FROM $(quote_ident "${schema}").$(quote_ident "${table}") WHERE ${where};")"
  if [[ -z "${count}" || "${count}" == 0 ]]; then
    printf '%s\n' "${next}" > "${state_file}"
    log INFO "No new rows in ${name}; watermark advanced to ${next}."
    return 0
  fi

  local run_dir="${LOCAL_ROOT}/${table}/${RUN_DATE}"
  local file="${run_dir}/postgres-${database}-${schema}-${table}-${RUN_TIMESTAMP}.csv.gz"
  mkdir -p "${run_dir}"
  log INFO "Backing up ${count} row(s) from ${name}."
  pg "${database}" -c "\\copy (SELECT * FROM $(quote_ident "${schema}").$(quote_ident "${table}") WHERE ${where}) TO STDOUT WITH (FORMAT csv, HEADER true)" \
    | gzip > "${file}"

  # A CSV dump with a header line; `|| true` because head closing the pipe
  # early must not count as a failure under pipefail.
  gzip -t "${file}" 2>/dev/null || die "Dump failed gzip validation: ${file}"
  [[ -n "$(gzip -dc "${file}" 2>/dev/null | head -n 1 || true)" ]] || die "Dump has no header line: ${file}"

  rclone copy "${file}" "${REMOTE_ROOT}/${table}/${RUN_DATE}"
  # Only after the upload: a failed run is simply repeated next time.
  printf '%s\n' "${next}" > "${state_file}"

  if [[ "${retention}" != 0 ]]; then
    rclone delete "${REMOTE_ROOT}/${table}" --min-age "${retention}d"
    rclone rmdirs "${REMOTE_ROOT}/${table}" --leave-root
  fi
}

container_running || die "Container ${CONTAINER} isn't running; not starting it."

IFS=',' read -r -a specs <<< "${REPO_POSTGRES_BACKUP_TABLE_SPECS}"
first_database="$(trim "${specs[0]%%.*}")"
[[ "$(pg_scalar "${first_database}" 'SELECT 1;' 2>/dev/null || true)" == 1 ]] \
  || die "Can't query ${first_database} in ${CONTAINER} as its POSTGRES_USER."

if [[ "${1:-}" == --check ]]; then
  rclone lsf "${REMOTE_PREFIX}/" >/dev/null || die "Can't list ${REMOTE_PREFIX}/ (check the B2 settings in .env.host)."
  log INFO "Check OK: ${#specs[@]} table spec(s), container ${CONTAINER}, database ${first_database}, B2 ${REMOTE_ROOT}/"
  exit 0
fi

mkdir -p "${LOCAL_ROOT}" "${STATE_ROOT}"
for raw in "${specs[@]}"; do
  spec="$(trim "${raw}")"
  [[ -n "${spec}" ]] && backup_table "${spec}"
done

find "${LOCAL_ROOT}" -type f -name '*.csv.gz' -mtime +"${LOCAL_KEEP_DAYS}" -delete 2>/dev/null || true
log INFO "PostgreSQL backup completed for ${REPO_NAME}."
