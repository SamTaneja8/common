from __future__ import annotations

import logging
import os
import threading
from copy import deepcopy
from typing import Any


logger = logging.getLogger("common_utils.proxy_telemetry")

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "configured_order": [],
    "providers": {},
    "last_provider": None,
}

_REMAINING_ENV_SUFFIXES = (
    "DATA_REMAINING_GB",
    "BANDWIDTH_REMAINING_GB",
    "BYTES_REMAINING",
    "DATA_REMAINING",
)


def _provider_key(provider_name: str) -> str:
    return provider_name.upper().replace("-", "_").replace(" ", "_")


def get_remaining_data_hint(provider_name: str) -> str | None:
    provider_key = _provider_key(provider_name)
    for suffix in _REMAINING_ENV_SUFFIXES:
        env_name = f"{provider_key}_{suffix}"
        value = os.getenv(env_name, "").strip()
        if value:
            return f"{env_name}={value}"
    return None


def _default_provider_metrics(provider_name: str) -> dict[str, Any]:
    return {
        "attempts": 0,
        "successes": 0,
        "failures": 0,
        "last_context": None,
        "last_error_type": None,
        "last_error_message": None,
        "remaining_data_hint": get_remaining_data_hint(provider_name),
    }


def _metrics_for_provider(provider_name: str) -> dict[str, Any]:
    return _STATE["providers"].setdefault(provider_name, _default_provider_metrics(provider_name))


def reset_proxy_metrics() -> None:
    with _LOCK:
        _STATE["configured_order"] = []
        _STATE["providers"] = {}
        _STATE["last_provider"] = None


def register_proxy_catalog(configs: list[dict[str, Any]] | list[str] | tuple[Any, ...]) -> None:
    provider_names: list[str] = []
    for config in configs:
        if isinstance(config, dict):
            provider_name = str(config.get("name") or "").strip()
        else:
            provider_name = str(config or "").strip()
        if provider_name:
            provider_names.append(provider_name)

    with _LOCK:
        _STATE["configured_order"] = provider_names
        for provider_name in provider_names:
            provider_metrics = _STATE["providers"].setdefault(provider_name, _default_provider_metrics(provider_name))
            provider_metrics["remaining_data_hint"] = get_remaining_data_hint(provider_name)

    logger.info("Registered proxy catalog providers=%s", provider_names)


def record_proxy_attempt(provider_name: str, context: str) -> None:
    with _LOCK:
        provider_metrics = _metrics_for_provider(provider_name)
        provider_metrics["attempts"] += 1
        provider_metrics["last_context"] = context
        provider_metrics["remaining_data_hint"] = get_remaining_data_hint(provider_name)
        _STATE["last_provider"] = provider_name

    logger.info(
        "Proxy attempt provider=%s context=%s remaining_data=%s",
        provider_name,
        context,
        get_remaining_data_hint(provider_name) or "unavailable",
    )


def record_proxy_success(provider_name: str, context: str) -> None:
    with _LOCK:
        provider_metrics = _metrics_for_provider(provider_name)
        provider_metrics["successes"] += 1
        provider_metrics["last_context"] = context
        _STATE["last_provider"] = provider_name

    logger.info("Proxy success provider=%s context=%s", provider_name, context)


def record_proxy_failure(
    provider_name: str,
    context: str,
    error_type: str | None,
    error_message: str | None,
) -> None:
    with _LOCK:
        provider_metrics = _metrics_for_provider(provider_name)
        provider_metrics["failures"] += 1
        provider_metrics["last_context"] = context
        provider_metrics["last_error_type"] = error_type
        provider_metrics["last_error_message"] = error_message
        provider_metrics["remaining_data_hint"] = get_remaining_data_hint(provider_name)
        _STATE["last_provider"] = provider_name

    logger.warning(
        "Proxy failure provider=%s context=%s error_type=%s error=%s remaining_data=%s",
        provider_name,
        context,
        error_type,
        error_message,
        get_remaining_data_hint(provider_name) or "unavailable",
    )


def snapshot_proxy_metrics() -> dict[str, Any]:
    with _LOCK:
        return deepcopy(_STATE)


def used_proxy_names(snapshot: dict[str, Any] | None = None) -> list[str]:
    snapshot = snapshot or snapshot_proxy_metrics()
    providers = snapshot.get("providers", {}) or {}
    configured_order = snapshot.get("configured_order", []) or []

    used_names: list[str] = []
    seen: set[str] = set()
    for provider_name in configured_order:
        metrics = providers.get(provider_name, {})
        if any(int(metrics.get(counter_name, 0) or 0) > 0 for counter_name in ("attempts", "successes", "failures")):
            seen.add(provider_name)
            used_names.append(provider_name)

    for provider_name, metrics in providers.items():
        if provider_name in seen:
            continue
        if any(int(metrics.get(counter_name, 0) or 0) > 0 for counter_name in ("attempts", "successes", "failures")):
            seen.add(provider_name)
            used_names.append(provider_name)
    return used_names


def merge_proxy_metrics(snapshot: dict[str, Any] | None) -> None:
    if not snapshot:
        return

    incoming_order = [str(name) for name in snapshot.get("configured_order", []) if str(name).strip()]
    incoming_providers = snapshot.get("providers", {}) or {}
    incoming_last_provider = snapshot.get("last_provider")

    with _LOCK:
        if incoming_order:
            merged_order = list(_STATE["configured_order"])
            for provider_name in incoming_order:
                if provider_name not in merged_order:
                    merged_order.append(provider_name)
            _STATE["configured_order"] = merged_order

        for provider_name, raw_metrics in incoming_providers.items():
            provider_metrics = _metrics_for_provider(provider_name)
            provider_metrics["attempts"] += int(raw_metrics.get("attempts", 0) or 0)
            provider_metrics["successes"] += int(raw_metrics.get("successes", 0) or 0)
            provider_metrics["failures"] += int(raw_metrics.get("failures", 0) or 0)
            if raw_metrics.get("last_context"):
                provider_metrics["last_context"] = raw_metrics.get("last_context")
            if raw_metrics.get("last_error_type"):
                provider_metrics["last_error_type"] = raw_metrics.get("last_error_type")
            if raw_metrics.get("last_error_message"):
                provider_metrics["last_error_message"] = raw_metrics.get("last_error_message")
            if raw_metrics.get("remaining_data_hint"):
                provider_metrics["remaining_data_hint"] = raw_metrics.get("remaining_data_hint")

        if incoming_last_provider:
            _STATE["last_provider"] = incoming_last_provider
