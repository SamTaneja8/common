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

RUN_ID="${JOB_RUN_ID:-$(python - <<'PY'
import uuid
print(uuid.uuid4())
PY
)}"

ARGUMENTS_JSON="$(python - <<'PY' "$@"
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

python -m common_utils.metering_cli start \
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
  FAILURE_DETAIL="$(python - <<'PY' "$OUTPUT_FILE"
from pathlib import Path
import sys

lines = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines()
print("\n".join(lines[-20:] if lines else ["No output captured."]))
PY
)"

  python -m common_utils.metering_cli shell-fail \
    --job-name "$JOB_NAME" \
    --service-name "$SERVICE_NAME" \
    --run-id "$RUN_ID" \
    --exit-code "$EXIT_CODE" \
    --detail-message "$FAILURE_DETAIL" >/dev/null || true
fi

exit "$EXIT_CODE"
