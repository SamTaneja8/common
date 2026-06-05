from __future__ import annotations

import argparse
import json

from common_utils.metering import JobMeter, ensure_metering_table, shell_mark_failure


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--job-name", required=True)
    start_parser.add_argument("--service-name", required=True)
    start_parser.add_argument("--trigger-source", default="shell")
    start_parser.add_argument("--arguments-json", default="{}")
    start_parser.add_argument("--step-name", default="Shell Invocation")
    start_parser.add_argument("--detail-message", default=None)

    fail_parser = subparsers.add_parser("shell-fail")
    fail_parser.add_argument("--job-name", required=True)
    fail_parser.add_argument("--service-name", required=True)
    fail_parser.add_argument("--run-id", required=True)
    fail_parser.add_argument("--exit-code", required=True, type=int)
    fail_parser.add_argument("--detail-message", required=True)

    args = parser.parse_args()
    ensure_metering_table()

    if args.command == "start":
        meter = JobMeter(
            job_name=args.job_name,
            service_name=args.service_name,
            trigger_source=args.trigger_source,
            parameters=json.loads(args.arguments_json),
        )
        meter.start(step_name=args.step_name, detail_message=args.detail_message)
        return 0

    shell_mark_failure(
        run_uuid=args.run_id,
        job_name=args.job_name,
        service_name=args.service_name,
        exit_code=args.exit_code,
        detail_message=args.detail_message,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
