from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import mysql.connector
except ImportError:  # pragma: no cover
    mysql = None  # type: ignore[assignment]

from common_utils.proxy_telemetry import snapshot_proxy_metrics


logger = logging.getLogger("common_utils.metering")


@dataclass(frozen=True, slots=True)
class TelemetryMysqlSettings:
    host: str
    port: int
    database: str
    user: str
    password: str


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def get_telemetry_mysql_settings() -> TelemetryMysqlSettings:
    return TelemetryMysqlSettings(
        host=_env("TELEMETRY_MYSQL_HOST", _env("MYSQL_HOST", "unified-mysql")),
        port=int(_env("TELEMETRY_MYSQL_PORT", _env("MYSQL_PORT", "3306")) or "3306"),
        database=_env("TELEMETRY_MYSQL_DATA", _env("MYSQL_DATA", "telemetry_db")),
        user=_env("TELEMETRY_MYSQL_USER", _env("MYSQL_USER")),
        password=_env("TELEMETRY_MYSQL_PASS", _env("MYSQL_PASS")),
    )


def open_telemetry_mysql_connection():
    if mysql is None:
        raise RuntimeError("mysql-connector-python is not installed")
    settings = get_telemetry_mysql_settings()
    return mysql.connector.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password,
        database=settings.database,
    )


def ensure_metering_table() -> bool:
    connection = None
    cursor = None
    try:
        connection = open_telemetry_mysql_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT 1 FROM job_run_metering LIMIT 1")
        cursor.fetchone()
        return True
    except Exception as exc:
        logger.warning("Unable to verify telemetry metering schema exists: %s", exc)
        return False
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _safe_read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def collect_system_metrics() -> dict[str, Any]:
    disk = shutil.disk_usage("/")
    mem_total_kb = None
    mem_available_kb = None
    meminfo = _safe_read("/proc/meminfo")
    if meminfo:
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_available_kb = int(line.split()[1])

    net_rx = 0
    net_tx = 0
    netdev = _safe_read("/proc/net/dev")
    if netdev:
        for line in netdev.splitlines()[2:]:
            parts = line.replace(":", " ").split()
            if len(parts) >= 10:
                net_rx += int(parts[1])
                net_tx += int(parts[9])

    uptime_seconds = None
    uptime = _safe_read("/proc/uptime")
    if uptime:
        uptime_seconds = int(float(uptime.split()[0]))

    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except (AttributeError, OSError):
        load_1m = load_5m = load_15m = None

    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "load_average_1m": load_1m,
        "load_average_5m": load_5m,
        "load_average_15m": load_15m,
        "memory_total_mb": round(mem_total_kb / 1024, 2) if mem_total_kb else None,
        "memory_available_mb": round(mem_available_kb / 1024, 2) if mem_available_kb else None,
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "disk_used_gb": round(disk.used / (1024**3), 2),
        "disk_free_gb": round(disk.free / (1024**3), 2),
        "network_rx_bytes": net_rx,
        "network_tx_bytes": net_tx,
        "uptime_seconds": uptime_seconds,
        "process_id": os.getpid(),
        "container_hostname": os.getenv("HOSTNAME"),
    }


