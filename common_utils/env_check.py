"""Shared engine for checking a repo's .env against a YAML schema of
required variables. The variable list is inherently repo-specific, so each
repo keeps its own scripts/env_schema.yaml; this module holds the reusable
parsing/checking/CLI logic so it isn't duplicated per repo.

Each repo's own scripts/check_env_config.py should be a thin wrapper --
see dealnews1/scripts/check_env_config.py for the reference shape:

    from common_utils.env_check import run_cli
    if __name__ == "__main__":
        raise SystemExit(run_cli(repo_dir=Path(__file__).resolve().parent.parent))

PyYAML is used when available. A small fallback parser handles the limited
schema shape used by these repos so the check can still run on a fresh host
before dependencies are installed. Runs directly on local text files; no
Docker/MySQL/network access needed.
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
    if not path.is_file():
        print(f"Schema file not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    raw_text = path.read_text(encoding="utf-8")
    if yaml is not None:
        data = yaml.safe_load(raw_text) or {}
    else:
        data = _load_schema_without_pyyaml(raw_text)
    return data.get("variables", [])


def _load_schema_without_pyyaml(raw_text: str) -> dict[str, Any]:
    """Tiny parser for this repo family's env_schema.yaml files.

    It intentionally supports only the subset we use here: top-level repo,
    a top-level variables list, and scalar fields under each `- name:` item.
    Folded/multiline descriptions are ignored after their key line because
    validation never needs their content.
    """
    data: dict[str, Any] = {"variables": []}
    current: dict[str, Any] | None = None
    in_variables = False
    skip_multiline_indent: int | None = None

    for raw_line in raw_text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if skip_multiline_indent is not None:
            if indent > skip_multiline_indent and not line.startswith("- name:"):
                continue
            skip_multiline_indent = None
        if line == "variables:":
            in_variables = True
            continue
        if not in_variables:
            if ":" in line:
                key, _, value = line.partition(":")
                data[key.strip()] = _parse_scalar(value.strip())
            continue
        if line.startswith("- name:"):
            current = {"name": _parse_scalar(line.partition(":")[2].strip())}
            data["variables"].append(current)
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value in {">", ">-", "|", "|-"}:
            skip_multiline_indent = indent
            continue
        current[key] = _parse_scalar(value)
    return data


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    was_quoted = value[0:1] in {'"', "'"} and value[-1:] == value[0] and len(value) >= 2
    if was_quoted:
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    return value


def check_required(env_values: dict[str, str], schema: list[dict[str, Any]]) -> list[str]:
    """Flags a required variable that's missing entirely, present but
    empty, or still set to its documented `placeholder` value (copied from
    the repo's own .env.example and never edited -- a plain "is the key
    present" check would miss that case, since every credential in these
    .env.example files ships with an obvious placeholder rather than being
    left blank)."""
    problems: list[str] = []
    for var in schema:
        if not _is_required(var, env_values):
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


def _is_required(var: dict[str, Any], env_values: dict[str, str]) -> bool:
    if var.get("required"):
        return True
    condition = var.get("required_when")
    if not isinstance(condition, dict):
        return False
    name = str(condition.get("name", "")).strip()
    if not name:
        return False
    value = env_values.get(name, "")
    if "equals" in condition:
        return value == str(condition["equals"])
    if "not_equals" in condition:
        return value != str(condition["not_equals"])
    if condition.get("not_empty"):
        return bool(value)
    return False


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


def _format_env_value(value: str) -> str:
    """Quotes a value for safe placement in a KEY=value line if it contains
    a space or `#` (which would otherwise start an inline comment or split
    the value on re-parsing); left bare otherwise, matching how most
    values already look in these .env/.env.example files."""
    if value and (" " in value or "#" in value):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def render_env(example_path: Path, env_path: Path) -> str:
    """Rebuilds a full .env by walking .env.example line by line -- keeping
    every comment, blank line, and section grouping exactly as written --
    but substituting each variable's value with whatever is actually
    configured in env_path, falling back to .env.example's own value when
    the real .env doesn't set that key at all.

    Useful whenever .env.example gains new variables or gets reorganized
    (new sections, updated comments) and you want a fresh, fully-commented
    .env matching that structure without manually re-merging already-
    configured real values by hand -- e.g. `check_env_config.py
    --render-env > .env.new`. Variables that exist only in the real .env
    and not in .env.example are intentionally left out: .env.example is
    the documented, canonical variable list here, and a value present in
    .env but missing from .env.example is exactly the drift
    diff_against_example() already exists to catch separately.
    """
    if not example_path.is_file():
        print(f"Example file not found: {example_path}", file=sys.stderr)
        raise SystemExit(2)

    real_values = parse_env_file(env_path)
    output_lines: list[str] = []
    for raw_line in example_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output_lines.append(raw_line)
            continue
        key_name = raw_line.partition("=")[0].strip()
        if key_name in real_values:
            output_lines.append(f"{key_name}={_format_env_value(real_values[key_name])}")
        else:
            output_lines.append(raw_line)
    return "\n".join(output_lines) + "\n"


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
    parser.add_argument(
        "--render-env",
        action="store_true",
        help=(
            "Print a new .env to stdout: .env.example's structure/comments/grouping, with each "
            "variable's value taken from the real --env-file when set (falling back to .env.example's "
            "own value otherwise). Prints only the rendered file, nothing else, so it can be redirected "
            "straight into a new .env -- e.g. check_env_config.py --render-env > .env.new"
        ),
    )
    args = parser.parse_args(argv)

    env_path = Path(args.env_file)
    schema_path = Path(args.schema)

    if args.render_env:
        print(render_env(Path(args.example_file), env_path), end="")
        return 0

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
