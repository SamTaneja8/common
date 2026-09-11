# Grafana alert provisioning

Alert rules for the orchestrator/telemetry work in this repo, written in Grafana's standard file-provisioning YAML format (`apiVersion: 1`). Not deployed or verified against a live Grafana instance from here — this needs real-world validation before you trust it in production.

## Files

- `alerting/host_resources.yaml` — host CPU/memory/disk exhaustion, reading the Prometheus metrics Alloy already ships (`prometheus.exporter.unix "host"` / `prometheus.exporter.cadvisor "docker"` in `../alloy/config.alloy`). No new metrics collection needed.
- `alerting/job_telemetry.yaml` — a `job_run_metering` row stuck in `RUNNING` for 90+ minutes. Deliberately does **not** alert on every individual job `FAILED` — with every production `pipeline.yaml` step now `on_failure: continue`, some failures (e.g. dealnews.com's own 403) are expected and would make that pure noise.
- `alerting/pipeline_run.yaml` — the three orchestrator-level checks: a run ending `PARTIAL`/`FAILED` (the main signal now that individual failures don't halt anything and the orchestrator's own exit code stays 0 for `PARTIAL`), a run stuck `RUNNING` past 3 hours, and a dead-man's-switch for zero `pipeline_run` rows in 26 hours (catches cron not firing or the orchestrator crashing before it can even create a row — neither other alert can see this, since it produces no row to alert on).
- `alerting/contact_points.yaml` — placeholder Discord receiver + routing policy for all of the above.

## Before applying any of this

1. **Fill in the placeholders.** Every file has `<PROMETHEUS_DATASOURCE_UID>`, `<MYSQL_TELEMETRY_DATASOURCE_UID>`, or `<DISCORD_WEBHOOK_URL>` — real values, not invented ones, since I have no access to your Grafana Cloud instance to look them up. Find a data source's UID in Grafana under Connections → Data sources → (the data source) → its settings page URL.
2. **Add the MySQL data source if it isn't already configured** — the `job_run_metering`/`pipeline_run` queries need a MySQL data source pointed at `telemetry_db` in Grafana Cloud, which was already recommended (Private Data Source Connect, not a public port) back when `deal_correlation`/`pipeline_run` were first scoped. If that's not set up yet, `job_telemetry.yaml`/`pipeline_run.yaml` have nothing to query against.
3. **Apply `pipeline_run.sql` first** if you haven't — these alert rules query columns (`pipeline_run.status`, `failed_step_name`, etc.) that don't exist until that migration runs against `telemetry_db`.

## How to apply

Depends on how your Grafana Cloud is set up:

- **If you run your own Grafana (self-hosted or on a VM) pointed at Grafana Cloud's Prometheus/Loki/MySQL as remote data sources**, drop these files into that Grafana's provisioning directory (typically `/etc/grafana/provisioning/alerting/`) and restart/reload Grafana — it picks up file-provisioned alerts automatically.
- **If you're on Grafana Cloud only (no self-hosted Grafana)**, these YAML files aren't directly consumable — you'd need to either (a) convert them via the [Grafana Alerting HTTP API](https://grafana.com/docs/grafana/latest/alerting/set-up/provision-alerting-resources/http-api-provisioning/) (`POST /api/v1/provisioning/alert-rules`, one call per rule, body derived from each rule's YAML), or (b) recreate them via the Grafana Cloud UI (Alerting → Alert rules → New) using the same query/condition logic documented in each file's comments. I didn't build either of those paths since I don't know which one applies to your setup — say which one you're on and I'll adapt.
