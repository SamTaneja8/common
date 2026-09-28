#!/usr/bin/env python3
"""Runs the sequence defined in pipeline.yaml: one step after another,
waiting for each to actually finish before starting the next, instead of
guessing at cron time offsets. Each step is invoked the exact same way you'd
run it by hand (common/scripts/run_script.sh <repo>/<path> [args]), so
locking/per-invocation logging/job telemetry are unchanged -- this only adds
sequencing, a resource check between steps, and a pipeline_run row tying a
run's individual job_run_metering rows together for the dashboard.

Each step may carry its own `schedule:` (standard 5-field cron syntax) --
see matches_cron_schedule() below. A step with no `schedule` is always
eligible. This lets one pipeline.yaml hold steps on different cadences with
a single hourly cron trigger, instead of needing a separate crontab line
(and a separate --pipeline-file) per schedule:

    0 * * * * /usr/bin/python3 /home/botuser/common/orchestration/run_pipeline.py

Since cron only invokes this hourly, a step's `schedule` minute field is
only ever checked against :00 -- write schedules in terms of which hour(s)
they should fire on (e.g. "0 */2 * * *" for every 2 hours), not specific
minutes. If nothing is due on a given invocation, the script logs that and
exits cleanly without creating a pipeline_run row.

Multiple pipeline.yaml files are also still supported side by side via
--pipeline-file, e.g. for a schedule that should live in its own file
rather than as embedded per-step schedules in one shared file.

Host dependencies (not bundled in any repo's image, since this never runs
inside one): PyYAML and mysql-connector-python, in common/.venv --
    common/scripts/setup_host_python.sh
When run as a program, this script re-executes itself with common/.venv's
Python if that exists, so the crontab line can keep using python3. (Ubuntu
24.04's python3-mysql.connector is 8.0.15, which calls ssl.wrap_socket,
removed in Python 3.12, and can't connect.)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_VENV_PYTHON = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
if (
    __name__ == "__main__"
    and _VENV_PYTHON.exists()
    and not os.environ.get("RUN_PIPELINE_IN_VENV")
):
    os.environ["RUN_PIPELINE_IN_VENV"] = "1"
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), *sys.argv])

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


def _parse_cron_field(field: str, min_val: int, max_val: int) -> set[int]:
    """Expands one cron field (comma-separated list of *, N, N-M, */S, or
    N-M/S) into the concrete set of values it matches within
    [min_val, max_val]. No support for names ("MON", "JAN") or the
    non-POSIX "L"/"W"/"#" extensions -- plain numeric cron only."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError(f"Step value must be positive: {part!r}/{step_str!r}")
        if part == "*":
            range_start, range_end = min_val, max_val
        elif "-" in part:
            start_str, end_str = part.split("-", 1)
            range_start, range_end = int(start_str), int(end_str)
        else:
            range_start = range_end = int(part)
        if not (min_val <= range_start <= max_val) or not (min_val <= range_end <= max_val) or range_start > range_end:
            raise ValueError(f"Field value out of range [{min_val}, {max_val}]: {part!r}")
        values.update(range(range_start, range_end + 1, step))
    return values


def matches_cron_schedule(schedule: str, when: datetime) -> bool:
    """Standard 5-field cron matching (minute hour day-of-month month
    day-of-week) against a naive local-time datetime -- local time, not
    UTC, since that's what an actual crontab entry on this host would mean
    too, and step schedules are meant to read exactly like one.

    Implements POSIX cron's day-of-month/day-of-week OR quirk: if BOTH
    fields are restricted (neither is "*"), a match on EITHER is enough to
    fire; if only one is restricted, that one alone governs. Most people
    only ever restrict one of the two, where this is invisible -- but it
    matters the moment both are set (e.g. "0 6 1 * MON" does NOT mean "6am
    on the 1st AND if it's a Monday", it means "6am on the 1st OR any
    Monday").
    """
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError(f"Cron schedule must have exactly 5 fields (minute hour dom month dow): {schedule!r}")
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    if when.minute not in _parse_cron_field(minute_f, 0, 59):
        return False
    if when.hour not in _parse_cron_field(hour_f, 0, 23):
        return False
    if when.month not in _parse_cron_field(month_f, 1, 12):
        return False

    dom_restricted = dom_f != "*"
    dow_restricted = dow_f != "*"
    dom_match = when.day in _parse_cron_field(dom_f, 1, 31)

    # cron's day-of-week is Sunday=0..Saturday=6 (7 also means Sunday);
    # Python's isoweekday() is Monday=1..Sunday=7 -- convert, then fold 7
    # back to 0 so both cron spellings of Sunday are recognized.
    dow_values = _parse_cron_field(dow_f, 0, 7)
    if 7 in dow_values:
        dow_values.discard(7)
        dow_values.add(0)
    cron_dow = when.isoweekday() % 7
    dow_match = cron_dow in dow_values

    if dom_restricted and dow_restricted:
        return dom_match or dow_match
    if dom_restricted:
        return dom_match
    if dow_restricted:
        return dow_match
    return True


def _is_step_due(step: dict[str, Any], now: datetime, log: logging.LoggerAdapter | logging.Logger) -> bool:
    schedule = step.get("schedule")
    if not schedule:
        return True
    try:
        return matches_cron_schedule(schedule, now)
    except ValueError as exc:
        log.error("Step=%s has an invalid schedule %r (%s) -- treating as not due", step.get("name"), schedule, exc)
        return False


