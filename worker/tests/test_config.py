from builder.constants_docker import RTD_DOCKER_BUILD_SETTINGS

from worker.config import resolve_build_os


def test_resolve_build_os_returns_a_concrete_os_as_is():
    assert resolve_build_os("ubuntu-24.04") == "ubuntu-24.04"


def test_resolve_build_os_resolves_the_ubuntu_lts_latest_alias():
    latest = RTD_DOCKER_BUILD_SETTINGS["os"]["ubuntu-lts-latest"].split(":", 1)[1]

    assert resolve_build_os("ubuntu-lts-latest") == latest
