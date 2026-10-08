from worker.config import find_config_file
from worker.config import resolve_build_os


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


def test_resolve_build_os_returns_a_concrete_os_unchanged():
    assert resolve_build_os("ubuntu-24.04") == "ubuntu-24.04"


def test_resolve_build_os_resolves_the_ubuntu_lts_latest_alias():
    assert resolve_build_os("ubuntu-lts-latest") == "ubuntu-26.04"


def test_resolve_build_os_defaults_to_the_latest_lts():
    """No hint from the web side (first build of a version): start on the LTS."""
    assert resolve_build_os(None) == "ubuntu-26.04"
    assert resolve_build_os("") == "ubuntu-26.04"
