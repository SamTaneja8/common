"""API provider authorization and balance checks (OpenAI, HuggingFace,
DeepSeek, Gemini), logged and persisted to telemetry_db.

Ported from the retired vpsmonitor repo's app/collectors/api_balances.py.
Unlike the proxy checks (common_utils.proxy_balance_check, log-only), results
here are also written to the `api_balance_check` table in telemetry_db --
see common/observability/sql/api_balance_check.sql -- so balance trends over
time are queryable, per explicit decision when vpsmonitor's functionality
was migrated into common.

Usage: python -m common_utils.api_balance_check
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error, parse, request

from common_utils.metering import open_telemetry_mysql_connection
from common_utils.proxy_settings import load_shared_proxy_env


logger = logging.getLogger("common_utils.api_balance_check")

OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
OPENAI_SUBSCRIPTION_URL = "https://api.openai.com/dashboard/billing/subscription"
OPENAI_USAGE_URL = "https://api.openai.com/dashboard/billing/usage"
HUGGINGFACE_WHOAMI_URL = "https://huggingface.co/api/whoami-v2"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _get_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


@dataclass(frozen=True)
class ApiBalanceSettings:
    openai_api_key: str
    openai_balance_api_url: str
    openai_balance_auth_header: str
    openai_balance_auth_value: str
    openai_balance_alert_usd: float
    huggingface_token: str
    huggingface_balance_api_url: str
    huggingface_balance_auth_header: str
    huggingface_balance_auth_value: str
    huggingface_balance_alert_usd: float
    deepseek_token: str
    deepseek_balance_alert_usd: float
    gemini_api_key: str
    gemini_balance_api_url: str
    gemini_balance_auth_header: str
    gemini_balance_auth_value: str
    gemini_balance_alert_usd: float


def load_settings() -> ApiBalanceSettings:
    load_shared_proxy_env()
    return ApiBalanceSettings(
        openai_api_key=_get_str("OPENAI_API_KEY"),
        openai_balance_api_url=_get_str("OPENAI_BALANCE_API_URL"),
        openai_balance_auth_header=_get_str("OPENAI_BALANCE_AUTH_HEADER", "Authorization"),
        openai_balance_auth_value=_get_str("OPENAI_BALANCE_AUTH_VALUE"),
        openai_balance_alert_usd=_get_float("OPENAI_BALANCE_ALERT_USD", 5.0),
        huggingface_token=_get_str("HF_TOKEN", _get_str("HUGGINGFACE_TOKEN")),
        huggingface_balance_api_url=_get_str("HUGGINGFACE_BALANCE_API_URL"),
        huggingface_balance_auth_header=_get_str("HUGGINGFACE_BALANCE_AUTH_HEADER", "Authorization"),
        huggingface_balance_auth_value=_get_str("HUGGINGFACE_BALANCE_AUTH_VALUE"),
        huggingface_balance_alert_usd=_get_float("HUGGINGFACE_BALANCE_ALERT_USD", 5.0),
        deepseek_token=_get_str("DEEPSEEK_TOKEN"),
        deepseek_balance_alert_usd=_get_float("DEEPSEEK_BALANCE_ALERT_USD", 5.0),
        gemini_api_key=_get_str("GEMINI_API_KEY", _get_str("GOOGLE_API_KEY")),
        gemini_balance_api_url=_get_str("GEMINI_BALANCE_API_URL"),
        gemini_balance_auth_header=_get_str("GEMINI_BALANCE_AUTH_HEADER", "Authorization"),
        gemini_balance_auth_value=_get_str("GEMINI_BALANCE_AUTH_VALUE"),
        gemini_balance_alert_usd=_get_float("GEMINI_BALANCE_ALERT_USD", 5.0),
    )


@dataclass(frozen=True)
class ApiCheckResult:
    provider: str
    configured: bool
    auth_ok: bool | None
    auth_status_code: int | None
    auth_message: str
    balance_usd: float | None
    balance_message: str
    alert_threshold_usd: float | None


def _request_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    req = request.Request(url, headers=headers or {})
    with request.urlopen(req, timeout=20) as response:
        return getattr(response, "status", 200), json.loads(response.read().decode("utf-8"))


def _auth_check(url: str, headers: dict[str, str] | None = None) -> tuple[bool | None, int | None, str]:
    try:
        status_code, _payload = _request_json(url, headers=headers)
        return True, status_code, "authorization passed"
    except error.HTTPError as exc:
        if exc.code in {401, 403, 407}:
            return False, exc.code, f"authorization failed with HTTP {exc.code}"
        return None, exc.code, f"request returned HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, None, f"request error: {exc}"


def _find_balance(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in (
            "balance",
            "credits",
            "credit",
            "total_balance",
            "available_balance",
            "availableBalance",
            "remaining_balance",
            "remainingBalance",
        ):
            raw = value.get(key)
            if isinstance(raw, (int, float)):
                return float(raw)
            if isinstance(raw, str):
                try:
                    return float(raw)
                except ValueError:
                    pass
        for nested in value.values():
            found = _find_balance(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_balance(nested)
            if found is not None:
                return found
    return None


def _custom_balance_check(*, api_url: str, auth_header: str, auth_value: str) -> tuple[float | None, str]:
    if not api_url:
        return None, "balance check skipped: no balance API URL configured"
    headers: dict[str, str] = {}
    if auth_header and auth_value:
        headers[auth_header] = auth_value
    try:
        _status_code, payload = _request_json(api_url, headers=headers)
        balance = _find_balance(payload)
        if balance is None:
            return None, "balance check failed: no numeric balance found in response"
        return balance, f"balance=${balance:.2f}"
    except Exception as exc:  # noqa: BLE001
        return None, f"balance check failed: {exc}"


def _openai_balance_check(settings: ApiBalanceSettings) -> tuple[float | None, str]:
    if not settings.openai_api_key:
        return None, "balance check skipped: OPENAI_API_KEY is not configured"

    if settings.openai_balance_api_url:
        return _custom_balance_check(
            api_url=settings.openai_balance_api_url,
            auth_header=settings.openai_balance_auth_header,
            auth_value=settings.openai_balance_auth_value or f"Bearer {settings.openai_api_key}",
        )

    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    try:
        _status_code, subscription_payload = _request_json(OPENAI_SUBSCRIPTION_URL, headers=headers)
        hard_limit = subscription_payload.get("hard_limit_usd")
        if hard_limit is None:
            return None, "balance check failed: hard_limit_usd missing from OpenAI subscription response"

        now = datetime.now(timezone.utc)
        start_date = now.replace(day=1).date().isoformat()
        end_date = now.date().isoformat()
        usage_url = f"{OPENAI_USAGE_URL}?start_date={start_date}&end_date={end_date}"
        _status_code, usage_payload = _request_json(usage_url, headers=headers)
        total_usage = usage_payload.get("total_usage")
        if total_usage is None:
            return None, "balance check failed: total_usage missing from OpenAI usage response"

        usage_usd = float(total_usage) / 100.0
        remaining_usd = float(hard_limit) - usage_usd
        return (
            remaining_usd,
            f"balance=${remaining_usd:.2f} (hard_limit=${float(hard_limit):.2f}, usage=${usage_usd:.2f}, period={start_date}..{end_date})",
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"balance check failed: {exc}"


def _deepseek_balance_check(settings: ApiBalanceSettings) -> tuple[float | None, str]:
    if not settings.deepseek_token:
        return None, "balance check skipped: DEEPSEEK_TOKEN is not configured"
    try:
        _status_code, payload = _request_json(
            DEEPSEEK_BALANCE_URL,
            headers={"Authorization": f"Bearer {settings.deepseek_token}"},
        )
        balance = _find_balance(payload)
        if balance is None and isinstance(payload, dict):
            balances = payload.get("balance_infos") or payload.get("balances")
            if isinstance(balances, list):
                total = 0.0
                found_any = False
                for item in balances:
                    found = _find_balance(item)
                    if found is not None:
                        total += found
                        found_any = True
                if found_any:
                    balance = total
        if balance is None:
            return None, "balance check failed: no numeric balance found in DeepSeek response"
        return balance, f"balance=${balance:.2f}"
    except Exception as exc:  # noqa: BLE001
        return None, f"balance check failed: {exc}"


def collect_api_health(settings: ApiBalanceSettings) -> list[ApiCheckResult]:
    results: list[ApiCheckResult] = []

    openai_configured = bool(settings.openai_api_key)
    if openai_configured:
        openai_headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
        auth_ok, auth_status_code, auth_message = _auth_check(OPENAI_MODELS_URL, openai_headers)
        balance_usd, balance_message = _openai_balance_check(settings)
    else:
        auth_ok, auth_status_code, auth_message = None, None, "authorization check skipped: OPENAI_API_KEY is not configured"
        balance_usd, balance_message = None, "balance check skipped: OPENAI_API_KEY is not configured"
    results.append(
        ApiCheckResult(
            provider="ChatGPT/OpenAI",
            configured=openai_configured,
            auth_ok=auth_ok,
            auth_status_code=auth_status_code,
            auth_message=auth_message,
            balance_usd=balance_usd,
            balance_message=balance_message,
            alert_threshold_usd=settings.openai_balance_alert_usd,
        )
    )

    hf_configured = bool(settings.huggingface_token)
    if hf_configured:
        hf_headers = {"Authorization": f"Bearer {settings.huggingface_token}"}
        auth_ok, auth_status_code, auth_message = _auth_check(HUGGINGFACE_WHOAMI_URL, hf_headers)
        balance_usd, balance_message = _custom_balance_check(
            api_url=settings.huggingface_balance_api_url,
            auth_header=settings.huggingface_balance_auth_header,
            auth_value=settings.huggingface_balance_auth_value or f"Bearer {settings.huggingface_token}",
        )
    else:
        auth_ok, auth_status_code, auth_message = None, None, "authorization check skipped: HF_TOKEN is not configured"
        balance_usd, balance_message = None, "balance check skipped: HF_TOKEN is not configured"
    results.append(
        ApiCheckResult(
            provider="HuggingFace",
            configured=hf_configured,
            auth_ok=auth_ok,
            auth_status_code=auth_status_code,
            auth_message=auth_message,
            balance_usd=balance_usd,
            balance_message=balance_message,
            alert_threshold_usd=settings.huggingface_balance_alert_usd,
        )
    )

    deepseek_configured = bool(settings.deepseek_token)
    if deepseek_configured:
        deepseek_headers = {"Authorization": f"Bearer {settings.deepseek_token}"}
        auth_ok, auth_status_code, auth_message = _auth_check(DEEPSEEK_BALANCE_URL, deepseek_headers)
        balance_usd, balance_message = _deepseek_balance_check(settings)
    else:
        auth_ok, auth_status_code, auth_message = None, None, "authorization check skipped: DEEPSEEK_TOKEN is not configured"
        balance_usd, balance_message = None, "balance check skipped: DEEPSEEK_TOKEN is not configured"
    results.append(
        ApiCheckResult(
            provider="DeepSeek",
            configured=deepseek_configured,
            auth_ok=auth_ok,
            auth_status_code=auth_status_code,
            auth_message=auth_message,
            balance_usd=balance_usd,
            balance_message=balance_message,
            alert_threshold_usd=settings.deepseek_balance_alert_usd,
        )
    )

    gemini_configured = bool(settings.gemini_api_key)
    if gemini_configured:
        gemini_url = f"{GEMINI_MODELS_URL}?key={parse.quote(settings.gemini_api_key, safe='')}"
        auth_ok, auth_status_code, auth_message = _auth_check(gemini_url)
        balance_usd, balance_message = _custom_balance_check(
            api_url=settings.gemini_balance_api_url,
            auth_header=settings.gemini_balance_auth_header,
            auth_value=settings.gemini_balance_auth_value,
        )
    else:
        auth_ok, auth_status_code, auth_message = None, None, "authorization check skipped: GEMINI_API_KEY is not configured"
        balance_usd, balance_message = None, "balance check skipped: GEMINI_API_KEY is not configured"
    results.append(
        ApiCheckResult(
            provider="Gemini",
            configured=gemini_configured,
            auth_ok=auth_ok,
            auth_status_code=auth_status_code,
            auth_message=auth_message,
            balance_usd=balance_usd,
            balance_message=balance_message,
            alert_threshold_usd=settings.gemini_balance_alert_usd,
        )
    )

    return results


def _record_result(result: ApiCheckResult) -> None:
    connection = None
    cursor = None
    try:
        connection = open_telemetry_mysql_connection()
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO api_balance_check (
                checked_at, provider, configured, auth_ok, auth_status_code,
                auth_message, balance_usd, balance_message, alert_threshold_usd
            ) VALUES (UTC_TIMESTAMP(), %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                result.provider,
                result.configured,
                result.auth_ok,
                result.auth_status_code,
                result.auth_message,
                result.balance_usd,
                result.balance_message,
                result.alert_threshold_usd,
            ),
        )
        connection.commit()
    except Exception as exc:
        logger.warning("api_balance_check write failed provider=%s error=%s", result.provider, exc)
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


def _log_result(result: ApiCheckResult) -> None:
    if not result.configured:
        logger.info("%s: not configured, skipping.", result.provider)
        return

    auth_status = "passed" if result.auth_ok else "failed" if result.auth_ok is False else "unknown"
    logger.info(
        "%s: auth=%s status_code=%s detail=%s",
        result.provider,
        auth_status,
        result.auth_status_code or "-",
        result.auth_message,
    )
    logger.info("%s: %s", result.provider, result.balance_message)

    if result.auth_ok is False and result.auth_status_code in {401, 403, 407}:
        logger.warning("%s API authorization failed (%s).", result.provider, result.auth_message)
    if (
        result.balance_usd is not None
        and result.alert_threshold_usd is not None
        and result.balance_usd < result.alert_threshold_usd
    ):
        logger.warning(
            "%s API balance is $%.2f, below threshold $%.2f.",
            result.provider,
            result.balance_usd,
            result.alert_threshold_usd,
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    results = collect_api_health(settings)
    for result in results:
        _log_result(result)
        if result.configured:
            _record_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
