#!/usr/bin/env bash
set -euo pipefail

# Daily API provider authorization/balance check (OpenAI, HuggingFace,
# DeepSeek, Gemini). Logs results and writes each to the api_balance_check
# table in telemetry_db -- see common/observability/sql/api_balance_check.sql.
# Ported from the retired vpsmonitor repo.
# Usage: check_api_balances.sh (no args; invoked by common/orchestration/pipeline.yaml)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/run_job_common.sh" \
  "common" \
  "API Balance Check" \
  "${PYTHON:-python3}" -m common_utils.api_balance_check
