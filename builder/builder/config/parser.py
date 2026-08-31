"""YAML parser for ``.readthedocs.yaml``."""

import yaml


__all__ = ("parse", "ParseError")


class ParseError(Exception):
    """Raised when the config file isn't valid YAML or isn't a non-empty mapping."""


def parse(stream):
    """
    Read ``stream`` and return the parsed configuration dict.

    The document must be valid YAML and a non-empty mapping; anything else
    raises :class:`ParseError`.
    """
    try:
        config = yaml.safe_load(stream)
    except yaml.YAMLError as error:
        raise ParseError(f"YAML: {error}")
    if not isinstance(config, dict):
        raise ParseError("Expected mapping")
    if not config:
        raise ParseError("Empty config")
    return config
