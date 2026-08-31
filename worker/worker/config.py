"""Locating and reading the project's ``.readthedocs.yaml``."""

import os

import yaml
from builder.constants_docker import RTD_DOCKER_BUILD_SETTINGS

from worker import constants
from worker.exceptions import BuildUserError
from worker.exceptions import PreContainerFailure


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


def read_build_os(config_path: str) -> str:
    """
    Parse ``.readthedocs.yaml`` and return the ``build.os`` value.

    Resolves the ``ubuntu-lts-latest`` alias via ``RTD_DOCKER_BUILD_SETTINGS``
    so the rest of the pipeline only ever sees a concrete OS tag.
    """
    with open(config_path) as fh:
        config = yaml.safe_load(fh)

    if not isinstance(config, dict):
        raise PreContainerFailure(BuildUserError.NO_CONFIG_FILE_DEPRECATED)

    build_os = (config.get("build") or {}).get("os")
    if not build_os:
        raise PreContainerFailure(BuildUserError.BUILD_OS_REQUIRED)

    if build_os == "ubuntu-lts-latest":
        alias = RTD_DOCKER_BUILD_SETTINGS["os"].get("ubuntu-lts-latest", "")
        if ":" in alias:
            build_os = alias.split(":", 1)[1]

    return build_os
