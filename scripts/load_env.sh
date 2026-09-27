#!/usr/bin/env bash
# Sourced by this repo's scripts: load_env_file FILE exports every setting in
# a Docker Compose-style .env file WITHOUT executing it. Compose accepts
# values a shell can't parse (spaces, ; ( ) |, "KEY: value" lines), so
# `set -a; source .env` breaks on real .env files. Values are taken
# literally (no ${VAR} expansion, as before: no template uses it); a value in
# the file overrides the environment, like `source` did.
load_env_file() {
  local file="$1" key value
  [[ -f "${file}" ]] || return 0
  while IFS=$'\t' read -r key value; do
    [[ -n "${key}" ]] && export "${key}=${value}"
  done < <(awk -v q="'" '
    { sub(/\r$/, "") }
    /^[[:space:]]*(#|$)/ { next }
    {
      line = $0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      if (match(line, /^[A-Za-z_][A-Za-z0-9_]*[[:space:]]*[=:]/)) {
        k = substr(line, 1, RLENGTH - 1); sub(/[[:space:]]+$/, "", k)
        v = substr(line, RLENGTH + 1); sub(/^[[:space:]]+/, "", v); sub(/[[:space:]]+$/, "", v)
        f = substr(v, 1, 1)
        if (length(v) >= 2 && (f == "\"" || f == q) && substr(v, length(v), 1) == f) v = substr(v, 2, length(v) - 2)
        print k "\t" v
      }
    }' "${file}")
  return 0
}
