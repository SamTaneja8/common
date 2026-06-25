# Grafana Cloud VPS Observability

This repo owns the shared Grafana Cloud and Alloy setup for VPS log and metric
shipping.
Application repos still own their job scripts, Docker builds, Docker cleanup,
database setup, and `LOG_SAVE_DAYS` log retention.

## What Grafana Cloud Replaces

Grafana Cloud replaces the visibility parts of the old `vpsmonitor` flow:

- centralized log search through Loki
- host CPU, memory, disk, filesystem, load, and network metrics
- Docker container metrics through cAdvisor
- Alloy self metrics
- dashboards built from logs and metrics
- alert rules and notifications
- optional external uptime checks through Synthetic Monitoring

Grafana Cloud does not run local VPS commands. Cron, scraper jobs, Docker
builds, Docker cleanup, and database maintenance remain local.

## Log Flow

The scraper repos write durable logs under their own repo folders:

- `/home/botuser/dealnews1/logs/container_runs/*.log`
- `/home/botuser/dealnews1/logs/cron_runs/*.log`
- `/home/botuser/dealmoon1/logs/container_runs/*.log`
- `/home/botuser/dealmoon1/logs/cron_runs/*.log`
- `/var/log/{syslog,messages,*.log}`

Always-on infrastructure containers, such as MySQL, Caddy, Headscale, n8n, and
networking services, are not cleaned up by scraper jobs. Alloy can also tail
Docker's own JSON log files for those persistent containers:

- `/var/lib/docker/containers/*/*.log`

Alloy tails those files with:

- [observability/alloy/config.alloy](/Users/samtaneja/Codex/common/observability/alloy/config.alloy)

Each repo remains responsible for deleting old log files. `LOG_SAVE_DAYS` is
resolved in this order:

1. exported environment value
2. app repo `.env`
3. shared `/home/botuser/common/.env`
4. final safety fallback of `7` only if no valid value exists

Docker-managed JSON logs should be controlled with Docker log rotation settings
on the VPS, not by scraper cleanup scripts.

## Metric Flow

The shared Alloy config also forwards VPS metrics to Grafana Cloud Metrics:

- `prometheus.exporter.unix` for host metrics
- `prometheus.exporter.cadvisor` for Docker/container metrics
- `prometheus.exporter.self` for Alloy health

The same config works on VPS1 and VPS2. Set a different `COMMON_HOST_LABEL` on
each VPS so dashboards and alerts can filter by host.

## Install Alloy On A VPS

Grafana Cloud's onboarding script is useful for installing Alloy and proving
that the credentials work. Do not commit the generated `/etc/alloy/config.alloy`
because it hardcodes the API key.

Use the values Grafana Cloud provided, but rotate the API key first if it has
been pasted into chat or committed anywhere. Put the values in
`/home/botuser/common/.env` on each VPS:

```bash
COMMON_HOST_LABEL="vps1"

GCLOUD_HOSTED_METRICS_URL="https://prometheus-prod-66-prod-us-east-3.grafana.net/api/prom/push"
GCLOUD_HOSTED_METRICS_ID="3327852"
GCLOUD_SCRAPE_INTERVAL="60s"

GCLOUD_HOSTED_LOGS_URL="https://logs-prod-042.grafana.net/loki/api/v1/push"
GCLOUD_HOSTED_LOGS_ID="1659601"
GCLOUD_RW_API_KEY="your-rotated-grafana-cloud-api-key"
```

Use `COMMON_HOST_LABEL="vps2"` on VPS2. Keep the Grafana credentials from that
same Grafana Cloud stack unless you intentionally split VPS1 and VPS2 into
different stacks.

Then run the shared setup script:

```bash
cd /home/botuser/common
./scripts/setup_grafana_alloy.sh --install-alloy --host-label vps1
```

On VPS2, use `--host-label vps2`.

The script:

- optionally runs Grafana Cloud's Linux binary installer
- copies [observability/alloy/config.alloy](/Users/samtaneja/Codex/common/observability/alloy/config.alloy) to `/etc/alloy/config.alloy`
- writes the `GCLOUD_*` values to `/etc/default/alloy`
- enables and restarts the Alloy service when `systemctl` is available

Preview without changing the VPS:

```bash
./scripts/setup_grafana_alloy.sh --host-label vps1 --dry-run
```

If Alloy cannot read `/var/lib/docker/containers/*/*.log`, either run the Alloy
service with sufficient permissions or remove the `docker_json_logs` source and
rely only on app-owned log files.

If Alloy cannot scrape Docker/container metrics, add the `alloy` service user to
the Docker group or run Alloy with sufficient access to `/var/run/docker.sock`.

Detailed MySQL internals are not enabled in the shared config yet. MySQL will
still appear as a Docker container with logs and container metrics. Add a MySQL
exporter later if you want query/cache/connection metrics inside Grafana.

## Cron Wrapper

Use [scripts/run_cron_job.sh](/Users/samtaneja/Codex/common/scripts/run_cron_job.sh) from cron to run one repo-owned job now, with no catch-up replay:

```cron
0 * * * * /home/botuser/common/scripts/run_cron_job.sh --name dealnews1_full_cycle --workdir /home/botuser/dealnews1 -- /home/botuser/dealnews1/jobs/run_full_cycle.sh
5 * * * * /home/botuser/common/scripts/run_cron_job.sh --name dealmoon1_full_cycle --workdir /home/botuser/dealmoon1 -- /home/botuser/dealmoon1/jobs/run_full_cycle.sh
```

The wrapper:

- creates one durable cron log in `<repo>/logs/cron_runs/`
- prevents overlapping runs of the same job
- prunes old cron logs using `LOG_SAVE_DAYS`
- uses the shared `common/.env` value when the app repo does not provide one
- does not itself build Docker images
- does not itself remove Docker images or containers
- does not replay missed jobs

The repo-owned full-cycle scripts invoked by cron may build images and clean up
their own containers/images. You can still run those operations manually:

```bash
cd /home/botuser/dealnews1
./scripts/rebuild_images.sh --refresh-base
./scripts/cleanup_docker_runtime.sh

cd /home/botuser/dealmoon1
./scripts/rebuild_images.sh --refresh-base
./scripts/cleanup_docker_runtime.sh
```

Those cleanup scripts target only the repo-owned scraper containers/images such
as `dealnews1-scraper`, `dealnews1-redirects`, `dealnews1:latest`,
`dealmoon1-scraper`, `dealmoon1-redirects`, and `dealmoon1:latest`. Persistent
infrastructure containers are out of scope.
