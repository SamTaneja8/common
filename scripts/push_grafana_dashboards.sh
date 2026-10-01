#!/usr/bin/env bash
# Uploads the dashboards in observability/grafana/dashboards/*.json to Grafana
# Cloud, into a "Deal pipeline" folder, replacing earlier versions (matched by
# each dashboard's uid). The JSON files are the source of truth: edit them,
# commit, re-run this. Edits made in the Grafana UI are overwritten, so export
# them back into the JSON first (Share -> Export -> Save to file).
#
# Needs in common/.env.host (host-only, never committed):
#   GRAFANA_URL       e.g. https://<stack>.grafana.net
#   GRAFANA_SA_TOKEN  a service account token with the Editor role
#                     (Administration -> Users and access -> Service accounts)
#
# Usage: push_grafana_dashboards.sh [--apply]   (dry run by default)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DASH_DIR="${COMMON_DIR}/observability/grafana/dashboards"
FOLDER_UID="deal-pipeline"
FOLDER_TITLE="Deal pipeline"
APPLY=0
[[ "${1:-}" == --apply ]] && APPLY=1

# shellcheck source=load_env.sh
source "${SCRIPT_DIR}/load_env.sh"
load_env_file "${COMMON_DIR}/.env.host"
for var in GRAFANA_URL GRAFANA_SA_TOKEN; do
  [[ -n "${!var:-}" ]] || { echo "${var} is not set in ${COMMON_DIR}/.env.host" >&2; exit 1; }
done
GRAFANA_URL="${GRAFANA_URL%/}"
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

api() {  # api METHOD PATH [BODY_FILE] -> prints "HTTP_CODE BODY"
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "${method}" -H "Authorization: Bearer ${GRAFANA_SA_TOKEN}" -H "Content-Type: application/json" -w '\n%{http_code}')
  [[ -n "${body}" ]] && args+=(--data-binary "@${body}")
  curl "${args[@]}" "${GRAFANA_URL}${path}"
}

shopt -s nullglob
files=("${DASH_DIR}"/*.json)
[[ ${#files[@]} -gt 0 ]] || { echo "No dashboards in ${DASH_DIR}" >&2; exit 1; }

echo "Grafana: ${GRAFANA_URL}  folder: ${FOLDER_TITLE} (${FOLDER_UID})"
for f in "${files[@]}"; do
  printf '  %-20s %s\n' "$(basename "${f}")" "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["uid"], "-", d["title"])' "${f}")"
done
if [[ ${APPLY} -eq 0 ]]; then
  echo "DRY RUN: would create the folder if missing and upload the dashboards above. Re-run with --apply."
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# Folder: create if it doesn't exist (200 = exists, 404 = create).
code="$(api GET "/api/folders/${FOLDER_UID}" | tail -n1)"
if [[ "${code}" == 404 ]]; then
  printf '{"uid":"%s","title":"%s"}' "${FOLDER_UID}" "${FOLDER_TITLE}" > "${tmp}/folder.json"
  out="$(api POST /api/folders "${tmp}/folder.json")"
  [[ "$(tail -n1 <<< "${out}")" == 200 ]] || { echo "Creating the folder failed: ${out}" >&2; exit 1; }
  echo "Created folder ${FOLDER_TITLE}"
elif [[ "${code}" != 200 ]]; then
  echo "Can't read folder ${FOLDER_UID} (HTTP ${code}): check GRAFANA_URL and the token's role" >&2
  exit 1
fi

failed=0
for f in "${files[@]}"; do
  python3 - "${f}" "${FOLDER_UID}" > "${tmp}/body.json" <<'PY'
import json, sys
dashboard = json.load(open(sys.argv[1]))
dashboard.pop("id", None)
print(json.dumps({"dashboard": dashboard, "folderUid": sys.argv[2], "overwrite": True,
                  "message": "pushed by common/scripts/push_grafana_dashboards.sh"}))
PY
  out="$(api POST /api/dashboards/db "${tmp}/body.json")"
  if [[ "$(tail -n1 <<< "${out}")" == 200 ]]; then
    echo "  uploaded $(basename "${f}"): ${GRAFANA_URL}$(head -n1 <<< "${out}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("url",""))')"
  else
    echo "  FAILED $(basename "${f}"): $(head -c 300 <<< "${out}")" >&2
    failed=1
  fi
done
exit "${failed}"
