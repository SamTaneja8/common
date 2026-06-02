# common_utils

Shared utilities intended to be reused by sibling repos such as:

- `/Users/samtaneja/Codex/datastage`
- `/Users/samtaneja/Codex/dealnews`

This repo is the planned shared home for:

- proxy credential and provider config helpers
- logging helpers
- other small reusable workflow utilities

## Current scope

For now, this repo provides:

- a shared `.env.example` for `EVOMI_*` and `FLOPPYDATA_*`
- a small Python loader that turns those env vars into structured proxy configs
- provider builders for Evomi, Bright Data, Decodo, FloppyData, and direct/no-proxy

## Notes

- Put real proxy credentials in `common_utils/.env` on the machine that runs the jobs.
- `PROXY` and `LOG_SAVE_DAYS` should remain in each application repo's own `.env`
- provider credentials like `EVOMI_*` and `FLOPPYDATA_*` are intended to live here
- set `SHARED_PROXY_ENV_FILE` if the shared proxy env file lives somewhere other than `common_utils/.env`
