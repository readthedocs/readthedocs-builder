"""
Read the Docs configuration parser.

Ported from ``readthedocs.config``. The Django-side notification registration
that upstream's ``__init__`` performs as a side effect is dropped — message
ids are still defined on the ``ConfigError`` / ``ConfigValidationError``
classes and attached to builds via the API at runtime.
"""

from .config import ALL
from .config import CONFIG_FILENAME_REGEX
from .config import LATEST_CONFIGURATION_VERSION
from .config import PIP
from .config import SETUPTOOLS
from .config import UV
from .config import BuildConfigV2
from .config import load
from .exceptions import ConfigError
from .exceptions import ConfigValidationError
from .find import find_one
from .parser import ParseError
from .parser import parse


__all__ = (
    "ALL",
    "BuildConfigV2",
    "CONFIG_FILENAME_REGEX",
    "ConfigError",
    "ConfigValidationError",
    "LATEST_CONFIGURATION_VERSION",
    "PIP",
    "ParseError",
    "SETUPTOOLS",
    "UV",
    "find_one",
    "load",
    "parse",
)
