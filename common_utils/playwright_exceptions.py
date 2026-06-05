"""Shared Playwright exception capture helpers for sibling scraper repos."""

from __future__ import annotations

import json
import hashlib
import inspect
import os
import re
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib import request

try:
    import mysql.connector
except ImportError:  # pragma: no cover
    mysql = None  # type: ignore[assignment]


SCHEDULED_SLOT_FORMAT = "%y%m%d-%H"
SCHEDULED_AT_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sanitize_filename_fragment(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return "event"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-") or "event"


def _safe_json(data: Any) -> str:
    return json.dumps(data, default=str, ensure_ascii=True)


def _coerce_location(location: Any) -> dict[str, Any]:
    if isinstance(location, dict):
        return dict(location)
    return {}


def _extract_code_context(exception: BaseException, *, context_lines: int = 5) -> dict[str, Any]:
    tb = exception.__traceback__
    if tb is None:
        return {}
    while tb.tb_next is not None:
        tb = tb.tb_next

    frame = tb.tb_frame
    frame_info = inspect.getframeinfo(frame, context=context_lines * 2 + 1)
    code_lines = frame_info.code_context or []
    start_line = (frame_info.lineno or 0) - (frame_info.index or 0)
    source = "".join(code_lines)
    return {
        "source_file": frame_info.filename,
        "source_line": frame_info.lineno,
        "function_name": frame_info.function,
        "code_context": [
            {
                "line_no": start_line + index,
                "text": line.rstrip("\n"),
                "is_exception_line": (start_line + index) == frame_info.lineno,
            }
            for index, line in enumerate(code_lines)
        ],
        "code_hash": hashlib.sha256(source.encode("utf-8")).hexdigest() if source else None,
    }


def resolve_scheduled_slot(
    *,
    explicit_slot: str | None = None,
    explicit_started_at: str | None = None,
    run_started_code: str | None = None,
) -> str | None:
    raw_slot = str(explicit_slot or os.getenv("RUN_SCHEDULED_SLOT_UTC", "")).strip()
    if raw_slot:
        try:
            datetime.strptime(raw_slot, SCHEDULED_SLOT_FORMAT)
            return raw_slot
        except ValueError:
            pass

    raw_started_at = str(explicit_started_at or os.getenv("RUN_SCHEDULED_AT_UTC", "")).strip()
    if raw_started_at:
        try:
            scheduled_start = datetime.strptime(raw_started_at, SCHEDULED_AT_FORMAT).replace(
                minute=0,
                second=0,
                microsecond=0,
            )
            return scheduled_start.strftime(SCHEDULED_SLOT_FORMAT)
        except ValueError:
            pass

    if run_started_code:
        try:
            scheduled_start = datetime.strptime(run_started_code, "%y%m%d-%H%M%S").replace(
                minute=0,
                second=0,
                microsecond=0,
            )
            return scheduled_start.strftime(SCHEDULED_SLOT_FORMAT)
        except ValueError:
            pass

    return None


@dataclass(frozen=True, slots=True)
class TelemetryMysqlConfig:
    host: str
    port: int
    database: str
    user: str
    password: str


@dataclass(frozen=True, slots=True)
class PlaywrightExceptionContext:
    repo_name: str
    job_name: str
    step_name: str
    service_name: str | None = None
    run_uuid: str | None = None
    sch_start: str | None = None
    item_key: str | None = None
    item_value: str | None = None
    target_url: str | None = None
    final_url: str | None = None
    page_title: str | None = None
    http_status: int | None = None
    proxy_provider: str | None = None
    proxy_endpoint: str | None = None
    proxy_session_id: str | None = None
    log_file_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PersistedPlaywrightException:
    exception_uuid: str
    payload_artifact_path: str | None
    html_artifact_path: str | None
    screenshot_artifact_path: str | None
    discord_sent: bool


class PlaywrightEventBuffer:
    def __init__(self, *, max_console_entries: int = 100, max_page_errors: int = 50, max_request_failures: int = 50) -> None:
        self.max_console_entries = max_console_entries
        self.max_page_errors = max_page_errors
        self.max_request_failures = max_request_failures
        self.console_logs: list[dict[str, Any]] = []
        self.page_errors: list[dict[str, Any]] = []
        self.request_failures: list[dict[str, Any]] = []

    def _append(self, bucket: list[dict[str, Any]], entry: dict[str, Any], limit: int) -> None:
        bucket.append(entry)
        if len(bucket) > limit:
            del bucket[0 : len(bucket) - limit]

    def _on_console(self, message: Any) -> None:
        try:
            location = _coerce_location(message.location)
        except Exception:
            location = {}
        self._append(
            self.console_logs,
            {
                "captured_at_utc": _utc_now().isoformat(),
                "type": getattr(message, "type", None),
                "text": message.text if hasattr(message, "text") else str(message),
                "location": location,
            },
            self.max_console_entries,
        )

    def _on_page_error(self, error: Any) -> None:
        self._append(
            self.page_errors,
            {
                "captured_at_utc": _utc_now().isoformat(),
                "message": str(error),
            },
            self.max_page_errors,
        )

    def _on_request_failed(self, request_obj: Any) -> None:
        failure = None
        try:
            failure = request_obj.failure
        except Exception:
            failure = None
        self._append(
            self.request_failures,
            {
                "captured_at_utc": _utc_now().isoformat(),
                "url": getattr(request_obj, "url", None),
                "method": getattr(request_obj, "method", None),
                "resource_type": getattr(request_obj, "resource_type", None),
                "failure": failure,
            },
            self.max_request_failures,
        )

    def attach_sync_page(self, page: Any) -> None:
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("requestfailed", self._on_request_failed)

    def attach_async_page(self, page: Any) -> None:
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("requestfailed", self._on_request_failed)


class PlaywrightExceptionPipeline:
    def __init__(
        self,
        *,
        telemetry_mysql: TelemetryMysqlConfig,
        artifacts_root: Path,
        discord_webhook_url: str | None,
        max_html_snippet_chars: int = 12000,
        retention_days: int = 7,
    ) -> None:
        self.telemetry_mysql = telemetry_mysql
        self.artifacts_root = artifacts_root
        self.discord_webhook_url = (discord_webhook_url or "").strip() or None
        self.max_html_snippet_chars = max(max_html_snippet_chars, 1000)
        self.retention_days = max(retention_days, 0)
        self._cleanup_done = False

    def new_event_buffer(self) -> PlaywrightEventBuffer:
        return PlaywrightEventBuffer()

    def capture_sync_exception(
        self,
        *,
        context: PlaywrightExceptionContext,
        exception: BaseException,
        event_buffer: PlaywrightEventBuffer | None,
        page: Any | None = None,
    ) -> PersistedPlaywrightException:
        try:
            page_title = context.page_title or _safe_sync_page_title(page)
            final_url = context.final_url or _safe_sync_page_url(page)
            html = _safe_sync_page_content(page)
            screenshot_path = self._safe_sync_screenshot(page, context=context)
            return self._persist_exception(
                context=context,
                exception=exception,
                event_buffer=event_buffer,
                page_title=page_title,
                final_url=final_url,
                html=html,
                screenshot_artifact_path=screenshot_path,
            )
        except Exception:
            return PersistedPlaywrightException(
                exception_uuid="",
                payload_artifact_path=None,
                html_artifact_path=None,
                screenshot_artifact_path=None,
                discord_sent=False,
            )

    async def capture_async_exception(
        self,
        *,
        context: PlaywrightExceptionContext,
        exception: BaseException,
        event_buffer: PlaywrightEventBuffer | None,
        page: Any | None = None,
    ) -> PersistedPlaywrightException:
        try:
            page_title = context.page_title or await _safe_async_page_title(page)
            final_url = context.final_url or await _safe_async_page_url(page)
            html = await _safe_async_page_content(page)
            screenshot_path = await self._safe_async_screenshot(page, context=context)
            return self._persist_exception(
                context=context,
                exception=exception,
                event_buffer=event_buffer,
                page_title=page_title,
                final_url=final_url,
                html=html,
                screenshot_artifact_path=screenshot_path,
            )
        except Exception:
            return PersistedPlaywrightException(
                exception_uuid="",
                payload_artifact_path=None,
                html_artifact_path=None,
                screenshot_artifact_path=None,
                discord_sent=False,
            )

    def _persist_exception(
        self,
        *,
        context: PlaywrightExceptionContext,
        exception: BaseException,
        event_buffer: PlaywrightEventBuffer | None,
        page_title: str | None,
        final_url: str | None,
        html: str | None,
        screenshot_artifact_path: str | None,
    ) -> PersistedPlaywrightException:
        created_at = _utc_now()
        exception_uuid = uuid.uuid4().hex
        sch_start = context.sch_start or resolve_scheduled_slot()
        traceback_text = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        html_snippet = (html or "")[: self.max_html_snippet_chars] or None
        code_context = _extract_code_context(exception)
        payload = {
            "exception_uuid": exception_uuid,
            "created_at_utc": created_at.isoformat(),
            "repo_name": context.repo_name,
            "job_name": context.job_name,
            "step_name": context.step_name,
            "service_name": context.service_name or context.repo_name,
            "run_uuid": context.run_uuid or os.getenv("JOB_RUN_ID", "") or None,
            "sch_start": sch_start,
            "item_key": context.item_key,
            "item_value": context.item_value,
            "target_url": context.target_url,
            "final_url": final_url,
            "page_title": page_title,
            "http_status": context.http_status,
            "error_type": type(exception).__name__,
            "error_message": str(exception),
            "python_traceback": traceback_text,
            "source_file": code_context.get("source_file"),
            "source_line": code_context.get("source_line"),
            "function_name": code_context.get("function_name"),
            "code_context": code_context.get("code_context") or [],
            "code_hash": code_context.get("code_hash"),
            "console_logs": event_buffer.console_logs if event_buffer else [],
            "page_errors": event_buffer.page_errors if event_buffer else [],
            "request_failures": event_buffer.request_failures if event_buffer else [],
            "html_snippet": html_snippet,
            "proxy_provider": context.proxy_provider,
            "proxy_endpoint": context.proxy_endpoint,
            "proxy_session_id": context.proxy_session_id,
            "metadata": context.metadata,
        }
        payload_artifact_path, html_artifact_path = self._write_artifacts(
            payload=payload,
            html=html,
            repo_name=context.repo_name,
            job_name=context.job_name,
            step_name=context.step_name,
            item_value=context.item_value,
            exception_uuid=exception_uuid,
            created_at=created_at,
        )
        discord_sent = self._insert_and_alert(
            payload=payload,
            payload_artifact_path=payload_artifact_path,
            html_artifact_path=html_artifact_path,
            screenshot_artifact_path=screenshot_artifact_path,
        )
        return PersistedPlaywrightException(
            exception_uuid=exception_uuid,
            payload_artifact_path=payload_artifact_path,
            html_artifact_path=html_artifact_path,
            screenshot_artifact_path=screenshot_artifact_path,
            discord_sent=discord_sent,
        )

    def _write_artifacts(
        self,
        *,
        payload: dict[str, Any],
        html: str | None,
        repo_name: str,
        job_name: str,
        step_name: str,
        item_value: str | None,
        exception_uuid: str,
        created_at: datetime,
    ) -> tuple[str | None, str | None]:
        self._cleanup_old_artifacts()
        artifact_dir = self.artifacts_root / created_at.strftime("%Y%m%d")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        base_name = "-".join(
            [
                created_at.strftime("%Y%m%d-%H%M%S"),
                _sanitize_filename_fragment(repo_name),
                _sanitize_filename_fragment(job_name),
                _sanitize_filename_fragment(step_name),
                _sanitize_filename_fragment(item_value),
                exception_uuid[:8],
            ]
        )
        payload_path = artifact_dir / f"{base_name}.json"
        payload_path.write_text(_safe_json(payload), encoding="utf-8")

        html_path: Path | None = None
        if html:
            html_path = artifact_dir / f"{base_name}.html"
            html_path.write_text(html, encoding="utf-8")

        return str(payload_path), str(html_path) if html_path else None

    def _cleanup_old_artifacts(self) -> None:
        if self._cleanup_done:
            return
        self._cleanup_done = True
        if not self.artifacts_root.exists():
            return

        cutoff_ts = (_utc_now().timestamp()) - (self.retention_days * 86400)
        for path in sorted(self.artifacts_root.rglob("*"), reverse=True):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff_ts:
                    path.unlink()
                    continue
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                continue

    def _insert_and_alert(
        self,
        *,
        payload: dict[str, Any],
        payload_artifact_path: str | None,
        html_artifact_path: str | None,
        screenshot_artifact_path: str | None,
    ) -> bool:
        sql = """
            INSERT INTO `playwright_exception_events` (
                exception_uuid, repo_name, job_name, step_name, service_name, run_uuid, sch_start,
                item_key, item_value, target_url, final_url, page_title, http_status, error_type,
                error_message, python_traceback, source_file, source_line, function_name, code_context_json, code_hash,
                console_logs_json, page_errors_json,
                request_failures_json, html_snippet, html_artifact_path, screenshot_artifact_path,
                payload_artifact_path, proxy_provider, proxy_endpoint, proxy_session_id,
                metadata_json, discord_exception_sent
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        legacy_sql = """
            INSERT INTO `playwright_exception_events` (
                exception_uuid, repo_name, job_name, step_name, service_name, run_uuid, sch_start,
                item_key, item_value, target_url, final_url, page_title, http_status, error_type,
                error_message, python_traceback, console_logs_json, page_errors_json,
                request_failures_json, html_snippet, html_artifact_path, screenshot_artifact_path,
                payload_artifact_path, proxy_provider, proxy_endpoint, proxy_session_id,
                metadata_json, discord_exception_sent
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        discord_sent = False
        if self.discord_webhook_url:
            discord_sent = self._send_discord_alert(payload, payload_artifact_path, html_artifact_path, screenshot_artifact_path)
        if mysql is None:
            return discord_sent
        connection = mysql.connector.connect(
            host=self.telemetry_mysql.host,
            port=self.telemetry_mysql.port,
            user=self.telemetry_mysql.user,
            password=self.telemetry_mysql.password,
            database=self.telemetry_mysql.database,
        )
        try:
            cursor = connection.cursor()
            legacy_params = (
                payload["exception_uuid"],
                payload["repo_name"],
                payload["job_name"],
                payload["step_name"],
                payload["service_name"],
                payload["run_uuid"],
                payload["sch_start"],
                payload["item_key"],
                payload["item_value"],
                payload["target_url"],
                payload["final_url"],
                payload["page_title"],
                payload["http_status"],
                payload["error_type"],
                payload["error_message"],
                payload["python_traceback"],
                _safe_json(payload["console_logs"]),
                _safe_json(payload["page_errors"]),
                _safe_json(payload["request_failures"]),
                payload["html_snippet"],
                html_artifact_path,
                screenshot_artifact_path,
                payload_artifact_path,
                payload["proxy_provider"],
                payload["proxy_endpoint"],
                payload["proxy_session_id"],
                _safe_json(payload["metadata"]),
                1 if discord_sent else 0,
            )
            try:
                cursor.execute(
                    sql,
                    (
                        payload["exception_uuid"],
                        payload["repo_name"],
                        payload["job_name"],
                        payload["step_name"],
                        payload["service_name"],
                        payload["run_uuid"],
                        payload["sch_start"],
                        payload["item_key"],
                        payload["item_value"],
                        payload["target_url"],
                        payload["final_url"],
                        payload["page_title"],
                        payload["http_status"],
                        payload["error_type"],
                        payload["error_message"],
                        payload["python_traceback"],
                        payload["source_file"],
                        payload["source_line"],
                        payload["function_name"],
                        _safe_json(payload["code_context"]),
                        payload["code_hash"],
                        _safe_json(payload["console_logs"]),
                        _safe_json(payload["page_errors"]),
                        _safe_json(payload["request_failures"]),
                        payload["html_snippet"],
                        html_artifact_path,
                        screenshot_artifact_path,
                        payload_artifact_path,
                        payload["proxy_provider"],
                        payload["proxy_endpoint"],
                        payload["proxy_session_id"],
                        _safe_json(payload["metadata"]),
                        1 if discord_sent else 0,
                    ),
                )
            except Exception as exc:
                if getattr(exc, "errno", None) not in {1054, 1136}:
                    raise
                cursor.execute(legacy_sql, legacy_params)
            connection.commit()
        finally:
            connection.close()
        return discord_sent

    def _send_discord_alert(
        self,
        payload: dict[str, Any],
        payload_artifact_path: str | None,
        html_artifact_path: str | None,
        screenshot_artifact_path: str | None,
    ) -> bool:
        if not self.discord_webhook_url:
            return False

        lines = [
            f"**Playwright Exception: {payload['repo_name']} / {payload['step_name']}**",
            f"Job: {payload['job_name']}",
            f"Type: {payload['error_type']}",
            f"Error: {payload['error_message']}",
        ]
        if payload.get("item_key") and payload.get("item_value"):
            lines.append(f"{payload['item_key']}: {payload['item_value']}")
        if payload.get("sch_start"):
            lines.append(f"Scheduled slot: {payload['sch_start']}")
        if payload.get("target_url"):
            lines.append(f"Target: {payload['target_url']}")
        if payload.get("final_url"):
            lines.append(f"Final: {payload['final_url']}")
        if payload.get("proxy_provider"):
            lines.append(f"Proxy: {payload['proxy_provider']}")
        if payload_artifact_path:
            lines.append(f"Payload: {payload_artifact_path}")
        if html_artifact_path:
            lines.append(f"HTML: {html_artifact_path}")
        if screenshot_artifact_path:
            lines.append(f"Screenshot: {screenshot_artifact_path}")
        lines.append(f"Exception ID: {payload['exception_uuid']}")

        content = "\n".join(lines)[:1900]
        webhook_request = request.Request(
            self.discord_webhook_url,
            data=json.dumps({"content": content}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(webhook_request, timeout=10):
            pass
        return True

    def _safe_sync_screenshot(self, page: Any | None, *, context: PlaywrightExceptionContext) -> str | None:
        if page is None:
            return None
        try:
            if page.is_closed():
                return None
        except Exception:
            return None

        screenshot_dir = self.artifacts_root / _utc_now().strftime("%Y%m%d")
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / (
            f"{_utc_now().strftime('%Y%m%d-%H%M%S')}-"
            f"{_sanitize_filename_fragment(context.repo_name)}-"
            f"{_sanitize_filename_fragment(context.step_name)}-"
            f"{_sanitize_filename_fragment(context.item_value)}.png"
        )
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
            return str(screenshot_path)
        except Exception:
            return None

    async def _safe_async_screenshot(self, page: Any | None, *, context: PlaywrightExceptionContext) -> str | None:
        if page is None:
            return None
        try:
            if page.is_closed():
                return None
        except Exception:
            return None

        screenshot_dir = self.artifacts_root / _utc_now().strftime("%Y%m%d")
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / (
            f"{_utc_now().strftime('%Y%m%d-%H%M%S')}-"
            f"{_sanitize_filename_fragment(context.repo_name)}-"
            f"{_sanitize_filename_fragment(context.step_name)}-"
            f"{_sanitize_filename_fragment(context.item_value)}.png"
        )
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
            return str(screenshot_path)
        except Exception:
            return None


def _safe_sync_page_title(page: Any | None) -> str | None:
    if page is None:
        return None
    try:
        return page.title()
    except Exception:
        return None


def _safe_sync_page_url(page: Any | None) -> str | None:
    if page is None:
        return None
    try:
        return page.url
    except Exception:
        return None


def _safe_sync_page_content(page: Any | None) -> str | None:
    if page is None:
        return None
    try:
        return page.content()
    except Exception:
        return None


async def _safe_async_page_title(page: Any | None) -> str | None:
    if page is None:
        return None
    try:
        return await page.title()
    except Exception:
        return None


async def _safe_async_page_url(page: Any | None) -> str | None:
    if page is None:
        return None
    try:
        return page.url
    except Exception:
        return None


async def _safe_async_page_content(page: Any | None) -> str | None:
    if page is None:
        return None
    try:
        return await page.content()
    except Exception:
        return None
