#!/usr/bin/env bash
# Keeps only the last KEEP_DAYS days of a timestamped log file (lines that
# start "YYYY-MM-DD "); untimestamped lines such as tracebacks go with the
# line before them. Used weekly (pipeline.yaml: orchestrator-log-trim) on the
# orchestrator's log, which cron appends to and nothing else rotates.
#
# Trims in place (same file, same inode) so a process appending to it with
# >> keeps writing to the live file. A line appended during the few
# milliseconds of the rewrite can be lost.
#
# Usage: trim_log.sh [FILE] [KEEP_DAYS]
#   FILE       default logs/orchestrator.log; relative paths are relative to common
#   KEEP_DAYS  default 28
set -euo pipefail

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
file="${1:-logs/orchestrator.log}"
[[ "${file}" == /* ]] || file="${COMMON_DIR}/${file}"
keep_days="${2:-28}"
[[ "${keep_days}" =~ ^[0-9]+$ ]] || { echo "KEEP_DAYS must be a number: ${keep_days}" >&2; exit 2; }

if [[ ! -f "${file}" ]]; then
  echo "Nothing to trim: ${file} doesn't exist."
  exit 0
fi

# GNU date on the servers; BSD date on a Mac.
cutoff="$(date -u -d "-${keep_days} days" +%Y-%m-%d 2>/dev/null || date -u -v-"${keep_days}"d +%Y-%m-%d)"
before_lines=$(wc -l < "${file}" | tr -d ' ')
before_size=$(du -h "${file}" | cut -f1)

tmp="$(mktemp "${file}.trim.XXXXXX")"
trap 'rm -f "${tmp}"' EXIT
awk -v cutoff="${cutoff}" '
  /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] / { keep = (substr($0, 1, 10) >= cutoff) }
  keep' "${file}" > "${tmp}"
cat "${tmp}" > "${file}"

after_lines=$(wc -l < "${file}" | tr -d ' ')
echo "Trimmed ${file} to lines from ${cutoff} on: ${before_lines} -> ${after_lines} lines (was ${before_size})."
