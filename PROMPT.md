# Recreate the common Repository

Use this prompt to recreate the `common` repository from scratch.

Build a shared Python utility package named `common` whose primary purpose is to keep proxy credentials, Playwright exception capture, job metering, and the standardized scraper base image in one place. Repositories such as `dealstage` should import this package and build from its base image instead of duplicating proxy, browser, and runtime logic in every repo.

## Repository Shape

Create:

- `pyproject.toml`: project name `common`, version `0.1.0`, Python `>=3.11`.
- `common_utils/__init__.py`: export shared proxy helpers.
- `common_utils/proxy_settings.py`: shared env loading, settings dataclass, and proxy config builders.
- `common_utils/playwright_exceptions.py`: shared Playwright exception capture pipeline.
- `common_utils/metering.py`: env-driven job metering helpers for `job_run_metering`.
- `common_utils/metering_cli.py`: CLI entry point for shell wrappers.
- `Dockerfile.scraper-base`: Python 3.11 slim Bookworm scraper base image with Playwright Chromium.
- `scripts/build_scraper_base.sh`: local base image build script.
- `scripts/run_job_common.sh`: generic command wrapper that records shell metering events.
- `bin/metering_cli.py`: executable wrapper installed into the base image path.
- `.env.example`: shared proxy credential variables for Evomi, Brightdata, Decodo, and FloppyData.
- `README.md`: install/use examples, local editable install examples, and env variable reference.

## Shared Env Loading

Implement `load_shared_proxy_env()`:

- If `SHARED_PROXY_ENV_FILE` is set, load that file with `python-dotenv`.
- Otherwise load `.env` from the common repo directory.
- Do not overwrite existing environment variables.

This allows a VPS or local machine to keep one shared credential file while each scraper repo only defines the proxy sequence or provider choice it needs.

## Settings

Create a `SharedProxySettings` dataclass that reads these values:

- Evomi: username, password, host default `core-residential.evomi.com`, port default `1000`, country default `US`.
- Brightdata: username, password, host default `brd.superproxy.io`, port default `22225`, country default `us`.
- Decodo: username, password, host default `gate.decodo.com`, port default `10001`, country default `us`.
- FloppyData: username, password, host default `gate.floppydata.com`, port default `10000`, country default `us`.

Keep country normalization provider-specific: Evomi uppercase, others lowercase.

## Proxy Builders

Implement provider builders returning dictionaries with this shape:

```python
{
  "name": "evomi",
  "playwright": {
    "server": "http://host:port",
    "username": "...",
    "password": "..."
  }
}
```

Required builders:

- `build_evomi_config(settings)`: append `_country-<COUNTRY>` to the password when that suffix is not already present.
- `build_brightdata_config(settings)`: username format `<username>-country-<country>-session-bd`.
- `build_decodo_config(settings)`: username format `<username>_country-<country>_session-dc`.
- `build_floppydata_config(settings)`: username format `<username>_country-<country>_session-fd`.
- `build_direct_config()`: return `{"name": "no-proxy", "playwright": None}`.
- `build_proxy_config_for_provider(provider_name)`: dispatch case-insensitively and raise a clear error for unknown providers.

## Scraper Base Image

Create a base image tagged locally as `local/scraper-base:py311-playwright` by default.

The image must:

- Use the Bookworm-based `python:3.11-slim` variant.
- Install `build-essential`, `curl`, `default-libmysqlclient-dev`, and `pkg-config`.
- Set `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`.
- Install common Python packages: `mysql-connector-python`, `requests`, `httpx`, `beautifulsoup4`, `playwright`, `playwright-stealth`, `python-dotenv`, and `pydantic`.
- Run `python -m playwright install --with-deps chromium`.
- Launch Chromium during image build against a small `data:` URL as a smoke test.
- Install the `common` package.
- Install `run_job_common.sh` and `metering_cli.py` into `/usr/local/bin`.
- Create `/var/www/app`, `/var/www/app/logs`, and `/var/www/app/artifacts`.

Do not bake credentials into the image.

## Shared Runtime Helpers

`playwright_exceptions.py` should expose the Playwright exception classes and helpers used by scraper repos: `TelemetryMysqlConfig`, `PlaywrightExceptionContext`, `PlaywrightExceptionPipeline`, event buffering, artifact writing, optional Discord alerting, and `resolve_scheduled_slot`.

`metering.py` should read telemetry connection settings directly from environment variables, write to `job_run_metering`, collect basic Linux system metrics, snapshot which proxy providers appear configured without logging secrets, and provide `JobMeter`, `ensure_metering_table`, and `shell_mark_failure`.

`metering_cli.py` should expose `start` and `shell-fail` commands.

## Data Captured

This repo does not scrape product data. It captures shared proxy credentials from environment variables, converts them into provider-specific Playwright proxy config objects, records job metering events when invoked by runtime wrappers, and records Playwright exception artifacts/events when imported by scraper repos.

## Insert and Update Logic

This repo does not own product tables. Its database writes are limited to shared telemetry helpers:

- `metering.py` inserts or updates one `job_run_metering` row per run UUID.
- `playwright_exceptions.py` inserts append-only rows into `playwright_exception_events`.
- Proxy credentials are centralized in this repo or in `SHARED_PROXY_ENV_FILE`, while consuming repos choose which provider sequence to use.

## Important Implementation Notes

- Keep the package dependency-light.
- Do not log secrets.
- Avoid hard-coding user-specific credentials.
- Keep exported helper names stable so `dealstage` and future scrapers can import from `common_utils.proxy_settings`.
