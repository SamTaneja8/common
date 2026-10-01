-- Read-only MySQL login for Grafana Cloud (dashboards and alert rules in
-- observability/grafana/). Grafana reaches unified-mysql through the PDC
-- agent (observability/grafana/pdc/), which runs on scraper-network, so the
-- login comes from a Docker address, hence '%'. MySQL itself is only
-- published on 127.0.0.1, so nothing outside VPS1 can use this login.
--
-- Run once as root in unified-mysql, with a real password in place of
-- <password> (never commit it). Then use grafana_reader / that password in
-- the Grafana Cloud MySQL data source.

CREATE USER IF NOT EXISTS 'grafana_reader'@'%' IDENTIFIED BY '<password>'
  WITH MAX_USER_CONNECTIONS 5;

-- SELECT only: dashboards can't change anything. telemetry_db is all
-- operational metering; dealstage_db holds the deal pipeline's tables
-- (amzn_*, llm_*, reviewgate_*), none of which store credentials.
GRANT SELECT ON telemetry_db.* TO 'grafana_reader'@'%';
GRANT SELECT ON dealstage_db.* TO 'grafana_reader'@'%';

-- Check:
SHOW GRANTS FOR 'grafana_reader'@'%';
