#!/usr/bin/env bash
# Creates or updates common/.venv: the Python environment for common's
# host-side scripts (the cron orchestrator and the balance checks), which run
# on the host, not in Docker. Installs requirements-host.txt. Safe to re-run.
#
# Why not apt: Ubuntu 24.04's python3-mysql.connector is 8.0.15, which calls
# ssl.wrap_socket (removed in Python 3.12) and fails on every connection.
#
# Usage: ./scripts/setup_host_python.sh      (needs python3-venv)
set -euo pipefail

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${COMMON_DIR}/.venv"

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "python3-venv is missing: sudo apt install -y python3-venv" >&2
  exit 1
fi

[[ -x "${VENV}/bin/python" ]] || python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet --upgrade -r "${COMMON_DIR}/requirements-host.txt"

"${VENV}/bin/python" - <<'PY'
import mysql.connector, sys, yaml
print(f"common/.venv ready: Python {sys.version.split()[0]}, "
      f"mysql-connector-python {mysql.connector.__version__}, PyYAML {yaml.__version__}")
PY
