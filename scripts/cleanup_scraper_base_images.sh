#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_DIR="$(cd "${PROJECT_DIR}/.." && pwd)"

IMAGE_REPO="local/scraper-base"
DRY_RUN=false
PRESERVE_REFS=()

usage() {
  cat >&2 <<'EOF'
Usage: cleanup_scraper_base_images.sh [options]

Options:
  --image-repo <repo>    Base image repository to inspect. Default: local/scraper-base
  --preserve <ref>       Extra fully-qualified image ref to preserve.
  --dry-run              Print unused tags without deleting them.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image-repo)
      [[ $# -ge 2 ]] || usage
      IMAGE_REPO="$2"
      shift 2
      ;;
    --preserve)
      [[ $# -ge 2 ]] || usage
      PRESERVE_REFS+=("$2")
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage
      ;;
  esac
done

declare -A PRESERVE=()

add_preserve() {
  local ref="$1"
  [[ -n "${ref}" ]] || return 0
  PRESERVE["${ref}"]=1
}

for ref in "${PRESERVE_REFS[@]}"; do
  add_preserve "${ref}"
done

while IFS= read -r ref; do
  if [[ "${ref}" == "${IMAGE_REPO}:"* ]]; then
    add_preserve "${ref}"
  fi
done < <(docker ps -a --format '{{.Image}}' 2>/dev/null || true)

while IFS= read -r env_file; do
  while IFS= read -r ref; do
    add_preserve "${ref}"
  done < <(
    sed -n 's/^SCRAPER_BASE_IMAGE=\(.*\)$/\1/p' "${env_file}" \
      | sed 's/[[:space:]]*$//' \
      | sed 's/^"//; s/"$//; s/^'\''//; s/'\''$//'
  )
done < <(find "${WORKSPACE_DIR}" -maxdepth 2 -name '.env' -type f 2>/dev/null)

mapfile -t IMAGE_REFS < <(docker images --format '{{.Repository}}:{{.Tag}}' "${IMAGE_REPO}" 2>/dev/null | sort -u)

if [[ ${#IMAGE_REFS[@]} -eq 0 ]]; then
  echo "No images found for ${IMAGE_REPO}"
  exit 0
fi

REMOVE_REFS=()
for ref in "${IMAGE_REFS[@]}"; do
  if [[ -z "${PRESERVE[${ref}]+x}" ]]; then
    REMOVE_REFS+=("${ref}")
  fi
done

if [[ ${#REMOVE_REFS[@]} -eq 0 ]]; then
  echo "No unused image tags to remove for ${IMAGE_REPO}"
  exit 0
fi

echo "Preserving:"
for ref in $(printf '%s\n' "${!PRESERVE[@]}" | sort); do
  if [[ "${ref}" == "${IMAGE_REPO}:"* ]]; then
    echo "  ${ref}"
  fi
done

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "Would remove:"
  printf '  %s\n' "${REMOVE_REFS[@]}"
  exit 0
fi

echo "Removing unused image tags:"
for ref in "${REMOVE_REFS[@]}"; do
  echo "  ${ref}"
  docker image rm "${ref}" >/dev/null || true
done
