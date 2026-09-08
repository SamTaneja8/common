-- pipeline_run: tracks one orchestrator invocation (the sequence of steps
-- defined in common/orchestration/pipeline.yaml) as a single row, so the
-- dashboard can show "step 4/9 running" and alerting can watch for a
-- stalled/failed sequence without inferring it from scattered
-- job_run_metering rows. Lives in telemetry_db (see dealnews1/sql/
-- telemetry_db.sql for job_run_metering's DDL, which this matches
-- conventions with: id + a separate unique run-identifier column, status as
-- free-form varchar rather than ENUM, created_at/updated_at housekeeping).
--
-- Apply against telemetry_db by hand (no migration tool for this shared
-- database):
--   mysql -h <TELEMETRY_MYSQL_HOST> -P <TELEMETRY_MYSQL_PORT> \
--     -u <TELEMETRY_MYSQL_USER> -p <TELEMETRY_MYSQL_DATA> < pipeline_run.sql

CREATE TABLE IF NOT EXISTS `pipeline_run` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `pipeline_run_id` varchar(64) NOT NULL COMMENT 'UUID minted once per orchestrator invocation',
  `pipeline_name` varchar(100) NOT NULL DEFAULT 'daily-full-pipeline' COMMENT 'which pipeline.yaml definition this run used',
  `status` varchar(16) NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING, SUCCESS, PARTIAL, FAILED',
  `trigger_source` varchar(32) DEFAULT NULL COMMENT 'cron, manual, etc -- same convention as job_run_metering.trigger_source',
  `total_steps` int NOT NULL,
  `current_step_index` int NOT NULL DEFAULT 0,
  `current_step_name` varchar(150) DEFAULT NULL,
  `failed_step_name` varchar(150) DEFAULT NULL COMMENT 'set when status=FAILED: the step whose on_failure=halt stopped the run',
  `started_at` datetime NOT NULL,
  `ended_at` datetime DEFAULT NULL,
  `duration_seconds` int DEFAULT NULL,
  `config_json` json DEFAULT NULL COMMENT 'resolved pipeline.yaml for this run -- actual batch_size/repeat/on_failure per step, so a later config edit does not make historical runs unreadable',
  `error_summary` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_pipeline_run_pipeline_run_id` (`pipeline_run_id`),
  KEY `idx_pipeline_run_status_started_at` (`status`,`started_at`),
  KEY `idx_pipeline_run_started_at` (`started_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Links each individual job_run_metering row back to the pipeline_run it was
-- part of, so "every job in this pipeline run" is a plain filtered query
-- instead of guessing from timestamp ranges. NULL for jobs run standalone
-- (manually, or a bare cron entry outside the orchestrator) -- orchestrated
-- and non-orchestrated runs share the same table either way.
ALTER TABLE `job_run_metering`
  ADD COLUMN `pipeline_run_id` varchar(64) DEFAULT NULL AFTER `run_uuid`,
  ADD KEY `idx_job_run_metering_pipeline_run_id` (`pipeline_run_id`);
