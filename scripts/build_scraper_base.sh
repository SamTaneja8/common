#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_TAG="${1:-local/scraper-base:py311-playwright}"

docker build \
  -f "${PROJECT_DIR}/Dockerfile.scraper-base" \
  -t "${IMAGE_TAG}" \
  "${PROJECT_DIR}"

echo "Built ${IMAGE_TAG}"
