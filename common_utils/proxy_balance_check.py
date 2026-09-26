"""Log-only proxy authorization and balance checks for Evomi and FloppyData.

Ported from the retired vpsmonitor repo's app/collectors/proxy_health.py
(plus the daily-heartbeat gating from its hourly_job.py) as a standalone
script in common. Results are logged only -- no database table -- per
explicit decision when vpsmonitor's functionality was migrated into common:
the proxy *connection* credentials this checks are already the ones every
repo's own scraper run logs against on failure, so a persisted history here
would just duplicate what Loki already retains from those run logs.

Usage: python -m common_utils.proxy_balance_check
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

from common_utils.proxy_settings import load_shared_proxy_env


logger = logging.getLogger("common_utils.proxy_balance_check")

EVOMI_BALANCE_URL = "https://reseller.evomi.com/v2/reseller/my_info"


def _get_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


@dataclass(frozen=True)
class ProxyBalanceSettings:
    proxy_check_url: str
    proxy_balance_alert_usd: float
    evomi_username: str
    evomi_password: str
    evomi_host: str
    evomi_port: str
    evomi_country: str
    evomi_api_key: str
    floppydata_username: str
    floppydata_password: str
    floppydata_host: str
    floppydata_port: str
    floppydata_country: str
    floppydata_balance_api_url: str
    floppydata_balance_auth_header: str
    floppydata_balance_auth_value: str


def load_settings() -> ProxyBalanceSettings:
    load_shared_proxy_env()
    return ProxyBalanceSettings(
        proxy_check_url=_get_str("PROXY_CHECK_URL", "http://httpbin.org/ip"),
        proxy_balance_alert_usd=_get_float("PROXY_BALANCE_ALERT_USD", 5.0),
        evomi_username=_get_str("EVOMI_USERNAME"),
        evomi_password=_get_str("EVOMI_PASSWORD"),
        evomi_host=_get_str("EVOMI_HOST", "core-residential.evomi.com"),
        evomi_port=_get_str("EVOMI_PORT", "1000"),
        evomi_country=_get_str("EVOMI_COUNTRY", "us").upper(),
        evomi_api_key=_get_str("EVOMI_API_KEY"),
        floppydata_username=_get_str("FLOPPYDATA_USERNAME"),
        floppydata_password=_get_str("FLOPPYDATA_PASSWORD"),
        floppydata_host=_get_str("FLOPPYDATA_HOST", "gate.floppydata.com"),
        floppydata_port=_get_str("FLOPPYDATA_PORT", "10000"),
        floppydata_country=_get_str("FLOPPYDATA_COUNTRY", "us"),
        floppydata_balance_api_url=_get_str("FLOPPYDATA_BALANCE_API_URL"),
        floppydata_balance_auth_header=_get_str("FLOPPYDATA_BALANCE_AUTH_HEADER", "Authorization"),
        floppydata_balance_auth_value=_get_str("FLOPPYDATA_BALANCE_AUTH_VALUE"),
    )


@dataclass(frozen=True)
class ProxyCheckResult:
    provider: str
    configured: bool
    auth_ok: bool | None
    auth_status_code: int | None
    auth_message: str
    balance_usd: float | None
    balance_message: str


def _evomi_proxy_password(settings: ProxyBalanceSettings) -> str:
    password = settings.evomi_password
    if not password:
        return ""
    if "_country-" in password:
        return password
    return f"{password}_country-{settings.evomi_country}"


def _floppydata_proxy_username(settings: ProxyBalanceSettings) -> str:
    username = settings.floppydata_username
    if not username:
        return ""
    return f"{username}_country-{settings.floppydata_country}_session-common"


def _proxy_check(url: str, proxy_url: str) -> tuple[bool | None, int | None, str]:
    opener = request.build_opener(
        request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    req = request.Request(url, headers={"User-Agent": "common-proxy-balance-check/1.0"})
    try:
        with opener.open(req, timeout=20) as response:
            return True, getattr(response, "status", 200), "authorization passed"
    except error.HTTPError as exc:
        if exc.code == 407:
            return False, exc.code, "proxy authorization failed with HTTP 407"
        return None, exc.code, f"proxy request returned HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "407" in message:
            return False, 407, f"proxy authorization failed: {message}"
        return None, None, f"proxy request error: {message}"


def _get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    import json

    req = request.Request(url, headers=headers or {})
    with request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _find_balance(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in (
            "balance",
            "credits",
            "credit",
            "remaining_balance",
            "remainingBalance",
            "available_balance",
            "availableBalance",
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


def _check_evomi_balance(settings: ProxyBalanceSettings) -> tuple[float | None, str]:
    if not settings.evomi_api_key:
        return None, "balance check skipped: EVOMI_API_KEY is not configured"
    try:
        payload = _get_json(EVOMI_BALANCE_URL, headers={"X-API-KEY": settings.evomi_api_key})
        balance = _find_balance(payload)
        if balance is None:
            return None, "balance check failed: no numeric balance found in Evomi response"
        return balance, f"balance=${balance:.2f}"
    except Exception as exc:  # noqa: BLE001
        return None, f"balance check failed: {exc}"


def _check_floppydata_balance(settings: ProxyBalanceSettings) -> tuple[float | None, str]:
    if not settings.floppydata_balance_api_url:
        return None, "balance check skipped: FLOPPYDATA_BALANCE_API_URL is not configured"
    headers: dict[str, str] = {}
    if settings.floppydata_balance_auth_header and settings.floppydata_balance_auth_value:
        headers[settings.floppydata_balance_auth_header] = settings.floppydata_balance_auth_value
    try:
        payload = _get_json(settings.floppydata_balance_api_url, headers=headers)
        balance = _find_balance(payload)
        if balance is None:
            return None, "balance check failed: no numeric balance found in FloppyData response"
        return balance, f"balance=${balance:.2f}"
    except Exception as exc:  # noqa: BLE001
        return None, f"balance check failed: {exc}"


def collect_proxy_health(settings: ProxyBalanceSettings) -> list[ProxyCheckResult]:
    results: list[ProxyCheckResult] = []

    evomi_configured = bool(settings.evomi_username and settings.evomi_password)
    if evomi_configured:
        evomi_proxy_url = "http://{username}:{password}@{host}:{port}".format(
            username=parse.quote(settings.evomi_username, safe=""),
            password=parse.quote(_evomi_proxy_password(settings), safe=""),
            host=settings.evomi_host,
            port=settings.evomi_port,
        )
        auth_ok, auth_status_code, auth_message = _proxy_check(settings.proxy_check_url, evomi_proxy_url)
        balance_usd, balance_message = _check_evomi_balance(settings)
    else:
        auth_ok, auth_status_code, auth_message = None, None, "authorization check skipped: proxy credentials not configured"
        balance_usd, balance_message = None, "balance check skipped: proxy credentials not configured"
    results.append(
        ProxyCheckResult(
            provider="Evomi",
            configured=evomi_configured,
            auth_ok=auth_ok,
            auth_status_code=auth_status_code,
            auth_message=auth_message,
            balance_usd=balance_usd,
            balance_message=balance_message,
        )
    )

    floppydata_configured = bool(settings.floppydata_username and settings.floppydata_password)
    if floppydata_configured:
        floppydata_proxy_url = "http://{username}:{password}@{host}:{port}".format(
            username=parse.quote(_floppydata_proxy_username(settings), safe=""),
            password=parse.quote(settings.floppydata_password, safe=""),
            host=settings.floppydata_host,
            port=settings.floppydata_port,
        )
        auth_ok, auth_status_code, auth_message = _proxy_check(settings.proxy_check_url, floppydata_proxy_url)
        balance_usd, balance_message = _check_floppydata_balance(settings)
    else:
        auth_ok, auth_status_code, auth_message = None, None, "authorization check skipped: proxy credentials not configured"
        balance_usd, balance_message = None, "balance check skipped: proxy credentials not configured"
    results.append(
        ProxyCheckResult(
            provider="FloppyData",
            configured=floppydata_configured,
            auth_ok=auth_ok,
            auth_status_code=auth_status_code,
            auth_message=auth_message,
            balance_usd=balance_usd,
            balance_message=balance_message,
        )
    )

    return results


def _log_result(settings: ProxyBalanceSettings, result: ProxyCheckResult) -> None:
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

    if result.auth_status_code == 407 or result.auth_ok is False:
        logger.warning("%s proxy authorization failed (%s).", result.provider, result.auth_message)
    if result.balance_usd is not None and result.balance_usd < settings.proxy_balance_alert_usd:
        logger.warning(
            "%s proxy balance is $%.2f, below threshold $%.2f.",
            result.provider,
            result.balance_usd,
            settings.proxy_balance_alert_usd,
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    results = collect_proxy_health(settings)
    for result in results:
        _log_result(settings, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
