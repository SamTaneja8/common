from pathlib import Path

from common_utils import env_check
from common_utils.env_check import _load_schema_without_pyyaml, _parse_scalar


def test_parse_scalar_coerces_unquoted_booleans_and_null() -> None:
    assert _parse_scalar("true") is True
    assert _parse_scalar("False") is False
    assert _parse_scalar("null") is None
    assert _parse_scalar("None") is None


def test_parse_scalar_leaves_quoted_boolean_like_strings_as_strings() -> None:
    # A schema author writing `default: "true"` means the literal string
    # "true" (e.g. an env var whose value is compared as text elsewhere),
    # not the YAML boolean -- quoting is exactly how YAML tells them apart.
    assert _parse_scalar('"true"') == "true"
    assert _parse_scalar("'false'") == "false"
    assert _parse_scalar('"null"') == "null"


def test_parse_scalar_strips_quotes_from_ordinary_strings() -> None:
    assert _parse_scalar('"change_me"') == "change_me"
    assert _parse_scalar("'change_me'") == "change_me"
    assert _parse_scalar("change_me") == "change_me"


def test_parse_scalar_empty_value() -> None:
    assert _parse_scalar("") == ""
    assert _parse_scalar("   ") == ""


SAMPLE_SCHEMA = """
repo: sample

variables:
  - name: MYSQL_PASS
    required: true
    placeholder: "change_me"
    description: >-
      Primary MySQL password. Ships as an obvious placeholder in
      .env.example -- must be replaced.

  - name: LOG_TO_CONSOLE
    required: true
    default: "true"

  - name: OPTIONAL_VAR
    required: false
"""


def test_fallback_parser_matches_expected_shape(tmp_path: Path) -> None:
    schema_path = tmp_path / "env_schema.yaml"
    schema_path.write_text(SAMPLE_SCHEMA, encoding="utf-8")

    data = _load_schema_without_pyyaml(schema_path.read_text(encoding="utf-8"))
    variables = {var["name"]: var for var in data["variables"]}

    assert set(variables) == {"MYSQL_PASS", "LOG_TO_CONSOLE", "OPTIONAL_VAR"}

    assert variables["MYSQL_PASS"]["required"] is True
    assert variables["MYSQL_PASS"]["placeholder"] == "change_me"
    # The folded (`>-`) multiline description is skipped rather than
    # mis-parsed -- it's never read by check_required/diff_against_example,
    # so dropping it is fine as long as it doesn't corrupt later keys.
    assert "description" not in variables["MYSQL_PASS"]

    assert variables["LOG_TO_CONSOLE"]["required"] is True
    # Regression: a quoted "true" default must stay the string "true", not
    # get coerced into the Python boolean True.
    assert variables["LOG_TO_CONSOLE"]["default"] == "true"

    assert variables["OPTIONAL_VAR"]["required"] is False


def test_load_schema_uses_fallback_when_pyyaml_unavailable(tmp_path: Path, monkeypatch) -> None:
    schema_path = tmp_path / "env_schema.yaml"
    schema_path.write_text(SAMPLE_SCHEMA, encoding="utf-8")

    monkeypatch.setattr(env_check, "yaml", None)
    variables = env_check.load_schema(schema_path)

    names = {var["name"] for var in variables}
    assert names == {"MYSQL_PASS", "LOG_TO_CONSOLE", "OPTIONAL_VAR"}
