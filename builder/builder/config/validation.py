"""
Validation helpers used by the configuration parser.

Ported verbatim from ``readthedocs.config.validation``. Each helper either
returns a normalized value or raises :class:`ConfigValidationError` with a
specific ``message_id`` that maps to a user-facing error message via the
notification system.
"""

import os

from .exceptions import ConfigValidationError


def validate_list(value):
    """Check that ``value`` is iterable and not a string/dict."""
    if isinstance(value, (dict, str)):
        raise ConfigValidationError(
            message_id=ConfigValidationError.INVALID_LIST,
            format_values={"value": value},
        )
    if not hasattr(value, "__iter__"):
        raise ConfigValidationError(
            message_id=ConfigValidationError.INVALID_LIST,
            format_values={"value": value},
        )
    return list(value)


def validate_dict(value):
    """Raise if ``value`` isn't a dict."""
    if not isinstance(value, dict):
        raise ConfigValidationError(
            message_id=ConfigValidationError.INVALID_DICT,
            format_values={"value": value},
        )


def validate_choice(value, choices):
    """Validate that ``value`` is one of ``choices``."""
    choices = validate_list(choices)
    if value not in choices:
        raise ConfigValidationError(
            message_id=ConfigValidationError.INVALID_CHOICE,
            format_values={
                "value": value,
                "choices": ", ".join(map(str, choices)),
            },
        )
    return value


def validate_bool(value):
    """Validate ``value`` is a boolean (or 0/1)."""
    if value not in (0, 1, False, True):
        raise ConfigValidationError(
            message_id=ConfigValidationError.INVALID_BOOL,
            format_values={"value": value},
        )
    return bool(value)


def validate_path(value, base_path):
    """Validate ``value`` is a non-empty string and normalize it relative to ``base_path``."""
    string_value = validate_string(value)
    if not string_value:
        raise ConfigValidationError(
            message_id=ConfigValidationError.INVALID_PATH,
            format_values={"value": value},
        )
    full_path = os.path.join(base_path, string_value)
    rel_path = os.path.relpath(full_path, base_path)
    return rel_path


def validate_path_pattern(value):
    """
    Normalize and validate a path pattern.

    Strips multiple ``/``, expands relatives, and rejects results that escape ``/``.
    """
    path = validate_string(value)
    path = "/" + path.lstrip("/")
    path = os.path.normpath(path)
    if not os.path.isabs(path):
        raise ConfigValidationError(
            message_id=ConfigValidationError.INVALID_PATH_PATTERN,
            format_values={"value": value},
        )
    path = path.lstrip("/")
    if not path:
        raise ConfigValidationError(
            message_id=ConfigValidationError.INVALID_PATH_PATTERN,
            format_values={"value": value},
        )
    return path


def validate_string(value):
    """Validate ``value`` is a string."""
    if not isinstance(value, str):
        raise ConfigValidationError(
            message_id=ConfigValidationError.INVALID_STRING,
            format_values={"value": value},
        )
    return str(value)
