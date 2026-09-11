from __future__ import annotations

import os
import urllib.parse
from datetime import datetime, timezone
from typing import Any


STAGE_COLORS: dict[str, int] = {
    "INGESTED": 0x3498DB,
    "REDIRECT_RESOLVED": 0x3498DB,
    "READY_FOR_REVIEW": 0xF1C40F,
    "PUBLISHED": 0x2ECC71,
    "FAILED": 0xE74C3C,
    "AI_STAGE_FAILED": 0xE74C3C,
    "PLAYWRIGHT_EXCEPTION": 0xE74C3C,
    "BLOCKED": 0xE67E22,
    "DELETED": 0x992D22,
}

STAGE_EMOJI: dict[str, str] = {
    "INGESTED": "\U0001F4E5",
    "REDIRECT_RESOLVED": "\U0001F517",
    "READY_FOR_REVIEW": "\U0001F9D0",
    "PUBLISHED": "\U0001F680",
    "FAILED": "\u274c",
    "AI_STAGE_FAILED": "\u274c",
    "PLAYWRIGHT_EXCEPTION": "\u274c",
    "BLOCKED": "\U0001F6D1",
    "DELETED": "\U0001F5D1",
}


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "-"


def _trim(value: str, max_len: int = 220) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def _add_field(fields: list[dict[str, Any]], name: str, value: Any, *, inline: bool = True) -> None:
    if value is None or value == "":
        return
    fields.append({"name": name, "value": _clean(value), "inline": inline})


def _grafana_base_url() -> str:
    return os.getenv("GRAFANA_BASE_URL", "").strip().rstrip("/")


def grafana_logs_url(
    *,
    repo: str | None = None,
    run_uuid: str | None = None,
    pipeline_run_id: str | None = None,
    content_id: str | None = None,
    asin: str | None = None,
    exception_uuid: str | None = None,
) -> str | None:
    base_url = _grafana_base_url()
    dashboard_uid = os.getenv("GRAFANA_LOGS_DASHBOARD_UID", "").strip()
    if not base_url or not dashboard_uid:
        return None

    search_terms = [run_uuid, pipeline_run_id, content_id, asin, exception_uuid]
    search = next((str(term).strip() for term in search_terms if str(term or "").strip()), "")
    query = {
        "var-host": os.getenv("COMMON_HOST_LABEL", ""),
        "var-repo": repo or ".*",
        "var-stream": ".*",
        "var-search": search,
    }
    return f"{base_url}/d/{urllib.parse.quote(dashboard_uid)}?{urllib.parse.urlencode(query)}"


def grafana_run_url(*, run_uuid: str | None = None, pipeline_run_id: str | None = None) -> str | None:
    base_url = _grafana_base_url()
    dashboard_uid = os.getenv("GRAFANA_RUN_DASHBOARD_UID", "").strip()
    if not base_url or not dashboard_uid:
        return None
    query = {
        "var-run_uuid": run_uuid or "",
        "var-pipeline_run_id": pipeline_run_id or "",
    }
    return f"{base_url}/d/{urllib.parse.quote(dashboard_uid)}?{urllib.parse.urlencode(query)}"


def build_grafana_fields(
    *,
    repo: str | None = None,
    run_uuid: str | None = None,
    pipeline_run_id: str | None = None,
    content_id: str | None = None,
    asin: str | None = None,
    exception_uuid: str | None = None,
) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    logs_url = grafana_logs_url(
        repo=repo,
        run_uuid=run_uuid,
        pipeline_run_id=pipeline_run_id,
        content_id=content_id,
        asin=asin,
        exception_uuid=exception_uuid,
    )
    run_url = grafana_run_url(run_uuid=run_uuid, pipeline_run_id=pipeline_run_id)
    _add_field(fields, "Grafana Logs", logs_url, inline=False)
    _add_field(fields, "Grafana Run", run_url, inline=False)
    return fields


