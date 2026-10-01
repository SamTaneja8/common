# Grafana Cloud: dashboards and alerts

## Dashboards (replacing the dashboard app)

Three dashboards, kept as JSON in `dashboards/` and uploaded by
`scripts/push_grafana_dashboards.sh`. They query MySQL directly
(`telemetry_db`, `dealstage_db`) through one Grafana Cloud MySQL data source,
reached over Private Data Source Connect (PDC), so nothing on VPS1 opens a
port. Logs are already in Grafana Cloud (Loki, shipped by Alloy): use Explore.

| File | Dashboard | Replaces (dashboard app page) |
|---|---|---|
| `operations.json` | Deal pipeline: operations: pipeline and job runs, failures, hung jobs, scraper intake, proxy success, Playwright exceptions | Operations |
| `deals.json` | Deal pipeline: deals and review: review queue by status, page scans, AI runs, publish log, failed AI tasks | Overview, Pipeline, Review queue |
| `ai_usage.json` | Deal pipeline: AI usage and cost: cost and tokens by model/stage, budget left, provider balances | Usage |

### Setup (once)

1. **PDC network** (Grafana Cloud): Connections -> Private data source
   connect -> add a network. Copy its signing token, hosted Grafana ID and
   cluster into `pdc/.env` on VPS1 (`cp pdc/.env.example pdc/.env`,
   `chmod 600`), then start the agent:
   `cd ~/common/observability/grafana/pdc && docker compose up -d`.
   Grafana's PDC page should show the agent as connected.
2. **Read-only MySQL login**: run `../sql/grafana_reader.sql` as root in
   unified-mysql with a real password (SELECT on `telemetry_db` and
   `dealstage_db` only).
3. **Data source** (Grafana Cloud): Connections -> Add new connection ->
   MySQL. Host `unified-mysql:3306`, database `telemetry_db`, user
   `grafana_reader`, and under "Private data source connect" pick the network
   from step 1. Save & test. Queries name their database
   (`dealstage_db.amzn_review_queue`), so one data source serves all three.
4. **Upload**: put `GRAFANA_URL` and `GRAFANA_SA_TOKEN` (Editor service
   account) in `common/.env.host`, then
   `~/common/scripts/push_grafana_dashboards.sh` (dry run) and again with
   `--apply`. Each dashboard's "MySQL" picker selects the data source.

All times are UTC (the dashboards are set to UTC; telemetry is written in
UTC). To change a dashboard, edit its JSON (or edit in Grafana and export it
over the file), commit, and re-run the push; uploads replace by `uid`.

## Alert rules


Alert rules for the orchestrator/telemetry work in this repo, written in Grafana's standard file-provisioning YAML format (`apiVersion: 1`). Not deployed or verified against a live Grafana instance from here — this needs real-world validation before you trust it in production.

## Files

- `alerting/host_resources.yaml` — host CPU/memory/disk exhaustion, reading the Prometheus metrics Alloy already ships (`prometheus.exporter.unix "host"` / `prometheus.exporter.cadvisor "docker"` in `../alloy/config.alloy`). No new metrics collection needed.
- `alerting/job_telemetry.yaml` — a `job_run_metering` row stuck in `RUNNING` for 90+ minutes. Deliberately does **not** alert on every individual job `FAILED` — with every production `pipeline.yaml` step now `on_failure: continue`, some failures (e.g. dealnews.com's own 403) are expected and would make that pure noise.
- `alerting/pipeline_run.yaml` — the three orchestrator-level checks: a run ending `PARTIAL`/`FAILED`, a run stuck `RUNNING` past 3 hours, and a dead-man's-switch for zero `pipeline_run` rows in 26 hours (catches cron not firing or the orchestrator crashing before it can even create a row).
- `alerting/discord_delivery.yaml` — detects summary Discord alerts that failed to send after the scraper job finished.
- `alerting/contact_points.yaml` — placeholder Discord receiver + routing policy for all of the above.

## Before applying any of this

1. **Fill in the placeholders.** Every file has `<PROMETHEUS_DATASOURCE_UID>`, `<MYSQL_TELEMETRY_DATASOURCE_UID>`, or `<DISCORD_WEBHOOK_URL>` — real values, not invented ones, since I have no access to your Grafana Cloud instance to look them up. Find a data source's UID in Grafana under Connections → Data sources → (the data source) → its settings page URL.
2. **Add the MySQL data source if it isn't already configured** — the `job_run_metering`/`pipeline_run` queries need a MySQL data source pointed at `telemetry_db` in Grafana Cloud, which was already recommended (Private Data Source Connect, not a public port) back when `deal_correlation`/`pipeline_run` were first scoped. If that's not set up yet, `job_telemetry.yaml`/`pipeline_run.yaml` have nothing to query against.
3. **Apply `pipeline_run.sql` first** if you haven't — these alert rules query columns (`pipeline_run.status`, `failed_step_name`, etc.) that don't exist until that migration runs against `telemetry_db`.
4. **Apply `../sql/alert_delivery_columns.sql`** before enabling `discord_delivery.yaml`.
5. **Set the Discord/Grafana link env vars in the app jobs** if you want Discord alerts to deep-link back into Grafana: `GRAFANA_BASE_URL`, `GRAFANA_LOGS_DASHBOARD_UID`, and optionally `GRAFANA_RUN_DASHBOARD_UID`. The logs dashboard accepts `var-search`, `var-run_uuid`, `var-pipeline_run_id`, `var-content_id`, and `var-asin`.

## How to apply

Depends on how your Grafana Cloud is set up:

- **If you run your own Grafana (self-hosted or on a VM) pointed at Grafana Cloud's Prometheus/Loki/MySQL as remote data sources**, drop these files into that Grafana's provisioning directory (typically `/etc/grafana/provisioning/alerting/`) and restart/reload Grafana — it picks up file-provisioned alerts automatically.
- **If you're on Grafana Cloud only (no self-hosted Grafana)**, these YAML files aren't directly consumable — you'd need to either (a) convert them via the [Grafana Alerting HTTP API](https://grafana.com/docs/grafana/latest/alerting/set-up/provision-alerting-resources/http-api-provisioning/) (`POST /api/v1/provisioning/alert-rules`, one call per rule, body derived from each rule's YAML), or (b) recreate them via the Grafana Cloud UI (Alerting → Alert rules → New) using the same query/condition logic documented in each file's comments. I didn't build either of those paths since I don't know which one applies to your setup — say which one you're on and I'll adapt.
