#!/usr/bin/env python3
"""Runs the sequence defined in pipeline.yaml: one step after another,
waiting for each to actually finish before starting the next, instead of
guessing at cron time offsets. Each step is invoked the exact same way you'd
run it by hand (common/scripts/run_script.sh <repo>/<path> [args]), so
locking/per-invocation logging/job telemetry are unchanged -- this only adds
sequencing, a resource check between steps, and a pipeline_run row tying a
run's individual job_run_metering rows together for the dashboard.

Runs directly on the VPS host (not inside a container) via cron:
    0 6 * * * /usr/bin/python3 /home/botuser/common/orchestration/run_pipeline.py

Host dependencies (not bundled in any repo's image, since this never runs
inside one): PyYAML and mysql-connector-python --
    pip3 install pyyaml mysql-connector-python
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    import mysql.connector
except ImportError:  # pragma: no cover
    mysql = None  # type: ignore[assignment]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [pipeline_run=%(pipeline_run_id)s] %(message)s",
)
logger = logging.getLogger("run_pipeline")

ORCHESTRATION_DIR = Path(__file__).resolve().parent
COMMON_DIR = ORCHESTRATION_DIR.parent
PARENT_DIR = COMMON_DIR.parent
RUN_SCRIPT = COMMON_DIR / "scripts" / "run_script.sh"
DEFAULT_PIPELINE_FILE = ORCHESTRATION_DIR / "pipeline.yaml"


class _RunIdAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict) -> tuple:
        kwargs.setdefault("extra", {})["pipeline_run_id"] = self.extra["pipeline_run_id"]
        return msg, kwargs


def _load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader so this bare host script sees the same
    TELEMETRY_MYSQL_* settings the containers get via docker-compose's
    env_file -- there's no docker-compose here to do it automatically."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _mysql_connection():
    if mysql is None:
        raise RuntimeError("mysql-connector-python is not installed (pip3 install mysql-connector-python)")
    return mysql.connector.connect(
        host=os.getenv("TELEMETRY_MYSQL_HOST", "unified-mysql"),
        port=int(os.getenv("TELEMETRY_MYSQL_PORT", "3306")),
        user=os.getenv("TELEMETRY_MYSQL_USER", ""),
        password=os.getenv("TELEMETRY_MYSQL_PASS", ""),
        database=os.getenv("TELEMETRY_MYSQL_DATA", "telemetry_db"),
    )


def _execute(query: str, params: tuple, *, context: str) -> None:
    connection = None
    cursor = None
    try:
        connection = _mysql_connection()
        cursor = connection.cursor()
        cursor.execute(query, params)
        connection.commit()
    except Exception as exc:
        logger.warning("pipeline_run write failed context=%s error=%s", context, exc)
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


def create_pipeline_run(pipeline_run_id: str, pipeline_name: str, total_steps: int, trigger_source: str) -> None:
    _execute(
        """
        INSERT INTO pipeline_run (
            pipeline_run_id, pipeline_name, status, trigger_source,
            total_steps, current_step_index, started_at
        ) VALUES (%s, %s, 'RUNNING', %s, %s, 0, UTC_TIMESTAMP())
        """,
        (pipeline_run_id, pipeline_name, trigger_source, total_steps),
        context="create",
    )


def update_current_step(pipeline_run_id: str, step_index: int, step_name: str) -> None:
    _execute(
        """
        UPDATE pipeline_run
        SET current_step_index = %s, current_step_name = %s
        WHERE pipeline_run_id = %s
        """,
        (step_index, step_name, pipeline_run_id),
        context="update_step",
    )


def finish_pipeline_run(
    pipeline_run_id: str,
    status: str,
    started_at: datetime,
    config_snapshot: dict[str, Any],
    failed_step_name: str | None,
    error_summary: str | None,
) -> None:
    duration_seconds = int((datetime.now(timezone.utc) - started_at).total_seconds())
    _execute(
        """
        UPDATE pipeline_run
        SET status = %s, ended_at = UTC_TIMESTAMP(), duration_seconds = %s,
            config_json = %s, failed_step_name = %s, error_summary = %s
        WHERE pipeline_run_id = %s
        """,
        (
            status,
            duration_seconds,
            json.dumps(config_snapshot, default=str),
            failed_step_name,
            error_summary,
            pipeline_run_id,
        ),
        context="finish",
    )


def link_job_run(pipeline_run_id: str, job_run_id: str) -> None:
    _execute(
        "UPDATE job_run_metering SET pipeline_run_id = %s WHERE run_uuid = %s",
        (pipeline_run_id, job_run_id),
        context="link_job_run",
    )


def wait_for_capacity(
    max_load_per_cpu: float = 1.5,
    max_wait_seconds: int = 300,
    poll_interval: int = 15,
    log: logging.LoggerAdapter | logging.Logger = logger,
) -> None:
    """Best-effort resource gate between steps -- not a hard guarantee, just
    backs off if the host is clearly already under load rather than piling
    another job on top blindly. Gives up and proceeds anyway after
    max_wait_seconds so a persistently busy host can't stall the pipeline
    forever."""
    try:
        cpu_count = os.cpu_count() or 1
    except Exception:
        cpu_count = 1
    threshold = max_load_per_cpu * cpu_count
    waited = 0
    while waited < max_wait_seconds:
        try:
            load1, _, _ = os.getloadavg()
        except (AttributeError, OSError):
            return  # not available on this platform; skip the gate
        if load1 <= threshold:
            return
        log.warning(
            "Host load %.2f exceeds threshold %.2f (cpu_count=%s); waiting %ss before next step",
            load1,
            threshold,
            cpu_count,
            poll_interval,
        )
        time.sleep(poll_interval)
        waited += poll_interval
    log.warning("Proceeding despite sustained high load after waiting %ss", max_wait_seconds)


def run_step(step: dict[str, Any], pipeline_run_id: str, log: logging.LoggerAdapter) -> bool:
    """Runs one step's `repeat` invocations in sequence. Returns True if all
    of them succeeded (or repeat wasn't set -- single run), False if any
    invocation failed."""
    name = step["name"]
    script_path = PARENT_DIR / step["script"]
    args = step.get("args", [])
    batch_env = step.get("batch_env")
    batch_size = step.get("batch_size")
    repeat = int(step.get("repeat", 1))

    all_succeeded = True
    for iteration in range(1, repeat + 1):
        job_run_id = str(uuid.uuid4())
        env = os.environ.copy()
        env["JOB_RUN_ID"] = job_run_id
        if batch_env:
            env[batch_env] = str(batch_size)

        cmd = [str(RUN_SCRIPT), str(script_path), *args]
        log.info(
            "Starting step=%s iteration=%s/%s job_run_id=%s %s=%s cmd=%s",
            name,
            iteration,
            repeat,
            job_run_id,
            batch_env or "-",
            batch_size if batch_env else "-",
            " ".join(cmd),
        )
        result = subprocess.run(cmd, env=env)
        link_job_run(pipeline_run_id, job_run_id)

        if result.returncode != 0:
            log.error(
                "Step failed step=%s iteration=%s/%s job_run_id=%s exit_code=%s",
                name,
                iteration,
                repeat,
                job_run_id,
                result.returncode,
            )
            all_succeeded = False
        else:
            log.info("Step succeeded step=%s iteration=%s/%s job_run_id=%s", name, iteration, repeat, job_run_id)

    return all_succeeded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-file", default=str(DEFAULT_PIPELINE_FILE))
    parser.add_argument("--trigger-source", default="cron")
    args = parser.parse_args()

    _load_env_file(COMMON_DIR / ".env")

    pipeline_file = Path(args.pipeline_file)
    config = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
    sequence = config["sequence"]

    pipeline_run_id = str(uuid.uuid4())
    log = _RunIdAdapter(logger, {"pipeline_run_id": pipeline_run_id})
    started_at = datetime.now(timezone.utc)
    pipeline_name = pipeline_file.stem

    create_pipeline_run(pipeline_run_id, pipeline_name, len(sequence), args.trigger_source)
    log.info("Pipeline run started pipeline_name=%s total_steps=%s", pipeline_name, len(sequence))

    any_step_failed = False
    failed_step_name: str | None = None
    error_summary: str | None = None

    try:
        for index, step in enumerate(sequence, start=1):
            update_current_step(pipeline_run_id, index, step["name"])
            wait_for_capacity(log=log)

            succeeded = run_step(step, pipeline_run_id, log)
            if not succeeded:
                any_step_failed = True
                on_failure = step.get("on_failure", "continue")
                if on_failure == "halt":
                    failed_step_name = step["name"]
                    error_summary = f"step '{step['name']}' failed with on_failure=halt"
                    log.error("Halting pipeline run at step=%s (on_failure=halt)", step["name"])
                    break
    except Exception as exc:  # noqa: BLE001 - must still close out the pipeline_run row
        log.exception("Pipeline run crashed unexpectedly")
        finish_pipeline_run(pipeline_run_id, "FAILED", started_at, config, failed_step_name, f"orchestrator crashed: {exc}")
        return 1

    if failed_step_name:
        status = "FAILED"
    elif any_step_failed:
        status = "PARTIAL"
    else:
        status = "SUCCESS"

    finish_pipeline_run(pipeline_run_id, status, started_at, config, failed_step_name, error_summary)
    log.info("Pipeline run finished status=%s", status)
    return 0 if status != "FAILED" else 1


if __name__ == "__main__":
    sys.exit(main())