class JobMeter:
    def __init__(
        self,
        job_name: str,
        service_name: str,
        trigger_source: str | None = None,
        parameters: dict[str, Any] | None = None,
        run_uuid: str | None = None,
    ) -> None:
        self.job_name = job_name
        self.service_name = service_name
        self.trigger_source = trigger_source or os.getenv("JOB_TRIGGER_SOURCE", "app")
        self.parameters = parameters or {}
        self.run_uuid = run_uuid or os.getenv("JOB_RUN_ID") or str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc)
        self.log_file_path = os.getenv("RUN_LOG_FILE", "")
        self._lock = threading.RLock()
        self._system_metrics: dict[str, Any] = {
            "start": collect_system_metrics(),
            "latest": None,
            "end": None,
        }

    def _execute(self, query: str, params: tuple[Any, ...]) -> None:
        connection = None
        cursor = None
        try:
            connection = open_telemetry_mysql_connection()
            cursor = connection.cursor()
            cursor.execute(query, params)
            connection.commit()
        except Exception as exc:
            logger.warning("Metering write failed run_uuid=%s error=%s", self.run_uuid, exc)
        finally:
            try:
                if cursor is not None:
                    cursor.close()
            except Exception:
                pass
            try:
                if connection is not None and connection.is_connected():
                    connection.close()
            except Exception:
                pass

    def start(self, step_name: str = "Initializing", detail_message: str | None = None) -> None:
        with self._lock:
            message_json = {"status": "RUNNING", "step": step_name, "progress": self.parameters}
            system_metrics_json = json.dumps(self._system_metrics, default=str)
            proxy_metrics_json = json.dumps(snapshot_proxy_metrics(), default=str)
        self._execute(
            """
            INSERT INTO job_run_metering (
                run_uuid, job_name, service_name, trigger_source, status, success_flag,
                step_name, started_at, log_file_path, parameters_json, message_json,
                system_metrics_json, proxy_metrics_json, detail_message
            ) VALUES (%s, %s, %s, %s, 'RUNNING', NULL, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                job_name = VALUES(job_name),
                service_name = VALUES(service_name),
                trigger_source = VALUES(trigger_source),
                status = VALUES(status),
                success_flag = VALUES(success_flag),
                step_name = VALUES(step_name),
                log_file_path = VALUES(log_file_path),
                parameters_json = VALUES(parameters_json),
                message_json = VALUES(message_json),
                system_metrics_json = VALUES(system_metrics_json),
                proxy_metrics_json = VALUES(proxy_metrics_json),
                detail_message = VALUES(detail_message)
            """,
            (
                self.run_uuid,
                self.job_name,
                self.service_name,
                self.trigger_source,
                step_name,
                self.started_at.replace(tzinfo=None),
                self.log_file_path or None,
                json.dumps(self.parameters, default=str),
                json.dumps(message_json, default=str),
                system_metrics_json,
                proxy_metrics_json,
                detail_message,
            ),
        )

    def update_progress(self, step_name: str, message_json: dict[str, Any], detail_message: str | None = None) -> None:
        with self._lock:
            self._system_metrics["latest"] = collect_system_metrics()
            system_metrics_json = json.dumps(self._system_metrics, default=str)
            proxy_metrics_json = json.dumps(snapshot_proxy_metrics(), default=str)
        self._execute(
            """
            UPDATE job_run_metering
            SET step_name = %s,
                message_json = %s,
                system_metrics_json = %s,
                proxy_metrics_json = %s,
                detail_message = %s
            WHERE run_uuid = %s
            """,
            (
                step_name,
                json.dumps(message_json, default=str),
                system_metrics_json,
                proxy_metrics_json,
                detail_message,
                self.run_uuid,
            ),
        )

    def heartbeat(self, step_name: str, detail_message: str | None = None, **progress: Any) -> None:
        self.update_progress(
            step_name=step_name,
            message_json={"status": "RUNNING", "step": step_name, "progress": progress},
            detail_message=detail_message,
        )

    def finish(
        self,
        status: str,
        success: bool,
        message_json: dict[str, Any],
        detail_message: str | None = None,
        error_lines: list[str] | None = None,
        exit_code: int | None = None,
    ) -> None:
        with self._lock:
            ended_at = datetime.now(timezone.utc)
            duration_seconds = int((ended_at - self.started_at).total_seconds())
            self._system_metrics["end"] = collect_system_metrics()
            system_metrics_json = json.dumps(self._system_metrics, default=str)
            proxy_metrics_json = json.dumps(snapshot_proxy_metrics(), default=str)
        self._execute(
            """
            UPDATE job_run_metering
            SET status = %s,
                success_flag = %s,
                ended_at = %s,
                duration_seconds = %s,
                exit_code = %s,
                message_json = %s,
                system_metrics_json = %s,
                proxy_metrics_json = %s,
                detail_message = %s,
                error_stack = %s
            WHERE run_uuid = %s
            """,
            (
                status,
                int(success),
                ended_at.replace(tzinfo=None),
                duration_seconds,
                exit_code,
                json.dumps(message_json, default=str),
                system_metrics_json,
                proxy_metrics_json,
                detail_message,
                "\n".join(error_lines or []),
                self.run_uuid,
            ),
        )


def shell_mark_failure(
    run_uuid: str,
    job_name: str,
    service_name: str,
    exit_code: int,
    detail_message: str,
) -> None:
    meter = JobMeter(
        job_name=job_name,
        service_name=service_name,
        trigger_source="shell",
        parameters={},
        run_uuid=run_uuid,
    )
    meter.finish(
        status="FAILED",
        success=False,
        message_json={
            "status": "FAILED",
            "source": "shell-wrapper",
            "exit_code": exit_code,
            "debug_tail": detail_message.splitlines(),
        },
        detail_message=detail_message,
        error_lines=detail_message.splitlines()[-10:],
        exit_code=exit_code,
    )


def shell_mark_success(
    run_uuid: str,
    job_name: str,
    service_name: str,
    detail_message: str | None = None,
) -> None:
    meter = JobMeter(
        job_name=job_name,
        service_name=service_name,
        trigger_source="shell",
        parameters={},
        run_uuid=run_uuid,
    )
    meter.finish(
        status="SUCCESS",
        success=True,
        message_json={"status": "SUCCESS", "source": "shell-wrapper"},
        detail_message=detail_message,
        exit_code=0,
    )
