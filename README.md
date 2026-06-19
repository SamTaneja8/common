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

## Shared Scraper Base Image

Build the shared scraper base image with:

```bash
./scripts/build_scraper_base.sh
```

That now creates:

- a versioned tag like `local/scraper-base:py311-playwright-20260619-174500`
- a floating compatibility alias `local/scraper-base:py311-playwright`

If you want a custom version suffix:

```bash
./scripts/build_scraper_base.sh --version 20260619-174500
```

If a scraper repo wants to pin a specific tested base image version, set:

```env
SCRAPER_BASE_IMAGE=local/scraper-base:py311-playwright-20260619-174500
```

in that repo's `.env`.

### Automatic Cleanup

After each versioned build, the build script automatically runs:

- [scripts/cleanup_scraper_base_images.sh](/Users/samtaneja/Codex/common/scripts/cleanup_scraper_base_images.sh)

The cleanup logic preserves:

- any `local/scraper-base:*` image currently used by a Docker container
- any `SCRAPER_BASE_IMAGE=` tag referenced by a sibling repo `.env`
- the freshly built versioned tag
- the floating alias tag

To preview cleanup without deleting anything:

```bash
./scripts/build_scraper_base.sh --cleanup-dry-run
```

## Notes

- Put real proxy credentials in `common_utils/.env` on the machine that runs the jobs.
- `PROXY` and `LOG_SAVE_DAYS` should remain in each application repo's own `.env`
- provider credentials like `EVOMI_*` and `FLOPPYDATA_*` are intended to live here
- set `SHARED_PROXY_ENV_FILE` if the shared proxy env file lives somewhere other than `common_utils/.env`
