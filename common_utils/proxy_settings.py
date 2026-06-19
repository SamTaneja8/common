from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _set_env_defaults_from_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(name, value)


def load_shared_proxy_env() -> None:
    env_file = os.getenv("SHARED_PROXY_ENV_FILE", "").strip()
    if env_file:
        _set_env_defaults_from_file(Path(env_file).expanduser())
        return
    _set_env_defaults_from_file(Path(__file__).resolve().parents[1] / ".env")


def _get_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True, slots=True)
class SharedProxySettings:
    evomi_username: str
    evomi_password: str
    evomi_host: str
    evomi_port: str
    evomi_country: str
    brightdata_username: str
    brightdata_password: str
    brightdata_host: str
    brightdata_port: str
    brightdata_country: str
    decodo_username: str
    decodo_password: str
    decodo_host: str
    decodo_port: str
    decodo_country: str
    floppydata_username: str
    floppydata_password: str
    floppydata_host: str
    floppydata_port: str
    floppydata_country: str


def load_shared_proxy_settings() -> SharedProxySettings:
    load_shared_proxy_env()
    return SharedProxySettings(
        evomi_username=_get_str("EVOMI_USERNAME"),
        evomi_password=_get_str("EVOMI_PASSWORD"),
        evomi_host=_get_str("EVOMI_HOST", "core-residential.evomi.com"),
        evomi_port=_get_str("EVOMI_PORT", "1000"),
        evomi_country=_get_str("EVOMI_COUNTRY", "us").upper(),
        brightdata_username=_get_str("BRIGHTDATA_USERNAME"),
        brightdata_password=_get_str("BRIGHTDATA_PASSWORD"),
        brightdata_host=_get_str("BRIGHTDATA_HOST", "brd.superproxy.io"),
        brightdata_port=_get_str("BRIGHTDATA_PORT", "22225"),
        brightdata_country=_get_str("BRIGHTDATA_COUNTRY", "us"),
        decodo_username=_get_str("DECODO_USERNAME"),
        decodo_password=_get_str("DECODO_PASSWORD"),
        decodo_host=_get_str("DECODO_HOST", "gate.decodo.com"),
        decodo_port=_get_str("DECODO_PORT", "10001"),
        decodo_country=_get_str("DECODO_COUNTRY", "us"),
        floppydata_username=_get_str("FLOPPYDATA_USERNAME"),
        floppydata_password=_get_str("FLOPPYDATA_PASSWORD"),
        floppydata_host=_get_str("FLOPPYDATA_HOST", "gate.floppydata.com"),
        floppydata_port=_get_str("FLOPPYDATA_PORT", "10000"),
        floppydata_country=_get_str("FLOPPYDATA_COUNTRY", "us"),
    )


def build_evomi_config(settings: SharedProxySettings | None = None) -> dict | None:
    settings = settings or load_shared_proxy_settings()
    if not (settings.evomi_username and settings.evomi_password):
        return None

    resolved_password = (
        settings.evomi_password
        if "_country-" in settings.evomi_password
        else f"{settings.evomi_password}_country-{settings.evomi_country}"
    )
    return {
        "name": "Evomi",
        "playwright": {
            "server": f"http://{settings.evomi_host}:{settings.evomi_port}",
            "username": settings.evomi_username,
            "password": resolved_password,
        },
    }


def build_brightdata_config(settings: SharedProxySettings | None = None) -> dict | None:
    settings = settings or load_shared_proxy_settings()
    if not (settings.brightdata_username and settings.brightdata_password):
        return None

    resolved_username = (
        f"{settings.brightdata_username}-country-{settings.brightdata_country}-session-bd"
    )
    return {
        "name": "Brightdata",
        "playwright": {
            "server": f"http://{settings.brightdata_host}:{settings.brightdata_port}",
            "username": resolved_username,
            "password": settings.brightdata_password,
        },
    }


def build_decodo_config(settings: SharedProxySettings | None = None) -> dict | None:
    settings = settings or load_shared_proxy_settings()
    if not (settings.decodo_username and settings.decodo_password):
        return None

    resolved_username = f"{settings.decodo_username}_country-{settings.decodo_country}_session-dc"
    return {
        "name": "Decodo",
        "playwright": {
            "server": f"http://{settings.decodo_host}:{settings.decodo_port}",
            "username": resolved_username,
            "password": settings.decodo_password,
        },
    }


def build_floppydata_config(settings: SharedProxySettings | None = None) -> dict | None:
    settings = settings or load_shared_proxy_settings()
    if not (settings.floppydata_username and settings.floppydata_password):
        return None

    resolved_username = (
        f"{settings.floppydata_username}_country-{settings.floppydata_country}_session-fd"
    )
    return {
        "name": "FloppyData",
        "playwright": {
            "server": f"http://{settings.floppydata_host}:{settings.floppydata_port}",
            "username": resolved_username,
            "password": settings.floppydata_password,
        },
    }


def build_direct_config() -> dict:
    return {"name": "no-proxy", "playwright": None}


PROVIDER_BUILDERS = {
    "DIRECT": build_direct_config,
    "EVOMI": build_evomi_config,
    "BRIGHTDATA": build_brightdata_config,
    "DECODO": build_decodo_config,
    "FLOPPYDATA": build_floppydata_config,
}


def build_proxy_config_for_provider(provider_name: str) -> dict | None:
    builder = PROVIDER_BUILDERS.get(provider_name.strip().upper())
    if builder is None:
        supported = ", ".join(sorted(PROVIDER_BUILDERS))
        raise ValueError(f"Unknown proxy provider '{provider_name}'. Supported providers: {supported}")
    return builder()


def get_proxy_order() -> list[str]:
    raw = _get_str("PROXY", "DIRECT")
    order = [
        normalized
        for normalized in ("".join(character for character in part.strip().upper() if character.isalnum()) for part in raw.split(","))
        if normalized
    ]
    return order or ["DIRECT"]


def build_proxy_configs(settings: SharedProxySettings | None = None) -> list[dict]:
    settings = settings or load_shared_proxy_settings()
    configs: list[dict] = []
    for provider_name in get_proxy_order():
        if provider_name == "DIRECT":
            configs.append(build_direct_config())
            continue
        config = build_proxy_config_for_provider(provider_name)
        if config is None:
            continue
        if provider_name == "EVOMI":
            config = build_evomi_config(settings)
        elif provider_name == "BRIGHTDATA":
            config = build_brightdata_config(settings)
        elif provider_name == "DECODO":
            config = build_decodo_config(settings)
        elif provider_name == "FLOPPYDATA":
            config = build_floppydata_config(settings)
        if config is not None:
            configs.append(config)
    return configs
