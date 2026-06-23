#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat >&2 <<'EOF'
Usage:
  run_cron_job.sh --name <job-name> --workdir <repo-dir> [options] -- <command...>

Options:
  --log-dir <dir>       Directory for durable cron logs.
                        Default: <workdir>/logs/cron_runs
  --lock-file <path>    Lock file used to prevent overlapping runs.
                        Default: /tmp/common-cron-<job-name>.lock
  --env-file <path>     Env file used only for LOG_SAVE_DAYS.
                        Default: <workdir>/.env
  --common-env-file <path>
                        Shared fallback env file for LOG_SAVE_DAYS.
                        Default: <common repo>/.env

The wrapper runs exactly one command now. It does not replay missed jobs.
EOF
}

JOB_NAME=""
WORKDIR=""
LOG_DIR=""
LOCK_FILE=""
ENV_FILE=""
COMMON_ENV_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      JOB_NAME="${2:-}"
      shift 2
      ;;
    --workdir)
      WORKDIR="${2:-}"
      shift 2
      ;;
    --log-dir)
      LOG_DIR="${2:-}"
      shift 2
      ;;
    --lock-file)
      LOCK_FILE="${2:-}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --common-env-file)
      COMMON_ENV_FILE="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "${JOB_NAME}" || -z "${WORKDIR}" || $# -eq 0 ]]; then
  usage
  exit 2
fi

if [[ ! -d "${WORKDIR}" ]]; then
  echo "Workdir does not exist: ${WORKDIR}" >&2
  exit 1
fi

LOG_DIR="${LOG_DIR:-${WORKDIR}/logs/cron_runs}"
LOCK_FILE="${LOCK_FILE:-/tmp/common-cron-${JOB_NAME}.lock}"
ENV_FILE="${ENV_FILE:-${WORKDIR}/.env}"
COMMON_ENV_FILE="${COMMON_ENV_FILE:-${COMMON_DIR}/.env}"

parse_log_save_days_value() {
  local raw_value="$1"
  local parsed_value

  parsed_value="$(
    printf '%s' "${raw_value}" \
      | sed -e 's/[[:space:]]*#.*$//' \
            -e 's/^[[:space:]]*//' \
            -e 's/[[:space:]]*$//' \
            -e 's/^"//' \
            -e 's/"$//' \
            -e "s/^'//" \
            -e "s/'$//"
  )"

  if [[ "${parsed_value}" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "${parsed_value}"
    return 0
  fi

  return 1
}

read_env_file_value() {
  local env_file="$1"

  [[ -f "${env_file}" ]] || return 0

  grep -E '^[[:space:]]*LOG_SAVE_DAYS[[:space:]]*=' "${env_file}" 2>/dev/null \
    | tail -n 1 \
    | cut -d '=' -f2- \
    || true
}

read_log_save_days() {
  local raw_value=""
  local parsed_value=""

  for raw_value in \
    "${LOG_SAVE_DAYS:-}" \
    "$(read_env_file_value "${ENV_FILE}")" \
    "$(read_env_file_value "${COMMON_ENV_FILE}")"; do
    if parsed_value="$(parse_log_save_days_value "${raw_value}")"; then
      printf '%s\n' "${parsed_value}"
      return 0
    fi
  done

  # Last-resort safety value if neither app nor common config is present.
  printf '7\n'
}

mkdir -p "${LOG_DIR}" "$(dirname "${LOCK_FILE}")"

LOG_SAVE_DAYS_RESOLVED="$(read_log_save_days)"
RUN_TS="$(date -u +"%Y%m%d-%H%M%S")"
LOG_FILE="${LOG_DIR}/${RUN_TS}-${JOB_NAME}.log"
EXIT_CODE=0

run_command() {
  echo "[$(date -u +"%Y-%m-%d %H:%M:%S")] START ${JOB_NAME}"
  echo "workdir=${WORKDIR}"
  echo "command=$*"
  echo "log_save_days=${LOG_SAVE_DAYS_RESOLVED}"

  cd "${WORKDIR}"
  set +e
  "$@"
  EXIT_CODE=$?
  set -e

  echo "[$(date -u +"%Y-%m-%d %H:%M:%S")] FINISH ${JOB_NAME} exit_code=${EXIT_CODE}"
  exit "${EXIT_CODE}"
}

if command -v flock >/dev/null 2>&1; then
  {
    flock -n 200 || {
      echo "[$(date -u +"%Y-%m-%d %H:%M:%S")] SKIP ${JOB_NAME}: another run is active."
      exit 75
    }
    run_command "$@"
  } 200>"${LOCK_FILE}" 2>&1 | tee "${LOG_FILE}"
else
  LOCK_DIR="${LOCK_FILE}.d"
  {
    if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
      echo "[$(date -u +"%Y-%m-%d %H:%M:%S")] SKIP ${JOB_NAME}: another run is active."
      exit 75
    fi
    trap 'rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT
    run_command "$@"
  } 2>&1 | tee "${LOG_FILE}"
fi
EXIT_CODE=${PIPESTATUS[0]}

find "${LOG_DIR}" -type f -name "*.log" -mtime +"${LOG_SAVE_DAYS_RESOLVED}" -delete 2>/dev/null || true

exit "${EXIT_CODE}"
