-- api_balance_check: one row per provider per run of
-- common_utils.api_balance_check (OpenAI/HuggingFace/DeepSeek/Gemini
-- authorization + balance checks), so balance trends over time are
-- queryable. Lives in telemetry_db, same database as job_run_metering and
-- pipeline_run -- matches their conventions (bigint id, free-form varchar
-- status-ish fields rather than ENUM, created_at housekeeping).
--
-- The proxy equivalent (common_utils.proxy_balance_check, Evomi/FloppyData)
-- is intentionally log-only with no table -- see that module's docstring.
--
-- Apply against telemetry_db by hand (no migration tool for this shared
-- database):
--   mysql -h <TELEMETRY_MYSQL_HOST> -P <TELEMETRY_MYSQL_PORT> \
--     -u <TELEMETRY_MYSQL_USER> -p <TELEMETRY_MYSQL_DATA> < api_balance_check.sql

CREATE TABLE IF NOT EXISTS `api_balance_check` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `checked_at` datetime NOT NULL,
  `provider` varchar(50) NOT NULL COMMENT 'ChatGPT/OpenAI, HuggingFace, DeepSeek, Gemini',
  `configured` tinyint(1) NOT NULL DEFAULT 0,
  `auth_ok` tinyint(1) DEFAULT NULL COMMENT 'NULL when the auth check itself errored rather than pass/fail',
  `auth_status_code` int DEFAULT NULL,
  `auth_message` text,
  `balance_usd` decimal(12,4) DEFAULT NULL,
  `balance_message` text,
  `alert_threshold_usd` decimal(12,4) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_api_balance_check_provider_checked_at` (`provider`,`checked_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
