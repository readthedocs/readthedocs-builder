"""
Tests for the YAML parser.

Ported from ``readthedocs/config/tests/test_parser.py``. Upstream passes a
``StringIO``; the builder's ``load()`` hands ``parse()`` the file contents as a
string, so that's what these exercise.
"""

from pytest import raises

from builder.config.parser import ParseError
from builder.config.parser import parse


def test_parse_empty_config_file():
    with raises(ParseError):
        parse("")


def test_parse_invalid_yaml():
    with raises(ParseError):
        parse("- - !asdf")


def test_parse_empty_mapping():
    # ``{}`` parses as a dict, so it clears the isinstance check and has to be
    # rejected by the emptiness check instead.
    with raises(ParseError):
        parse("{}")


def test_parse_bad_type():
    # A bare scalar parses as a string, not the mapping we require.
    with raises(ParseError):
        parse("Hello")


def test_parse_single_config():
    config = parse("base: path")
    assert isinstance(config, dict)
    assert config["base"] == "path"


def test_parse_null_value():
    assert parse("base: null")["base"] is None


def test_parse_empty_value():
    assert parse("base:")["base"] is None


def test_parse_empty_string_value():
    assert parse('base: ""')["base"] == ""


def test_parse_empty_list():
    assert parse("base: []")["base"] == []


def test_do_not_parse_multiple_configs_in_one_file():
    with raises(ParseError):
        parse(
            """
base: path
---
base: other_path
name: second
nested:
    works: true
        """
        )


def test_parse_rejects_unsafe_yaml_tags():
    # ``safe_load`` refuses to instantiate arbitrary Python objects.
    with raises(ParseError):
        parse("value: !!python/object/apply:os.system ['echo hello']")
