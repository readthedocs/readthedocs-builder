"""Locating and reading the project's ``.readthedocs.yaml``."""

import os

import yaml
from builder.constants_docker import RTD_DOCKER_BUILD_SETTINGS

from worker import constants
from worker.exceptions import BuildUserError
from worker.exceptions import PreContainerFailure


def _resolve_within(dest: str, candidate: str) -> str | None:
    """
    Resolve ``candidate`` and return it only if it stays inside ``dest``.

    Guards the host against a repo escaping the throwaway sparse checkout to
    make the worker read an arbitrary host file (``build.os`` then flows into
    logs and the docker image tag). Two vectors are closed:

    - a ``readthedocs_yaml_path`` that is absolute or uses ``..`` — ``os.path.join``
      happily returns a path outside ``dest`` for either;
    - a ``.readthedocs.yaml`` committed as a symlink pointing outside the
      checkout — ``os.path.isfile`` follows it.

    ``os.path.realpath`` collapses symlinks and ``..`` so the containment check
    sees where the path actually lands. Mirrors ``builder.config.load``'s
    ``safe_open`` containment (GHSA-368m-86q9-m99w), which the runner-side load
    already enforces; the worker reads the same file first and must match.
    """
    dest_real = os.path.realpath(dest)
    real = os.path.realpath(candidate)
    if real != dest_real and not real.startswith(dest_real + os.sep):
        return None
    return real if os.path.isfile(real) else None


def find_config_file(dest: str, yaml_path: str | None = None) -> str | None:
    """
    Return path to the config file that landed, or ``None``.

    With ``yaml_path`` set, only that path is considered — no fallback to the
    default names, matching ``builder.config.load``. In every case the returned
    path is guaranteed to resolve inside ``dest`` (see :func:`_resolve_within`).
    """
    if yaml_path:
        return _resolve_within(dest, os.path.join(dest, yaml_path))

    for name in constants.CONFIG_FILENAMES:
        found = _resolve_within(dest, os.path.join(dest, name))
        if found:
            return found
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
