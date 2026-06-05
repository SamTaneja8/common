from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

try:
    import mysql.connector
except ImportError:  # pragma: no cover
    mysql = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelemetryMysqlConfig:
    host: str
    port: int
    database: str
    user: str
    password: str


@dataclass(frozen=True, slots=True)
class ProxyAttemptEvent:
    repo_name: str
    job_name: str
    provider_name: str | None
    target_url: str | None
    success: bool
    deal_journey_id: str | None = None
    run_uuid: str | None = None
    target_domain: str | None = None
    http_status: int | None = None
    latency_ms: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JourneyEvent:
    deal_journey_id: str
    repo_name: str
    job_name: str
    event_type: str
    run_uuid: str | None = None
    source_name: str | None = None
    source_id: str | None = None
    asin: str | None = None
    marketplace: str | None = None
    entity_table: str | None = None
    entity_id: str | None = None
    status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def telemetry_config_from_env() -> TelemetryMysqlConfig:
    return TelemetryMysqlConfig(
        host=os.getenv("TELEMETRY_MYSQL_HOST") or os.getenv("MYSQL_HOST") or "unified-mysql",
        port=int(os.getenv("TELEMETRY_MYSQL_PORT") or os.getenv("MYSQL_PORT") or "3306"),
        database=os.getenv("TELEMETRY_MYSQL_DATA") or os.getenv("MYSQL_DATA") or "telemetry_db",
        user=os.getenv("TELEMETRY_MYSQL_USER") or os.getenv("MYSQL_USER") or "",
        password=os.getenv("TELEMETRY_MYSQL_PASS") or os.getenv("MYSQL_PASS") or "",
    )


def _connect(config: TelemetryMysqlConfig):
    if mysql is None:
        raise RuntimeError("mysql-connector-python is not installed")
    return mysql.connector.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        database=config.database,
    )


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, default=str, ensure_ascii=True)


def target_domain_from_url(target_url: str | None) -> str | None:
    if not target_url:
        return None
    try:
        return urlparse(target_url).netloc.lower() or None
    except Exception:
        return None


def record_proxy_attempt(event: ProxyAttemptEvent, config: TelemetryMysqlConfig | None = None) -> None:
    config = config or telemetry_config_from_env()
    try:
        connection = _connect(config)
    except Exception as exc:
        logger.warning("Unable to connect to telemetry DB for proxy attempt event: %s", exc)
        return
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO `proxy_attempt_events` (
                    deal_journey_id, run_uuid, repo_name, job_name, provider_name,
                    target_domain, target_url, success_flag, http_status, latency_ms,
                    error_type, error_message, metadata_json, attempted_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())
                """,
                (
                    event.deal_journey_id,
                    event.run_uuid or os.getenv("JOB_RUN_ID") or None,
                    event.repo_name,
                    event.job_name,
                    event.provider_name,
                    event.target_domain or target_domain_from_url(event.target_url),
                    event.target_url,
                    1 if event.success else 0,
                    event.http_status,
                    event.latency_ms,
                    event.error_type,
                    event.error_message,
                    _json(event.metadata),
                ),
            )
            connection.commit()
        except Exception as exc:
            logger.warning("Unable to record proxy attempt event: %s", exc)
    finally:
        connection.close()


def record_journey_event(event: JourneyEvent, config: TelemetryMysqlConfig | None = None) -> None:
    config = config or telemetry_config_from_env()
    try:
        connection = _connect(config)
    except Exception as exc:
        logger.warning("Unable to connect to telemetry DB for journey event: %s", exc)
        return
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO `pipeline_journey_events` (
                    deal_journey_id, run_uuid, repo_name, job_name, event_type,
                    source_name, source_id, asin, marketplace, entity_table,
                    entity_id, status, metadata_json, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())
                """,
                (
                    event.deal_journey_id,
                    event.run_uuid or os.getenv("JOB_RUN_ID") or None,
                    event.repo_name,
                    event.job_name,
                    event.event_type,
                    event.source_name,
                    event.source_id,
                    event.asin,
                    event.marketplace,
                    event.entity_table,
                    event.entity_id,
                    event.status,
                    _json(event.metadata),
                ),
            )
            connection.commit()
        except Exception as exc:
            logger.warning("Unable to record journey event: %s", exc)
    finally:
        connection.close()


class ProxyAttemptTimer:
    def __init__(
        self,
        *,
        repo_name: str,
        job_name: str,
        provider_name: str | None,
        target_url: str | None,
        deal_journey_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.repo_name = repo_name
        self.job_name = job_name
        self.provider_name = provider_name
        self.target_url = target_url
        self.deal_journey_id = deal_journey_id
        self.metadata = metadata or {}
        self.started = time.monotonic()

    def finish(
        self,
        *,
        success: bool,
        http_status: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        record_proxy_attempt(
            ProxyAttemptEvent(
                repo_name=self.repo_name,
                job_name=self.job_name,
                provider_name=self.provider_name,
                target_url=self.target_url,
                deal_journey_id=self.deal_journey_id,
                success=success,
                http_status=http_status,
                latency_ms=int((time.monotonic() - self.started) * 1000),
                error_type=error_type,
                error_message=error_message,
                metadata={**self.metadata, "recorded_at_utc": datetime.now(timezone.utc).isoformat()},
            )
        )
