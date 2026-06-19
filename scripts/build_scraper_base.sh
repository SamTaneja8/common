#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_REPO="local/scraper-base"
ALIAS_TAG="py311-playwright"
VERSION_SUFFIX="$(date -u +%Y%m%d-%H%M%S)"
SKIP_CLEANUP=false
CLEANUP_DRY_RUN=false
LEGACY_IMAGE_TAG=""

usage() {
  cat >&2 <<'EOF'
Usage: build_scraper_base.sh [options] [legacy-full-image-tag]

Options:
  --version <suffix>       Version suffix appended to the alias tag.
                           Example result: local/scraper-base:py311-playwright-20260619-174500
  --image-repo <repo>      Docker repository name. Default: local/scraper-base
  --alias-tag <tag>        Floating compatibility tag. Default: py311-playwright
  --skip-cleanup           Do not run unused-version cleanup after the build.
  --cleanup-dry-run        Show which old tags would be removed without deleting them.

Legacy compatibility:
  Passing a single positional image tag keeps the old behavior and only tags that image.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || usage
      VERSION_SUFFIX="$2"
      shift 2
      ;;
    --image-repo)
      [[ $# -ge 2 ]] || usage
      IMAGE_REPO="$2"
      shift 2
      ;;
    --alias-tag)
      [[ $# -ge 2 ]] || usage
      ALIAS_TAG="$2"
      shift 2
      ;;
    --skip-cleanup)
      SKIP_CLEANUP=true
      shift
      ;;
    --cleanup-dry-run)
      CLEANUP_DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      if [[ -n "${LEGACY_IMAGE_TAG}" ]]; then
        usage
      fi
      LEGACY_IMAGE_TAG="$1"
      shift
      ;;
  esac
done

VERSIONED_TAG="${IMAGE_REPO}:${ALIAS_TAG}-${VERSION_SUFFIX}"
FLOATING_TAG="${IMAGE_REPO}:${ALIAS_TAG}"

BUILD_ARGS=(-f "${PROJECT_DIR}/Dockerfile.scraper-base")
if [[ -n "${LEGACY_IMAGE_TAG}" ]]; then
  BUILD_ARGS+=(-t "${LEGACY_IMAGE_TAG}")
else
  BUILD_ARGS+=(-t "${VERSIONED_TAG}" -t "${FLOATING_TAG}")
fi

docker build \
  "${BUILD_ARGS[@]}" \
  "${PROJECT_DIR}"

if [[ -n "${LEGACY_IMAGE_TAG}" ]]; then
  echo "Built ${LEGACY_IMAGE_TAG}"
  exit 0
fi

printf '%s\n' "${VERSIONED_TAG}" > "${PROJECT_DIR}/.last_scraper_base_build"
echo "Built ${VERSIONED_TAG}"
echo "Updated floating tag ${FLOATING_TAG}"

if [[ "${SKIP_CLEANUP}" == "false" ]]; then
  CLEANUP_ARGS=("${PROJECT_DIR}/scripts/cleanup_scraper_base_images.sh" --image-repo "${IMAGE_REPO}" --preserve "${VERSIONED_TAG}" --preserve "${FLOATING_TAG}")
  if [[ "${CLEANUP_DRY_RUN}" == "true" ]]; then
    CLEANUP_ARGS+=(--dry-run)
  fi
  bash "${CLEANUP_ARGS[@]}"
fi
