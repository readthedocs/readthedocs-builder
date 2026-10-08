"""Locating the project's ``.readthedocs.yaml`` and resolving ``build.os``."""

import os

from builder.constants_docker import resolve_build_os_alias

from worker import constants


def find_config_file(dest: str, yaml_path: str | None = None) -> str | None:
    """
    Return path to the config file that landed, or ``None``.

    With ``yaml_path`` set, only that path is considered — no fallback to the
    default names, matching ``builder.config.load``.
    """
    if yaml_path:
        candidate = os.path.join(dest, yaml_path)
        return candidate if os.path.isfile(candidate) else None

    for name in constants.CONFIG_FILENAMES:
        candidate = os.path.join(dest, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def resolve_build_os(build_os: str | None) -> str:
    """
    Concrete OS tag for a ``build.os`` value, or the default when unset.

    Resolves the ``ubuntu-lts-latest`` alias via ``RTD_DOCKER_BUILD_SETTINGS``
    so the rest of the pipeline only ever sees a concrete tag.
    """
    return resolve_build_os_alias(build_os or constants.DEFAULT_BUILD_OS)
