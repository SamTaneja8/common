#!/usr/bin/env bash
# Copies the files in one folder to Backblaze B2 without ever overwriting or
# deleting there: for stores of immutable files, such as dealvant's design
# release archives (reviewgate's jobs/run_release_backup.sh on VPS1, every
# release staged; dealvant's on VPS2, the ones shipped). Only top-level files
# are copied (subfolders such as build/ are not). Files named with --mutable
# (a log that grows) are replaced instead.
#
# B2 settings (BACKBLAZE_B2_*) come from RELEASE_BACKUP_ENV, else the first
# of common/.env.host (VPS2) and setup_mysql/.env (VPS1) that sets them. The
# destination is <bucket>/<BACKBLAZE_B2_ROOT_PATH>/<name>, so each host's copy
# stays separate.
#
# Usage: backup_dir_to_backblaze.sh --dir DIR --name NAME [--mutable FILE]... [--check]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPOS_ROOT="${REPOS_ROOT:-$(cd "${COMMON_DIR}/.." && pwd)}"
log() { printf '[%s] %-5s %s\n' "$(date -u +'%Y-%m-%d %H:%M:%S')" "$1" "$2"; }
die() { log ERROR "$1"; exit 1; }

DIR="" NAME="" CHECK=0 MUTABLE=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --mutable) MUTABLE+=("$2"); shift 2 ;;
    --check) CHECK=1; shift ;;
    *) die "unknown argument $1" ;;
  esac
done
[[ -n "${DIR}" && -n "${NAME}" ]] || die "usage: backup_dir_to_backblaze.sh --dir DIR --name NAME [--mutable FILE]... [--check]"
[[ "${NAME}" =~ ^[A-Za-z0-9._-]+$ ]] || die "--name: letters, digits, . _ - only"

# shellcheck source=load_env.sh
source "${SCRIPT_DIR}/load_env.sh"
ENV_USED=""
for candidate in "${RELEASE_BACKUP_ENV:-}" "${COMMON_DIR}/.env.host" "${REPOS_ROOT}/setup_mysql/.env"; do
  [[ -n "${candidate}" && -f "${candidate}" ]] || continue
  if grep -q '^[[:space:]]*\(export[[:space:]]\+\)\?BACKBLAZE_B2_BUCKET' "${candidate}"; then
    load_env_file "${candidate}"
    ENV_USED="${candidate}"
    break
  fi
done
[[ -n "${ENV_USED}" ]] || die "No BACKBLAZE_B2_* settings found (common/.env.host on VPS2, setup_mysql/.env on VPS1, or RELEASE_BACKUP_ENV)."
command -v rclone >/dev/null 2>&1 || die "rclone is not installed."
for var in BACKBLAZE_B2_ACCOUNT_ID BACKBLAZE_B2_APPLICATION_KEY BACKBLAZE_B2_BUCKET; do
  [[ -n "${!var:-}" ]] || die "${var} is not set in ${ENV_USED}."
done

# rclone reads its B2 remote from the environment; no rclone.conf needed
# (as backup_postgres_to_backblaze.sh does).
REMOTE_NAME="${BACKBLAZE_B2_REMOTE_NAME:-dealb2}"
remote_key="$(printf '%s' "${REMOTE_NAME}" | tr '[:lower:]-.' '[:upper:]__')"
remote_key="${remote_key//[^A-Z0-9_]/_}"
export "RCLONE_CONFIG_${remote_key}_TYPE=b2"
export "RCLONE_CONFIG_${remote_key}_ACCOUNT=${BACKBLAZE_B2_ACCOUNT_ID}"
export "RCLONE_CONFIG_${remote_key}_KEY=${BACKBLAZE_B2_APPLICATION_KEY}"
root_path="${BACKBLAZE_B2_ROOT_PATH:-}"
root_path="${root_path#/}"
root_path="${root_path%/}"
PREFIX="${REMOTE_NAME}:${BACKBLAZE_B2_BUCKET}${root_path:+/${root_path}}"
REMOTE="${PREFIX}/${NAME}"

if [[ ${CHECK} -eq 1 ]]; then
  rclone lsf -q "${PREFIX}/" >/dev/null || die "Can't list ${PREFIX}/ (check the B2 settings in ${ENV_USED})."
  log INFO "B2 reachable; ${DIR} goes to ${REMOTE} (settings from ${ENV_USED})"
  exit 0
fi

[[ -d "${DIR}" ]] || { log INFO "No folder at ${DIR} yet; nothing to back up."; exit 0; }
# Top-level files only, minus the mutable ones (first matching rule wins).
filters=()
for file in "${MUTABLE[@]+"${MUTABLE[@]}"}"; do filters+=(--filter "- /${file}"); done
filters+=(--filter "+ /*" --filter "- **")
rclone copy -q --immutable "${filters[@]}" "${DIR}" "${REMOTE}"
for file in "${MUTABLE[@]+"${MUTABLE[@]}"}"; do
  [[ -f "${DIR}/${file}" ]] && rclone copyto -q "${DIR}/${file}" "${REMOTE}/${file}"
done
count="$(find "${DIR}" -maxdepth 1 -type f | wc -l | tr -d ' ')"
log INFO "${count} file(s) in ${DIR} backed up to ${REMOTE}"
