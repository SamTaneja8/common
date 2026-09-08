#!/usr/bin/env bash

set -euo pipefail

# Short, standard-pattern cron entrypoint so every crontab line can look the
# same instead of repeating --name/--workdir/-- on every entry:
#   run_script.sh <repo>/<scripts-or-jobs>/<script.sh> [script args...]
# Derives --workdir (the repo root, two directories up from the script) and
# --name (<repo>-<script-basename>) from the script's own path, then
# delegates to run_cron_job.sh for the actual lock/log/retention handling.
#
# Usage examples (from crontab, paths relative to this repo's parent dir):
#   run_script.sh dealnews1/scripts/run_ingest_container.sh --no-deps
#   run_script.sh dealmoon1/jobs/run_full_cycle.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PARENT_DIR="$(cd "${COMMON_DIR}/.." && pwd)"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <repo>/<scripts-or-jobs>/<script.sh> [script-args...]" >&2
  exit 1
fi

TARGET_SCRIPT="$1"
shift

if [[ "${TARGET_SCRIPT}" != /* ]]; then
  TARGET_SCRIPT="${PARENT_DIR}/${TARGET_SCRIPT}"
fi

if [[ ! -f "${TARGET_SCRIPT}" ]]; then
  echo "Script not found: ${TARGET_SCRIPT}" >&2
  exit 1
fi

# …/<repo>/scripts/<script>.sh (or …/<repo>/jobs/<script>.sh) -> …/<repo>
SCRIPT_PARENT_DIR="$(cd "$(dirname "${TARGET_SCRIPT}")" && pwd)"
WORKDIR="$(cd "${SCRIPT_PARENT_DIR}/.." && pwd)"
REPO_NAME="$(basename "${WORKDIR}")"
SCRIPT_BASENAME="$(basename "${TARGET_SCRIPT}")"
JOB_NAME="${REPO_NAME}-${SCRIPT_BASENAME%.sh}"

exec "${SCRIPT_DIR}/run_cron_job.sh" --name "${JOB_NAME}" --workdir "${WORKDIR}" -- "${TARGET_SCRIPT}" "$@"
