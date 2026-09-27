#!/usr/bin/env bash
# Shared helper that standardizes how scheduled jobs are launched for scraper-style
# repos: reports job start/failure to common_utils.metering_cli (exporting
# JOB_RUN_ID/JOB_TRIGGER_SOURCE/JOB_ARGUMENTS_JSON) and tees command output while
# preserving the wrapped command's exit code.
# Usage: run_job_common.sh <service> <job-name> <command...>
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: run_job_common.sh <service> <job-name> <command...>" >&2
  exit 1
fi

SERVICE_NAME="$1"
JOB_NAME="$2"
shift 2

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Host jobs use common/.venv (scripts/setup_host_python.sh: a current
# mysql-connector-python) when it exists, else python3. Ubuntu 24.04 has no
# `python`, and its python3-mysql.connector can't connect on Python 3.12.
if [[ -z "${PYTHON:-}" && -x "${COMMON_DIR}/.venv/bin/python" ]]; then
  PY="${COMMON_DIR}/.venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi
export PYTHON="${PY}"

# Host-only settings (e.g. TELEMETRY_MYSQL_HOST=127.0.0.1, port 53306) live in
# common/.env.host, which containers never load. Parsed, never `source`d; a
# value already in the environment wins.
HOST_ENV_FILE="${COMMON_DIR}/.env.host"
if [[ -f "${HOST_ENV_FILE}" ]]; then
  while IFS=$'\t' read -r key value; do
    [[ -n "${key}" && -z "${!key:-}" ]] && export "${key}=${value}"
  done < <(awk -v q="'" '
    { sub(/\r$/, "") }
    /^[[:space:]]*(#|$)/ { next }
    {
      line = $0; sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      if (match(line, /^[A-Za-z_][A-Za-z0-9_]*[[:space:]]*[=:]/)) {
        k = substr(line, 1, RLENGTH - 1); sub(/[[:space:]]+$/, "", k)
        v = substr(line, RLENGTH + 1); sub(/^[[:space:]]+/, "", v); sub(/[[:space:]]+$/, "", v)
        f = substr(v, 1, 1)
        if (length(v) >= 2 && (f == "\"" || f == q) && substr(v, length(v), 1) == f) v = substr(v, 2, length(v) - 2)
        print k "\t" v
      }
    }' "${HOST_ENV_FILE}")
fi

RUN_ID="${JOB_RUN_ID:-$("$PY" - <<'PY'
import uuid
print(uuid.uuid4())
PY
)}"

ARGUMENTS_JSON="$("$PY" - <<'PY' "$@"
import json
import sys
print(json.dumps({"script_args": sys.argv[1:]}))
PY
)"

OUTPUT_FILE="$(mktemp)"
trap 'rm -f "$OUTPUT_FILE"' EXIT

export JOB_RUN_ID="$RUN_ID"
export JOB_TRIGGER_SOURCE="${JOB_TRIGGER_SOURCE:-shell}"
export JOB_ARGUMENTS_JSON="$ARGUMENTS_JSON"

"$PY" -m common_utils.metering_cli start \
  --job-name "$JOB_NAME" \
  --service-name "$SERVICE_NAME" \
  --trigger-source "$JOB_TRIGGER_SOURCE" \
  --arguments-json "$ARGUMENTS_JSON" \
  --step-name "Shell Invocation" \
  --detail-message "Shell wrapper launched $*" >/dev/null || true

set +e
"$@" 2>&1 | tee "$OUTPUT_FILE"
EXIT_CODE=${PIPESTATUS[0]}
set -e

if [[ $EXIT_CODE -ne 0 ]]; then
  FAILURE_DETAIL="$("$PY" - <<'PY' "$OUTPUT_FILE"
from pathlib import Path
import sys

lines = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines()
print("\n".join(lines[-20:] if lines else ["No output captured."]))
PY
)"

  "$PY" -m common_utils.metering_cli shell-fail \
    --job-name "$JOB_NAME" \
    --service-name "$SERVICE_NAME" \
    --run-id "$RUN_ID" \
    --exit-code "$EXIT_CODE" \
    --detail-message "$FAILURE_DETAIL" >/dev/null || true
fi

exit "$EXIT_CODE"
