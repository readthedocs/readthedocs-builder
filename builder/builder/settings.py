"""
Build-time configuration for the builder.

Replaces ``django.conf.settings`` reads from the upstream codebase.
"""

import os
import socket


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


# Filesystem layout inside the container.
DOCROOT = os.environ.get("RTD_DOCROOT", "/home/docs/checkouts")

# Instance running this build. Used to claim ``Build.builder``, which the
# healthcheck endpoint matches its ``?builder=`` param against.
RTD_BUILDER_HOSTNAME = os.environ.get("RTD_BUILDER_HOSTNAME") or socket.gethostname()

# Container user split, enforced by ``docker exec --user``: user code runs as
# RTD_DOCKER_USER; ``apt-get`` and other privileged commands run as
# RTD_DOCKER_SUPER_USER.
RTD_DOCKER_USER = os.environ.get("RTD_DOCKER_USER", "docs")
# Numeric ids for the same user. Needed because the runner may have to chown a
# file *for* the build container from a process where that user doesn't exist
# — the dev compose service has no ``docs`` in its own passwd db, so resolving
# by name fails there. Production creates ``docs`` with these ids on the host.
RTD_DOCKER_UID = int(os.environ.get("RTD_DOCKER_UID", "1005"))
RTD_DOCKER_GID = int(os.environ.get("RTD_DOCKER_GID", "205"))
RTD_DOCKER_SUPER_USER = os.environ.get("RTD_DOCKER_SUPER_USER", "root")
RTD_DOCKER_WORKDIR = os.environ.get("RTD_DOCKER_WORKDIR", "/home/docs")

# Mapping of ``build.os`` / ``build.tools`` config-file values to docker
# images and asdf-installed tool versions. The full table lives in
# ``builder.constants_docker`` (Django-free).
from builder.constants_docker import RTD_DOCKER_BUILD_SETTINGS  # noqa: E402  # noqa: F401

# Feature flags.
RTD_ENFORCE_BROWNOUTS_FOR_DEPRECATIONS = _bool(
    "RTD_ENFORCE_BROWNOUTS_FOR_DEPRECATIONS", default=False
)


