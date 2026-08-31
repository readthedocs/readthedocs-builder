import pytest

from worker.config import find_config_file
from worker.config import read_build_os
from worker.exceptions import BuildUserError
from worker.exceptions import PreContainerFailure


def test_find_config_file_returns_the_default_name(tmp_path, write_config):
    write_config(tmp_path / ".readthedocs.yaml", {"version": 2})

    assert find_config_file(str(tmp_path)) == str(tmp_path / ".readthedocs.yaml")


def test_find_config_file_returns_none_when_no_candidate_exists(tmp_path):
    assert find_config_file(str(tmp_path)) is None


def test_find_config_file_prefers_the_first_candidate_filename(tmp_path, write_config):
    write_config(tmp_path / ".readthedocs.yaml", {"version": 2})
    write_config(tmp_path / "readthedocs.yml", {"version": 2})

    assert find_config_file(str(tmp_path)) == str(tmp_path / ".readthedocs.yaml")


def test_find_config_file_uses_the_custom_yaml_path(tmp_path, write_config):
    write_config(tmp_path / "subpath" / ".readthedocs.yaml", {"version": 2})

    found = find_config_file(str(tmp_path), yaml_path="subpath/.readthedocs.yaml")

    assert found == str(tmp_path / "subpath" / ".readthedocs.yaml")


def test_find_config_file_does_not_fall_back_when_custom_yaml_path_is_missing(
    tmp_path, write_config
):
    """A custom path is used exclusively, matching ``builder.config.load``."""
    write_config(tmp_path / ".readthedocs.yaml", {"version": 2})

    assert find_config_file(str(tmp_path), yaml_path="nope/.readthedocs.yaml") is None


def test_read_build_os_returns_the_configured_os(tmp_path, write_config):
    path = write_config(
        tmp_path / ".readthedocs.yaml", {"version": 2, "build": {"os": "ubuntu-24.04"}}
    )

    assert read_build_os(path) == "ubuntu-24.04"


def test_read_build_os_resolves_the_ubuntu_lts_latest_alias(tmp_path, write_config):
    path = write_config(
        tmp_path / ".readthedocs.yaml", {"version": 2, "build": {"os": "ubuntu-lts-latest"}}
    )

    assert read_build_os(path) == "ubuntu-26.04"


def test_read_build_os_raises_build_os_required_when_missing(tmp_path, write_config):
    path = write_config(
        tmp_path / ".readthedocs.yaml", {"version": 2, "build": {"tools": {"python": "3"}}}
    )

    with pytest.raises(PreContainerFailure) as excinfo:
        read_build_os(path)

    assert excinfo.value.message_id == BuildUserError.BUILD_OS_REQUIRED


def test_read_build_os_raises_no_config_file_when_yaml_is_not_a_mapping(tmp_path, write_config):
    path = write_config(tmp_path / ".readthedocs.yaml", "just a string")

    with pytest.raises(PreContainerFailure) as excinfo:
        read_build_os(path)

    assert excinfo.value.message_id == BuildUserError.NO_CONFIG_FILE_DEPRECATED
