# common_utils

Shared utilities intended to be reused by sibling repos such as:

- `/Users/samtaneja/Codex/datastage`
- `/Users/samtaneja/Codex/dealnews`

This repo is the planned shared home for:

- proxy credential and provider config helpers
- logging helpers
- other small reusable workflow utilities

## Current scope

For now, this repo provides:

- a shared `.env.example` for `EVOMI_*` and `FLOPPYDATA_*`
- a small Python loader that turns those env vars into structured proxy configs
- provider builders for Evomi, Bright Data, Decodo, FloppyData, and direct/no-proxy

## Dealvant publishing

The one write path into Dealvant's Postgres, used by amazonnew, reviewgate
and autopub:

- `common_utils/dealvant/store.py`: upserts for products, offers, articles
  and roundups; `require_schema()` checks `schema_meta.schema_version`.
- `common_utils/dealvant/products.py`: Amazon page scan to product record, US
  price parsing, the shared foreign-price rule.
- `common_utils/dealvant/roundups.py`: roundup prompt, safe link assembly,
  pick labels and JSON-LD.
- `common_utils/dealvant/checks.py`: SEO/grammar/spelling checks (needs
  `pyspellchecker` in the consumer).
- `common_utils/dealvant/requests.py`: the roundup request queue
  (`reviewgate_roundup_requests` in dealstage MySQL) between reviewgate and
  autopub's worker; `testing.py` has an in-memory stand-in for tests.
- `common_utils/dealvant/ROUNDUPS.md`: how roundup articles are made.

Tests: `DEALVANT_TEST_PG_URI=postgresql://... pytest tests` (database tests
are skipped without it; they need a throwaway database with
`dealvant/db/schema.sql` applied).

## Shared Scraper Base Image

`common` owns the shared scraper base image. The image includes Python 3.11,
Playwright Chromium, shared scraper libraries, and the `run_job_common.sh` /
`metering_cli.py` helpers. It does **not** contain `common_utils`: each image
built on it (amazonnew, dealnews1, dealmoon1) copies `common_utils` in as one
of its own last layers, so a `common_utils` change neither rebuilds the base
nor invalidates the dependency installs layered on top of it.

Which repos a `common` change actually needs rebuilt is worked out by
`scripts/common_consumers.py` (used by deal-pipeline's `deploy.sh common`):
it maps the changed files to `common_utils` modules and finds the repos that
import them.

### Local Build

Build a local copy of the shared scraper base image with:

```bash
./scripts/build_scraper_base.sh
```

That now creates:

- a versioned tag like `local/scraper-base:py311-playwright-20260619-174500`
- a floating compatibility alias `local/scraper-base:py311-playwright`

If you want a custom version suffix:

```bash
./scripts/build_scraper_base.sh --version 20260619-174500
```

If a scraper repo wants to pin a specific tested base image version, set:

```env
SCRAPER_BASE_IMAGE=local/scraper-base:py311-playwright-20260619-174500
```

in that repo's `.env`.

### GitHub Container Registry

Pushing a semantic version tag from this repo publishes the shared base image to
GitHub Container Registry:

```bash
git tag v1.1.0
git push origin v1.1.0
```

The workflow publishes:

- `ghcr.io/<github-owner-lowercase>/scraper-base:1.1.0`
- `ghcr.io/<github-owner-lowercase>/scraper-base:1.1`
- `ghcr.io/<github-owner-lowercase>/scraper-base:1`

For this account, consumers should pin the exact version:

```env
SCRAPER_BASE_IMAGE=ghcr.io/samtaneja8/scraper-base:1.1.0
```

The workflow lowercases the GitHub owner before building the image name because
Docker image references must be lowercase.

### Automatic Cleanup

After each versioned build, the build script automatically runs:

- [scripts/cleanup_scraper_base_images.sh](scripts/cleanup_scraper_base_images.sh)

The cleanup logic preserves:

- any `local/scraper-base:*` image currently used by a Docker container
- any `SCRAPER_BASE_IMAGE=` tag referenced by a sibling repo `.env`
- the freshly built versioned tag
- the floating alias tag

To preview cleanup without deleting anything:

```bash
./scripts/build_scraper_base.sh --cleanup-dry-run
```

## Notes

- Put real proxy credentials in `common_utils/.env` on the machine that runs the jobs.
- `PROXY` should remain in each application repo's own `.env`
- `LOG_SAVE_DAYS` can live in an app repo `.env`; if missing there, wrappers use `common/.env`
- provider credentials like `EVOMI_*` and `FLOPPYDATA_*` are intended to live here
- set `SHARED_PROXY_ENV_FILE` if the shared proxy env file lives somewhere other than `common_utils/.env`

## VPS Observability and Cron

`common` also owns the shared VPS observability setup that replaces the old
`vpsmonitor` logging and host-monitoring role:

- [observability/alloy/config.alloy](observability/alloy/config.alloy) tails app-owned log files, Docker JSON logs, host metrics, Docker container metrics, and Alloy self metrics into Grafana Cloud.
- [scripts/run_cron_job.sh](scripts/run_cron_job.sh) is the simple shared cron wrapper for running one repo-owned job now.
- [scripts/setup_grafana_alloy.sh](scripts/setup_grafana_alloy.sh) installs the Grafana-provided Alloy plumbing on VPS1/VPS2 using values from `common/.env`.
- [docs/grafana-cloud-logging.md](docs/grafana-cloud-logging.md) documents the VPS install and cron examples.

The shared cron wrapper itself does not build images, clean Docker images,
mutate MySQL, or replay missed jobs. Those operations stay repo-owned or
manual, depending on the app script cron invokes.