class _RunIdAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict) -> tuple:
        kwargs.setdefault("extra", {})["pipeline_run_id"] = self.extra["pipeline_run_id"]
        return msg, kwargs


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser so this bare host script sees the same
    settings the containers get via docker-compose's env_file -- there's no
    docker-compose here to do it automatically."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    # Same forms Docker Compose accepts: KEY=value, KEY: value, export KEY=...,
    # optionally quoted. The first value seen for a key wins.
    pattern = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*)$")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values.setdefault(key, value)
    return values


def _load_env_file(path: Path) -> None:
    for key, value in _read_env_file(path).items():
        os.environ.setdefault(key, value)


# The environment every step inherits; set by _prepare_environment().
_STEP_BASE_ENV: dict[str, str] | None = None


def _prepare_environment(common_dir: Path) -> dict[str, str]:
    """Loads common/.env into this process, then overlays common/.env.host
    for this process only, and returns the environment for steps.

    .env.host holds host-only settings (TELEMETRY_MYSQL_HOST=127.0.0.1,
    port 53306) for this script's own telemetry writes. Steps must not
    inherit them: the repos' compose files take TELEMETRY_MYSQL_* from the
    calling environment first, so every container job was pointed at
    127.0.0.1:53306 -- itself -- and its metering writes failed. Host-side
    steps (common/scripts/run_job_common.sh) read .env.host themselves.
    A value already in the environment (cron's) still wins over both files.
    """
    host_only = {k: v for k, v in _read_env_file(common_dir / ".env.host").items() if k not in os.environ}
    _load_env_file(common_dir / ".env")
    step_env = os.environ.copy()
    os.environ.update(host_only)
    return step_env


def _mysql_connection():
    if mysql is None:
        raise RuntimeError("mysql-connector-python is not installed; run common/scripts/setup_host_python.sh")
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


DEFAULT_STEP_TIMEOUT_MINUTES = 90
TIMEOUT_EXIT_CODE = 124


def _job_containers(project: str) -> set[str]:
    """IDs of running one-off `docker compose run` containers of a compose
    project (a repo's job containers). Empty if Docker isn't reachable."""
    try:
        out = subprocess.run(
            ["docker", "ps", "-q", "--no-trunc",
             "--filter", f"label=com.docker.compose.project={project}",
             "--filter", "label=com.docker.compose.oneoff=True"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(out.stdout.split()) if out.returncode == 0 else set()


def _run_with_timeout(cmd: list[str], env: dict[str, str], step: dict[str, Any], log: logging.LoggerAdapter) -> int:
    """Runs one step invocation, killing it after the step's timeout_minutes
    (default DEFAULT_STEP_TIMEOUT_MINUTES). A job that never exits used to
    hold its container and memory for days (dealmoon1, 2026-09). On timeout
    the whole process group gets SIGTERM, then SIGKILL after 30 s, and any
    job container this invocation started is stopped. Returns the exit code,
    or TIMEOUT_EXIT_CODE."""
    timeout_s = float(step.get("timeout_minutes", DEFAULT_STEP_TIMEOUT_MINUTES)) * 60
    project = str(step["script"]).split("/", 1)[0]
    containers_before = _job_containers(project)
    proc = subprocess.Popen(cmd, env=env, start_new_session=True)
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        log.error("Step timed out step=%s after %.0f min; stopping it", step["name"], timeout_s / 60)
        for sig, grace in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        for container in sorted(_job_containers(project) - containers_before):
            log.error("Stopping job container left by timed-out step=%s container=%s", step["name"], container[:12])
            subprocess.run(["docker", "stop", container], capture_output=True, timeout=60)
        return TIMEOUT_EXIT_CODE


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
        env = dict(_STEP_BASE_ENV if _STEP_BASE_ENV is not None else os.environ)
        env["JOB_RUN_ID"] = job_run_id
        env["PIPELINE_RUN_ID"] = pipeline_run_id
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
        returncode = _run_with_timeout(cmd, env, step, log)
        link_job_run(pipeline_run_id, job_run_id)

        if returncode != 0:
            log.error(
                "Step failed step=%s iteration=%s/%s job_run_id=%s exit_code=%s",
                name,
                iteration,
                repeat,
                job_run_id,
                returncode,
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

    global _STEP_BASE_ENV
    _STEP_BASE_ENV = _prepare_environment(COMMON_DIR)

    pipeline_file = Path(args.pipeline_file)
    config = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
    sequence = config["sequence"]
    pipeline_name = pipeline_file.stem

    # Created before we even know whether anything is due: every log call
    # in this module goes through a formatter that requires pipeline_run_id
    # on the record (see logging.basicConfig below), including from
    # _is_step_due() logging a malformed schedule -- so `log` has to exist
    # before that filtering step runs, not after.
    pipeline_run_id = str(uuid.uuid4())
    log = _RunIdAdapter(logger, {"pipeline_run_id": pipeline_run_id})

    now = datetime.now()  # local time, matching what a crontab entry means on this host
    due_sequence = [step for step in sequence if _is_step_due(step, now, log)]
    if not due_sequence:
        log.info(
            "No steps scheduled for %s -- nothing to do this invocation (pipeline_name=%s, %s/%s steps have a schedule)",
            now.strftime("%Y-%m-%d %H:%M"),
            pipeline_name,
            sum(1 for step in sequence if step.get("schedule")),
            len(sequence),
        )
        return 0

    started_at = datetime.now(timezone.utc)

    create_pipeline_run(pipeline_run_id, pipeline_name, len(due_sequence), args.trigger_source)
    log.info(
        "Pipeline run started pipeline_name=%s total_steps=%s (of %s in file)",
        pipeline_name,
        len(due_sequence),
        len(sequence),
    )

    any_step_failed = False
    failed_step_name: str | None = None
    error_summary: str | None = None

    try:
        for index, step in enumerate(due_sequence, start=1):
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
    return 0 if status == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
