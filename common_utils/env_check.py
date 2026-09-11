"""Shared engine for checking a repo's .env against a YAML schema of
required variables. The variable list is inherently repo-specific, so each
repo keeps its own scripts/env_schema.yaml; this module holds the reusable
parsing/checking/CLI logic so it isn't duplicated per repo.

Each repo's own scripts/check_env_config.py should be a thin wrapper --
see dealnews1/scripts/check_env_config.py for the reference shape:

    from common_utils.env_check import run_cli
    if __name__ == "__main__":
        raise SystemExit(run_cli(repo_dir=Path(__file__).resolve().parent.parent))

Host dependency: PyYAML (pip3 install pyyaml) -- not bundled in any repo's
own Docker image (see common/Dockerfile.scraper-base's pip install list),
same stated dependency common/orchestration/run_pipeline.py already has.
Runs directly on the host; no Docker/MySQL/network access needed, since
this only ever reads local text files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser -- deliberately not python-dotenv, so this
    module's only dependency is PyYAML and it works identically whether or
    not a given repo's own container image is anywhere nearby."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_schema(path: Path) -> list[dict[str, Any]]:
    if yaml is None:
        print("PyYAML is required: pip3 install pyyaml", file=sys.stderr)
        raise SystemExit(2)
    if not path.is_file():
        print(f"Schema file not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("variables", [])


def check_required(env_values: dict[str, str], schema: list[dict[str, Any]]) -> list[str]:
    """Flags a required variable that's missing entirely, present but
    empty, or still set to its documented `placeholder` value (copied from
    the repo's own .env.example and never edited -- a plain "is the key
    present" check would miss that case, since every credential in these
    .env.example files ships with an obvious placeholder rather than being
    left blank)."""
    problems: list[str] = []
    for var in schema:
        if not var.get("required"):
            continue
        name = var["name"]
        placeholder = var.get("placeholder")
        if name not in env_values:
            problems.append(f"MISSING    {name} -- not set at all in .env")
            continue
        value = env_values[name]
        if not value:
            problems.append(f"EMPTY      {name} -- present but has no value")
            continue
        if placeholder is not None and value == placeholder:
            problems.append(f"PLACEHOLDER {name} -- still set to the .env.example placeholder ({placeholder!r})")
    return problems


def check_provider_credentials(
    env_values: dict[str, str],
    common_env_path: Path,
    *,
    provider_list_var: str = "PROXY",
    excluded_providers: frozenset[str] = frozenset({"DIRECT"}),
) -> list[str]:
    """For repos whose settings.py loads a shared common/.env in addition
    to their own -- PROXY names providers (e.g. DIRECT,EVOMI,BRIGHTDATA)
    whose actual USERNAME/PASSWORD credentials live in common/.env, not the
    repo's own .env. Checking the repo's own .env alone would never catch a
    provider listed in PROXY with no real credentials configured for it: a
    provider with empty credentials doesn't error at config-load time, it
    just builds a proxy config with blank auth that fails at the point of
    use, indistinguishable then from a real outage. Not every repo uses
    this provider-list pattern (e.g. amazonnew's own single-provider Evomi
    setup keeps its credentials in its own .env, not common/.env) -- pass
    provider_list_var=None from a repo's wrapper to skip this check
    entirely rather than mis-apply it."""
    if provider_list_var is None:
        return []
    raw_value = env_values.get(provider_list_var, "")
    if not raw_value:
        return []
    providers = [p.strip().upper() for p in raw_value.split(",") if p.strip() and p.strip().upper() not in excluded_providers]
    if not providers:
        return []

    common_values = parse_env_file(common_env_path)
    problems: list[str] = []
    for provider in providers:
        for suffix in ("USERNAME", "PASSWORD"):
            key = f"{provider}_{suffix}"
            value = common_values.get(key, "")
            if not value:
                problems.append(
                    f"PROVIDER CRED {key} -- {provider_list_var} includes {provider}, "
                    f"but {key} is missing/empty in {common_env_path}"
                )
    return problems


def diff_against_example(schema: list[dict[str, Any]], example_path: Path) -> list[str]:
    example_values = parse_env_file(example_path)
    schema_names = {var["name"] for var in schema}
    example_names = set(example_values.keys())

    notes: list[str] = []
    for name in sorted(example_names - schema_names):
        notes.append(f"In .env.example but not in the schema: {name}")
    for name in sorted(schema_names - example_names):
        notes.append(f"In the schema but not in .env.example: {name}")
    return notes


def run_cli(
    *,
    repo_dir: Path,
    schema_file: Path | None = None,
    default_env_file: Path | None = None,
    default_example_file: Path | None = None,
    default_common_env_file: Path | None = None,
    provider_list_var: str | None = "PROXY",
    argv: list[str] | None = None,
) -> int:
    """Full CLI: argparse, load, check, print, exit code. Each repo's thin
    scripts/check_env_config.py just calls this with its own defaults --
    schema_file defaults to <repo_dir>/scripts/env_schema.yaml, env/example
    files default to <repo_dir>/.env and <repo_dir>/.env.example, and
    common_env_file defaults to <repo_dir>/../common/.env. Pass
    provider_list_var=None for a repo with no common/.env-split provider
    credentials to check (e.g. reviewgate, aistage)."""
    schema_file = schema_file or (repo_dir / "scripts" / "env_schema.yaml")
    default_env_file = default_env_file or (repo_dir / ".env")
    default_example_file = default_example_file or (repo_dir / ".env.example")
    default_common_env_file = default_common_env_file or (repo_dir.parent / "common" / ".env")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(default_env_file))
    parser.add_argument("--schema", default=str(schema_file))
    parser.add_argument("--example-file", default=str(default_example_file))
    parser.add_argument("--common-env-file", default=str(default_common_env_file))
    parser.add_argument("--diff-example", action="store_true", help="Also report drift between the schema and .env.example")
    args = parser.parse_args(argv)

    env_path = Path(args.env_file)
    schema_path = Path(args.schema)

    schema = load_schema(schema_path)
    if not env_path.is_file():
        print(f"No .env file found at {env_path} -- nothing to check against.", file=sys.stderr)
        return 1

    env_values = parse_env_file(env_path)
    problems = check_required(env_values, schema)
    problems += check_provider_credentials(env_values, Path(args.common_env_file), provider_list_var=provider_list_var)

    required_count = sum(1 for var in schema if var.get("required"))
    print(f"Checked {required_count} required variable(s) in {env_path} against {schema_path}")
    if problems:
        print(f"\n{len(problems)} problem(s) found:\n")
        for problem in problems:
            print(f"  {problem}")
    else:
        print("All required variables are present and configured.")

    if args.diff_example:
        drift = diff_against_example(schema, Path(args.example_file))
        if drift:
            print(f"\n{len(drift)} drift note(s) against {args.example_file}:\n")
            for note in drift:
                print(f"  {note}")

    return 1 if problems else 0
