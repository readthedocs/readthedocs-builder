"""Resolving the ``build.os`` value into a concrete build image tag."""

from builder.constants_docker import RTD_DOCKER_BUILD_SETTINGS


def resolve_build_os(build_os: str) -> str:
    """
    Resolve the ``ubuntu-lts-latest`` alias via ``RTD_DOCKER_BUILD_SETTINGS``.

    The image tag is built from the OS name, so the alias has to become a
    concrete OS (``ubuntu-26.04``) before it reaches Docker.
    """
    if build_os == "ubuntu-lts-latest":
        alias = RTD_DOCKER_BUILD_SETTINGS["os"].get("ubuntu-lts-latest", "")
        if ":" in alias:
            return alias.split(":", 1)[1]
    return build_os
