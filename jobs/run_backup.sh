#!/usr/bin/env bash
# telemetry_db backup, moved here from the retired dashboard repo on
# 2026-10-01. BACKUP_REPO_NAME stays "dashboard" so the backup engine keeps
# its per-table positions and B2 folder (vps1/dashboard/): no second full
# dump, and restores find every increment in one place.
# Runs this repo's backup through the shared engine in setup_mysql
# (scripts/backup_repo_targets_to_backblaze.sh). This repo declares WHAT to
# back up in .env.host (telemetry_db: the end-to-end metering store; host-only,
# so containers never see these settings); setup_mysql owns HOW
# (docker exec dumps, rclone upload, Backblaze credentials); WHEN is the `dashboard-backup` step in common/orchestration/pipeline.yaml.
# DATASETUP_HOME overrides where setup_mysql is checked out.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/.env.host"

# .env is a Docker Compose env file, not a shell script: values can contain
# spaces, ; ( ) | and Compose also accepts "KEY: value". So never `source` it
# -- read only the backup keys, in any form Compose accepts (KEY=value,
# KEY: value, export KEY=value, quoted values, CRLF line endings).
env_value() {
  [[ -f "${ENV_FILE}" ]] || return 0
  awk -v key="$1" -v q="'" '
    { sub(/\r$/, "") }
    /^[[:space:]]*(#|$)/ { next }
    {
      line = $0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      if (match(line, /^[A-Za-z_][A-Za-z0-9_]*[[:space:]]*[=:]/)) {
        k = substr(line, 1, RLENGTH - 1); sub(/[[:space:]]+$/, "", k)
        if (k == key) {
          v = substr(line, RLENGTH + 1)
          sub(/^[[:space:]]+/, "", v); sub(/[[:space:]]+$/, "", v)
          f = substr(v, 1, 1)
          if (length(v) >= 2 && (f == "\"" || f == q) && substr(v, length(v), 1) == f) v = substr(v, 2, length(v) - 2)
          val = v; found = 1
        }
      }
    }
    END { if (found) print val }' "${ENV_FILE}"
}

# A value already in the environment (e.g. set by the caller) wins over .env.
for key in DATASETUP_HOME BACKUP_REPO_NAME BACKUP_RETENTION BACKUP_DATABASES_OF_INTEREST \
           REPO_MYSQL_BACKUP_TABLE_SPECS BACKUP_INCREMENTAL_TABLES \
           POSTGRES_BACKUP_DATABASES_OF_INTEREST REPO_POSTGRES_BACKUP_TABLE_SPECS \
           POSTGRES_BACKUP_INCREMENTAL_TABLES POSTGRES_BACKUP_RETENTION_BY_DATABASE; do
  if [[ -z "${!key:-}" ]]; then
    value="$(env_value "${key}")"
    [[ -n "${value}" ]] && export "${key}=${value}"
  fi
done

export BACKUP_REPO_NAME="${BACKUP_REPO_NAME:-dashboard}"
export BACKUP_RETENTION="${BACKUP_RETENTION:-0}"
export REPO_MYSQL_BACKUP_TABLE_SPECS="${REPO_MYSQL_BACKUP_TABLE_SPECS:-}"
export REPO_POSTGRES_BACKUP_TABLE_SPECS="${REPO_POSTGRES_BACKUP_TABLE_SPECS:-}"
export BACKUP_DATABASES_OF_INTEREST="${BACKUP_DATABASES_OF_INTEREST:-}"
export BACKUP_INCREMENTAL_TABLES="${BACKUP_INCREMENTAL_TABLES:-}"
export POSTGRES_BACKUP_DATABASES_OF_INTEREST="${POSTGRES_BACKUP_DATABASES_OF_INTEREST:-}"
export POSTGRES_BACKUP_INCREMENTAL_TABLES="${POSTGRES_BACKUP_INCREMENTAL_TABLES:-}"
export POSTGRES_BACKUP_RETENTION_BY_DATABASE="${POSTGRES_BACKUP_RETENTION_BY_DATABASE:-}"

# Run through bash: the engine is committed without the execute bit in setup_mysql.
exec bash "${DATASETUP_HOME:-/home/botuser/setup_mysql}/scripts/backup_repo_targets_to_backblaze.sh"
