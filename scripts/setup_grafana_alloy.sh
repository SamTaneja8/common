#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
HOST_LABEL="${COMMON_HOST_LABEL:-}"
CONFIG_SOURCE="${PROJECT_DIR}/observability/alloy/config.alloy"
CONFIG_PATH="/etc/alloy/config.alloy"
DEFAULTS_PATH="/etc/default/alloy"
INSTALL_ALLOY=false
RESTART_ALLOY=true
DRY_RUN=false
ARCH="${ARCH:-}"

usage() {
  cat >&2 <<'EOF'
Usage:
  setup_grafana_alloy.sh [options]

Options:
  --env-file <path>       Env file containing Grafana GCLOUD_* values.
                          Default: <common repo>/.env
  --host-label <label>    Host label for Grafana dashboards, e.g. vps1 or vps2.
                          Default: COMMON_HOST_LABEL from env/env-file
  --config-source <path>  Alloy config to install.
                          Default: common/observability/alloy/config.alloy
  --config-path <path>    Destination Alloy config path.
                          Default: /etc/alloy/config.alloy
  --defaults-path <path>  Destination Alloy environment file.
                          Default: /etc/default/alloy
  --install-alloy         Run Grafana Cloud's Linux binary installer first.
  --skip-restart          Do not restart Alloy after writing config.
  --dry-run               Print what would happen without writing files.
  -h, --help              Show this help.

Required env values:
  GCLOUD_HOSTED_METRICS_URL
  GCLOUD_HOSTED_METRICS_ID
  GCLOUD_SCRAPE_INTERVAL
  GCLOUD_HOSTED_LOGS_URL
  GCLOUD_HOSTED_LOGS_ID
  GCLOUD_RW_API_KEY

Do not commit a real GCLOUD_RW_API_KEY. Put it in common/.env on each VPS.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --host-label)
      HOST_LABEL="${2:-}"
      shift 2
      ;;
    --config-source)
      CONFIG_SOURCE="${2:-}"
      shift 2
      ;;
    --config-path)
      CONFIG_PATH="${2:-}"
      shift 2
      ;;
    --defaults-path)
      DEFAULTS_PATH="${2:-}"
      shift 2
      ;;
    --install-alloy)
      INSTALL_ALLOY=true
      shift
      ;;
    --skip-restart)
      RESTART_ALLOY=false
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

HOST_LABEL="${HOST_LABEL:-${COMMON_HOST_LABEL:-}}"
ARCH="${ARCH:-$(uname -m)}"
case "${ARCH}" in
  x86_64)
    ARCH="amd64"
    ;;
  aarch64|arm64)
    ARCH="arm64"
    ;;
esac

require_value() {
  local name="$1"
  local value="${!name:-}"

  if [[ -z "${value}" ]]; then
    echo "Missing required value: ${name}" >&2
    exit 1
  fi
}

require_file() {
  local path="$1"

  if [[ ! -f "${path}" ]]; then
    echo "Required file not found: ${path}" >&2
    exit 1
  fi
}

run_or_print() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    printf 'DRY RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

alloy_service_available() {
  command -v systemctl >/dev/null 2>&1 \
    && systemctl list-unit-files alloy.service --no-pager >/dev/null 2>&1
}

write_defaults() {
  local temp_file
  temp_file="$(mktemp)"

  cat > "${temp_file}" <<EOF
CONFIG_FILE="${CONFIG_PATH}"
CUSTOM_ARGS=""
COMMON_HOST_LABEL="${HOST_LABEL}"
GCLOUD_HOSTED_METRICS_URL="${GCLOUD_HOSTED_METRICS_URL}"
GCLOUD_HOSTED_METRICS_ID="${GCLOUD_HOSTED_METRICS_ID}"
GCLOUD_SCRAPE_INTERVAL="${GCLOUD_SCRAPE_INTERVAL}"
GCLOUD_HOSTED_LOGS_URL="${GCLOUD_HOSTED_LOGS_URL}"
GCLOUD_HOSTED_LOGS_ID="${GCLOUD_HOSTED_LOGS_ID}"
GCLOUD_RW_API_KEY="${GCLOUD_RW_API_KEY}"
EOF

  run_or_print sudo install -m 600 -o root -g root "${temp_file}" "${DEFAULTS_PATH}"
  rm -f "${temp_file}"
}

install_alloy_with_grafana_script() {
  local installer_url="https://storage.googleapis.com/cloud-onboarding/alloy/scripts/install-linux-binary.sh"

  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "DRY RUN: would run Grafana Cloud Alloy installer from ${installer_url}"
    return 0
  fi

  ARCH="${ARCH}" \
  GCLOUD_HOSTED_METRICS_URL="${GCLOUD_HOSTED_METRICS_URL}" \
  GCLOUD_HOSTED_METRICS_ID="${GCLOUD_HOSTED_METRICS_ID}" \
  GCLOUD_SCRAPE_INTERVAL="${GCLOUD_SCRAPE_INTERVAL}" \
  GCLOUD_HOSTED_LOGS_URL="${GCLOUD_HOSTED_LOGS_URL}" \
  GCLOUD_HOSTED_LOGS_ID="${GCLOUD_HOSTED_LOGS_ID}" \
  GCLOUD_RW_API_KEY="${GCLOUD_RW_API_KEY}" \
    /bin/sh -c "$(curl -fsSL "${installer_url}")"
}

require_file "${CONFIG_SOURCE}"
require_value HOST_LABEL
require_value GCLOUD_HOSTED_METRICS_URL
require_value GCLOUD_HOSTED_METRICS_ID
require_value GCLOUD_SCRAPE_INTERVAL
require_value GCLOUD_HOSTED_LOGS_URL
require_value GCLOUD_HOSTED_LOGS_ID
require_value GCLOUD_RW_API_KEY

if [[ "${INSTALL_ALLOY}" == "true" ]]; then
  install_alloy_with_grafana_script
fi

run_or_print sudo mkdir -p "$(dirname "${CONFIG_PATH}")"
run_or_print sudo install -m 644 -o root -g root "${CONFIG_SOURCE}" "${CONFIG_PATH}"
write_defaults

if [[ "${RESTART_ALLOY}" == "true" ]]; then
  if [[ "${DRY_RUN}" == "true" ]] || alloy_service_available; then
    run_or_print sudo systemctl enable alloy
    run_or_print sudo systemctl restart alloy
    run_or_print sudo systemctl status alloy --no-pager
  else
    echo "alloy.service was not found; start Alloy manually with:" >&2
    echo "  ./alloy-linux-${ARCH} run ${CONFIG_PATH}" >&2
  fi
fi

echo "Grafana Alloy setup complete for ${HOST_LABEL}."
