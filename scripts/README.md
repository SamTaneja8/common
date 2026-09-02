# scripts/

Operational helper scripts for `common`, shared across dealnews1/dealmoon1/amazonnew and other scraper-style repos. See `../SCRIPTS.md` for more detail.

- **`build_scraper_base.sh`** — Builds the shared scraper-base Docker image, tags it with a versioned tag and a floating alias tag, and optionally runs cleanup of unused older versions afterward.
- **`cleanup_scraper_base_images.sh`** — Deletes unused local `local/scraper-base` image tags, preserving any tag still referenced by a running/stopped container, a repo's `.env` `SCRAPER_BASE_IMAGE` setting, or an explicit `--preserve` ref.
- **`run_cron_job.sh`** — VPS cron wrapper that runs one repo-owned command now (no missed-job replay), prevents overlapping runs with a lock file, writes a durable cron log, and prunes old logs using `LOG_SAVE_DAYS`.
- **`run_job_common.sh`** — Shared wrapper that standardizes how scheduled jobs are launched for scraper-style repos, reporting start/failure to `common_utils.metering_cli` while teeing output and preserving the wrapped command's exit code.
- **`setup_grafana_alloy.sh`** — Installs/configures the shared Grafana Cloud Alloy plumbing on a VPS from `common/.env`, writing `/etc/default/alloy` and `/etc/alloy/config.alloy`, then restarts Alloy.
