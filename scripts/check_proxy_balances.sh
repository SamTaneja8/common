#!/usr/bin/env bash
set -euo pipefail

# Daily proxy authorization/balance check (Evomi, FloppyData). Log-only --
# see common_utils/proxy_balance_check.py's docstring for why there's no
# database table. Ported from the retired vpsmonitor repo.
# Usage: check_proxy_balances.sh (no args; invoked by common/orchestration/pipeline.yaml)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/run_job_common.sh" \
  "common" \
  "Proxy Balance Check" \
  python -m common_utils.proxy_balance_check
