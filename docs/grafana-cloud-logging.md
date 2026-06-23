# Grafana Cloud VPS Logging

This repo owns the shared Grafana Cloud and Alloy setup for VPS log shipping.
Application repos still own their job scripts, Docker builds, Docker cleanup,
database setup, and `LOG_SAVE_DAYS` log retention.

## What Grafana Cloud Replaces

Grafana Cloud replaces the visibility parts of the old `vpsmonitor` flow:

- centralized log search through Loki
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

## Install Alloy On A VPS

Install Grafana Alloy from the official apt repository:

```bash
sudo apt-get update
sudo apt-get install -y gpg wget
sudo mkdir -p /etc/apt/keyrings
sudo wget -O /etc/apt/keyrings/grafana.asc https://apt.grafana.com/gpg-full.key
sudo chmod 644 /etc/apt/keyrings/grafana.asc
echo "deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update
sudo apt-get install -y alloy
```

Copy the shared config:

```bash
sudo mkdir -p /etc/alloy
sudo cp /home/botuser/common/observability/alloy/config.alloy /etc/alloy/config.alloy
```

Set credentials in `/etc/default/alloy`:

```bash
COMMON_HOST_LABEL="vps1"
GRAFANA_CLOUD_LOKI_URL="https://logs-prod-000.grafana.net/loki/api/v1/push"
GRAFANA_CLOUD_LOKI_USERNAME="your-loki-username"
GRAFANA_CLOUD_LOKI_TOKEN="your-cloud-access-policy-token"
```

Use `COMMON_HOST_LABEL="vps2"` on VPS2.

Start Alloy:

```bash
sudo systemctl enable alloy
sudo systemctl restart alloy
sudo systemctl status alloy
```

If Alloy cannot read `/var/lib/docker/containers/*/*.log`, either run the Alloy
service with sufficient permissions or remove the `docker_json_logs` source and
rely only on app-owned log files.

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