def build_deal_progress_embed(
    *,
    stage: str,
    title: str,
    source_name: str,
    content_id: str,
    asin: str | None = None,
    status: str = "OK",
    summary: str | None = None,
    url: str | None = None,
    extra_fields: list[dict[str, Any]] | None = None,
    review_queue_id: int | str | None = None,
    candidate_id: int | str | None = None,
    enriched_offer_id: int | str | None = None,
    run_uuid: str | None = None,
    pipeline_run_id: str | None = None,
    job_name: str | None = None,
    step_name: str | None = None,
    scheduled_start: str | None = None,
    repo: str | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    repo_name = repo or source_name
    resolved_run_uuid = run_uuid or os.getenv("JOB_RUN_ID", "").strip() or None
    resolved_pipeline_run_id = pipeline_run_id or os.getenv("PIPELINE_RUN_ID", "").strip() or None
    fields: list[dict[str, Any]] = [
        {"name": "Source", "value": _clean(source_name), "inline": True},
        {"name": "Stage", "value": stage.replace("_", " ").title(), "inline": True},
        {"name": "Status", "value": _clean(status), "inline": True},
    ]
    _add_field(fields, "Run UUID", resolved_run_uuid)
    _add_field(fields, "Pipeline Run", resolved_pipeline_run_id)
    _add_field(fields, "Job", job_name)
    _add_field(fields, "Step", step_name)
    _add_field(fields, "Scheduled Start", scheduled_start)
    _add_field(fields, "Content ID", content_id)
    _add_field(fields, "ASIN", asin)
    _add_field(fields, "Deal #", review_queue_id)
    _add_field(fields, "Candidate ID", candidate_id)
    _add_field(fields, "Enriched Offer ID", enriched_offer_id)
    if extra_fields:
        fields.extend(extra_fields)
    fields.extend(
        build_grafana_fields(
            repo=repo_name,
            run_uuid=resolved_run_uuid,
            pipeline_run_id=resolved_pipeline_run_id,
            content_id=content_id,
            asin=asin,
        )
    )
    if summary:
        fields.append({"name": "Summary", "value": _trim(str(summary), 500), "inline": False})

    embed: dict[str, Any] = {
        "title": f"{STAGE_EMOJI.get(stage, '')} {stage.replace('_', ' ').title()}: {_clean(title)}".strip(),
        "color": STAGE_COLORS.get(stage, 0x95A5A6),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": fields[:25],
    }
    if url:
        embed["url"] = url
    env_name = environment or os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or ""
    footer_bits = [bit for bit in (env_name, repo_name, os.getenv("COMMON_HOST_LABEL", "")) if bit]
    if footer_bits:
        embed["footer"] = {"text": " | ".join(footer_bits)}
    return embed


def build_playwright_exception_embed(
    *,
    payload: dict[str, Any],
    payload_artifact_path: str | None = None,
    html_artifact_path: str | None = None,
    screenshot_artifact_path: str | None = None,
) -> dict[str, Any]:
    repo_name = payload.get("repo_name")
    exception_uuid = payload.get("exception_uuid")
    fields: list[dict[str, Any]] = [
        {"name": "Repo", "value": _clean(repo_name), "inline": True},
        {"name": "Job", "value": _clean(payload.get("job_name")), "inline": True},
        {"name": "Step", "value": _clean(payload.get("step_name")), "inline": True},
    ]
    _add_field(fields, "Run UUID", payload.get("run_uuid"))
    _add_field(fields, "Exception ID", exception_uuid)
    _add_field(fields, "Type", payload.get("exception_type") or payload.get("error_type"))
    _add_field(fields, "Item Key", payload.get("item_key"))
    _add_field(fields, "Item Value", payload.get("item_value"))
    _add_field(fields, "Scheduled Start", payload.get("sch_start"))
    _add_field(fields, "Target URL", payload.get("target_url"), inline=False)
    _add_field(fields, "Final URL", payload.get("final_url"), inline=False)
    _add_field(fields, "Proxy", payload.get("proxy_label"))
    _add_field(fields, "Payload", payload_artifact_path, inline=False)
    _add_field(fields, "HTML", html_artifact_path, inline=False)
    _add_field(fields, "Screenshot", screenshot_artifact_path, inline=False)
    fields.extend(
        build_grafana_fields(
            repo=repo_name,
            run_uuid=payload.get("run_uuid"),
            exception_uuid=exception_uuid,
        )
    )
    message = _trim(str(payload.get("exception_message") or payload.get("error_message") or ""), 700)
    if message:
        fields.append({"name": "Error", "value": message, "inline": False})

    return {
        "title": f"{STAGE_EMOJI['PLAYWRIGHT_EXCEPTION']} Playwright Exception: {_clean(exception_uuid)}",
        "color": STAGE_COLORS["PLAYWRIGHT_EXCEPTION"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": fields[:25],
    }
