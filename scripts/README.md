# scripts/

Operational helper scripts for `common`, shared across dealnews1/dealmoon1/amazonnew and other scraper-style repos. See `../SCRIPTS.md` for more detail.

- **`backup_postgres_to_backblaze.sh`** — Incremental Postgres table backups to Backblaze B2 for a repo whose Postgres runs on this host (dealvant on vps2): dumps rows above each table's watermark via `psql` inside the container, uploads with rclone, then records the new watermark. The repo's `jobs/run_backup.sh` passes the table specs and container; B2 settings come from `common/.env.host`. `--check` tests settings, container and B2 without dumping. MySQL backups stay in setup_mysql.
- **`build_scraper_base.sh`** — Builds the shared scraper-base Docker image, tags it with a versioned tag and a floating alias tag, and optionally runs cleanup of unused older versions afterward. Skips the build when nothing that goes into the image changed (a fingerprint label on the image); `--force` rebuilds anyway, e.g. for base-image security updates.
- **`cleanup_scraper_base_images.sh`** — Deletes unused local `local/scraper-base` image tags, preserving any tag still referenced by a running/stopped container, a repo's `.env` `SCRAPER_BASE_IMAGE` setting, or an explicit `--preserve` ref.
- **`push_grafana_dashboards.sh`** — Uploads `observability/grafana/dashboards/*.json` to Grafana Cloud into a "Deal pipeline" folder, replacing earlier versions by uid. Needs `GRAFANA_URL` and `GRAFANA_SA_TOKEN` in `.env.host`. Dry run unless `--apply`.
- **`run_cron_job.sh`** — VPS cron wrapper that runs one repo-owned command now (no missed-job replay), prevents overlapping runs with a lock file, writes a durable cron log, and prunes old logs using `LOG_SAVE_DAYS`.
- **`run_job_common.sh`** — Shared wrapper that standardizes how scheduled jobs are launched for scraper-style repos, reporting start/failure to `common_utils.metering_cli` while teeing output and preserving the wrapped command's exit code.
- **`trim_log.sh`** — Keeps the last N days (default 28) of a timestamped log, in place so appenders keep writing; run weekly on `logs/orchestrator.log` by the `orchestrator-log-trim` step.
- **`setup_grafana_alloy.sh`** — Installs/configures the shared Grafana Cloud Alloy plumbing on a VPS from `common/.env`, writing `/etc/default/alloy` and `/etc/alloy/config.alloy`, then restarts Alloy.
