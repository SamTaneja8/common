-- deal_correlation: cross-repo lookup so a Grafana dashboard can trace one
-- content_id/asin all the way to its published Dealvant record. Lives in the
-- shared telemetry_db (same database as job_run_metering) so one Grafana
-- MySQL data source covers both without a cross-engine join -- the published
-- side lives in a separate Postgres database (Dealvant) that can't be joined
-- live, so this table materializes that linkage instead.
--
-- Apply against telemetry_db. There is no schema-migration tool for this
-- shared database today (unlike each repo's own apply_schema_updates.py), so
-- this needs to be run by hand:
--   mysql -h <TELEMETRY_MYSQL_HOST> -P <TELEMETRY_MYSQL_PORT> \
--     -u <TELEMETRY_MYSQL_USER> -p <TELEMETRY_MYSQL_DATA> < deal_correlation.sql

CREATE TABLE IF NOT EXISTS `deal_correlation` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `source_repo` VARCHAR(64) NOT NULL COMMENT 'dealnews1 or dealmoon1',
    `content_id` VARCHAR(255) NOT NULL COMMENT 'dealnews1 Deal.content_id or dealmoon1 Deal.dealmoon_id',
    `asin` VARCHAR(32) NOT NULL,
    `marketplace` VARCHAR(32) DEFAULT NULL,
    `matched_at` DATETIME NOT NULL,
    `dealvant_target_table` VARCHAR(64) DEFAULT NULL,
    `dealvant_target_id` VARCHAR(255) DEFAULT NULL,
    `published_at` DATETIME DEFAULT NULL,
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_deal_correlation_source` (`source_repo`, `content_id`),
    KEY `ix_deal_correlation_asin` (`asin`),
    KEY `ix_deal_correlation_dealvant_target` (`dealvant_target_table`, `dealvant_target_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- updated_at is the backup watermark (dashboard's jobs/run_backup.sh): rows
-- are updated in place when a deal is published, so an id watermark would
-- keep only the pre-publish version. Added here too for tables created before
-- the column existed; safe to re-run.
SET @db_name := DATABASE();

SET @ddl := (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE `deal_correlation` ADD COLUMN `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP AFTER `published_at`',
    'SELECT 1'
  )
  FROM information_schema.columns
  WHERE table_schema = @db_name AND table_name = 'deal_correlation' AND column_name = 'updated_at'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
