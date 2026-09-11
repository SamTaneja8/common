-- Adds Discord delivery status to summary alert telemetry. Safe to re-run.
-- Apply against telemetry_db:
--   mysql -h <TELEMETRY_MYSQL_HOST> -P <TELEMETRY_MYSQL_PORT> \
--     -u <TELEMETRY_MYSQL_USER> -p <TELEMETRY_MYSQL_DATA> < alert_delivery_columns.sql

SET @db_name := DATABASE();

SET @ddl := (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE `deals_ingest` ADD COLUMN `discord_sent` tinyint(1) DEFAULT NULL AFTER `discord_description`',
    'SELECT 1'
  )
  FROM information_schema.columns
  WHERE table_schema = @db_name AND table_name = 'deals_ingest' AND column_name = 'discord_sent'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl := (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE `deals_ingest` ADD COLUMN `discord_error` text DEFAULT NULL AFTER `discord_sent`',
    'SELECT 1'
  )
  FROM information_schema.columns
  WHERE table_schema = @db_name AND table_name = 'deals_ingest' AND column_name = 'discord_error'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl := (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE `deals_redirect` ADD COLUMN `discord_sent` tinyint(1) DEFAULT NULL AFTER `discord_description`',
    'SELECT 1'
  )
  FROM information_schema.columns
  WHERE table_schema = @db_name AND table_name = 'deals_redirect' AND column_name = 'discord_sent'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @ddl := (
  SELECT IF(
    COUNT(*) = 0,
    'ALTER TABLE `deals_redirect` ADD COLUMN `discord_error` text DEFAULT NULL AFTER `discord_sent`',
    'SELECT 1'
  )
  FROM information_schema.columns
  WHERE table_schema = @db_name AND table_name = 'deals_redirect' AND column_name = 'discord_error'
);
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
