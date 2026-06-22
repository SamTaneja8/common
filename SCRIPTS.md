# SCRIPTS

This document summarizes the files in `scripts/` for `common`.

Use this file as a quick map when you need to find the right operational helper, maintenance script, or deployment wrapper.

## Runtime and service control

Purpose: start, stop, or invoke the repo runtime and its recurring operational entrypoints.

- `run_job_common.sh`: Shared helper that standardizes how scheduled jobs are launched for scraper-style repos.

## Build and image management

Purpose: build or rebuild Docker images, runtimes, and shared container dependencies.

- `build_scraper_base.sh`: Builds the shared scraper base image, tags it, and optionally cleans up unused older versions.
- `cleanup_scraper_base_images.sh`: Deletes unused shared scraper-base image tags that are no longer referenced by active repos or containers.
