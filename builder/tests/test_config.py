"""
Tests for the ``.readthedocs.yaml`` parser.

Ported from ``readthedocs/config/tests/test_config.py``. Differences from
upstream, all intentional:

- ``load()`` takes no second positional argument (upstream passes the project's
  ``readthedocs_yaml_path`` as a dict); here it's an optional keyword.
- No ``override_settings(DOCROOT=...)``: upstream routes reads through
  ``safe_open`` for symlink containment, the builder uses plain ``open()``
  until ``builder.filesystem`` is ported.
- The v1 config keys (``build.image``, ``python.version``,
  ``python.system_packages``) were dropped in the port, so upstream's tests for
  them are replaced by tests asserting they're now rejected as unknown keys.
"""

import os
import re

import pytest
from conftest import apply_fs
from conftest import get_build_config
from pytest import raises

from builder.config import ALL
from builder.config import PIP
from builder.config import SETUPTOOLS
from builder.config import UV
from builder.config import BuildConfigV2
from builder.config import load
from builder.config.config import CONFIG_FILENAME_REGEX
from builder.config.exceptions import ConfigError
from builder.config.exceptions import ConfigValidationError
from builder.exceptions import SymlinkOutsideBasePath
from builder.config.models import BuildJobs
from builder.config.models import BuildJobsBuildTypes
from builder.config.models import PythonInstall
from builder.config.models import PythonInstallRequirements
from builder.config.models import UvInstall


# ---------------------------------------------------------------------------
# Loading the config file
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _docroot_at_tmpdir(tmpdir, monkeypatch):
    # ``load()`` reads the config through ``safe_open``, which enforces DOCROOT
    # containment. Point DOCROOT at the per-test tmpdir so config files written
    # there pass the check (in production the checkout lives under DOCROOT).
    monkeypatch.setattr("builder.settings.DOCROOT", str(tmpdir))


@pytest.mark.parametrize(
    "files",
    [
        {"readthedocs.ymlmore": ""},
        {"first": {"readthedocs.yml": ""}},
        {"startreadthedocs.yml": ""},
        {"second": {"confuser.txt": "content"}},
        {"noroot": {"readthedocs.ymlmore": ""}},
        {"third": {"readthedocs.yml": "content", "Makefile": ""}},
        {"noroot": {"startreadthedocs.yml": ""}},
        {"fourth": {"samplefile.yaml": "content"}},
        {"readthebots.yaml": ""},
        {"fifth": {"confuser.txt": "", "readthedocs.yml": "content"}},
    ],
)
def test_load_no_config_file(tmpdir, files):
    apply_fs(tmpdir, files)
    with raises(ConfigError) as e:
        load(str(tmpdir))
    assert e.value.message_id == ConfigError.DEFAULT_PATH_NOT_FOUND


def test_load_empty_config_file(tmpdir):
    apply_fs(tmpdir, {"readthedocs.yml": ""})
    with raises(ConfigError):
        load(str(tmpdir))


def test_load_rejects_config_symlinked_outside_the_repo(tmpdir):
    # A ``.readthedocs.yaml`` that is a symlink resolving outside the repo
    # checkout must be refused (path-traversal via symlink, GHSA-368m-86q9-m99w).
    secret = tmpdir.join("secret.yaml")
    secret.write("version: 2\nbuild:\n  os: ubuntu-22.04\n  tools:\n    python: '3'\n")
    repo = tmpdir.mkdir("repo")
    os.symlink(str(secret), str(repo.join(".readthedocs.yaml")))

    with raises(SymlinkOutsideBasePath):
        load(str(repo))


def test_load_version2(tmpdir):
    apply_fs(
        tmpdir,
        {
            "readthedocs.yml": """\
version: "2"
build:
  os: ubuntu-22.04
  tools:
    python: "3"
sphinx:
  configuration: docs/conf.py
"""
        },
    )
    build = load(str(tmpdir))
    assert isinstance(build, BuildConfigV2)


def test_load_unsupported_version(tmpdir):
    apply_fs(tmpdir, {"readthedocs.yml": "version: 3"})
    with raises(ConfigError) as excinfo:
        load(str(tmpdir))
    assert excinfo.value.message_id == ConfigError.INVALID_VERSION


def test_load_defaults_to_version_2_when_unset(tmpdir):
    apply_fs(
        tmpdir,
        {
            "readthedocs.yml": """\
build:
  os: ubuntu-22.04
  tools:
    python: "3"
sphinx:
  configuration: docs/conf.py
"""
        },
    )
    assert isinstance(load(str(tmpdir)), BuildConfigV2)


def test_load_reports_invalid_syntax(tmpdir):
    apply_fs(tmpdir, {"readthedocs.yml": "- - !asdf"})
    with raises(ConfigError) as excinfo:
        load(str(tmpdir))
    assert excinfo.value.message_id == ConfigError.SYNTAX_INVALID


def test_load_uses_an_explicit_yaml_path(tmpdir):
    apply_fs(
        tmpdir,
        {
            "docs": {
                "custom.yaml": """\
version: "2"
build:
  os: ubuntu-22.04
  tools:
    python: "3"
sphinx:
  configuration: docs/conf.py
"""
            },
        },
    )
    build = load(str(tmpdir), readthedocs_yaml_path="docs/custom.yaml")
    assert isinstance(build, BuildConfigV2)


def test_load_does_not_fall_back_when_the_explicit_yaml_path_is_missing(tmpdir):
    # A root config exists, but an explicit path was requested — it must not
    # silently fall back to the root one.
    apply_fs(tmpdir, {"readthedocs.yml": "version: 2"})
    with raises(ConfigError) as excinfo:
        load(str(tmpdir), readthedocs_yaml_path="docs/custom.yaml")
    assert excinfo.value.message_id == ConfigError.CONFIG_PATH_NOT_FOUND


@pytest.mark.parametrize(
    "filename",
    [".readthedocs.yaml", ".readthedocs.yml", "readthedocs.yaml", "readthedocs.yml"],
)
def test_config_filename_regex_matches_supported_names(filename):
    assert re.match(CONFIG_FILENAME_REGEX, filename)


@pytest.mark.parametrize("filename", ["readthedocs.txt", "rtd.yaml", "readthedocs.yamlmore"])
def test_config_filename_regex_rejects_other_names(filename):
    assert not re.match(CONFIG_FILENAME_REGEX, filename)


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def test_version_is_two():
    build = get_build_config({})
    assert build.version == "2"


# ---------------------------------------------------------------------------
# base_path resolution
# ---------------------------------------------------------------------------


def test_base_path_defaults_to_the_source_file_directory(tmpdir):
    build = get_build_config({}, source_file=str(tmpdir.join("readthedocs.yml")))
    assert build.base_path == str(tmpdir)


def test_base_path_is_the_source_file_itself_when_it_is_a_directory(tmpdir):
    # A directory source_file means the config came from somewhere other than a
    # file in the repo, so it doubles as the base path.
    build = get_build_config({}, source_file=str(tmpdir))
    assert build.base_path == str(tmpdir)


def test_base_path_can_be_overridden(tmpdir):
    build = get_build_config(
        {},
        source_file=str(tmpdir.join("readthedocs.yml")),
        base_path="/explicit/base",
    )
    assert build.base_path == "/explicit/base"


# ---------------------------------------------------------------------------
# formats
# ---------------------------------------------------------------------------


def test_formats_default_is_empty():
    build = get_build_config({}, validate=True)
    assert build.formats == []


def test_formats_accepts_valid_values():
    build = get_build_config({"formats": ["htmlzip", "pdf", "epub"]}, validate=True)
    assert build.formats == ["htmlzip", "pdf", "epub"]


def test_formats_all_expands_to_every_format():
    build = get_build_config({"formats": "all"}, validate=True)
    assert build.formats == ["htmlzip", "pdf", "epub"]


def test_formats_accepts_an_empty_list():
    build = get_build_config({"formats": []}, validate=True)
    assert build.formats == []


def test_formats_rejects_an_unknown_format():
    build = get_build_config({"formats": ["invalid"]})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


def test_formats_rejects_a_string_other_than_all():
    build = get_build_config({"formats": "pdf"})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST


# ---------------------------------------------------------------------------
# conda
# ---------------------------------------------------------------------------


def test_conda_is_none_by_default():
    build = get_build_config({}, validate=True)
    assert build.conda is None


def test_conda_accepts_an_environment_file(tmpdir):
    apply_fs(tmpdir, {"environment.yml": ""})
    build = get_build_config(
        {"conda": {"environment": "environment.yml"}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.conda.environment == "environment.yml"


def test_conda_requires_the_environment_key():
    build = get_build_config({"conda": {}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.VALUE_NOT_FOUND


def test_conda_rejects_a_non_dict():
    build = get_build_config({"conda": ["environment.yml"]})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_DICT


@pytest.mark.parametrize("tool", ["miniconda-latest", "mambaforge-latest", "miniforge3-latest"])
def test_conda_key_is_required_when_using_a_conda_interpreter(tool, tmpdir):
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": tool}}},
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.CONDA_KEY_REQUIRED


def test_conda_key_is_not_required_when_using_build_commands(tmpdir):
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "miniconda-latest"},
                "commands": ["echo 'hello'"],
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.conda is None


@pytest.mark.parametrize(
    "tool, interpreter",
    [
        ("3", "python"),
        ("miniconda-latest", "conda"),
        ("miniforge3-latest", "conda"),
        ("mambaforge-latest", "mamba"),
    ],
)
def test_python_interpreter_is_derived_from_the_python_tool(tool, interpreter, tmpdir):
    apply_fs(tmpdir, {"environment.yml": ""})
    build = get_build_config(
        {
            "build": {"os": "ubuntu-22.04", "tools": {"python": tool}},
            "conda": {"environment": "environment.yml"},
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.python_interpreter == interpreter


# ---------------------------------------------------------------------------
# build.os / build.tools
# ---------------------------------------------------------------------------


def test_build_accepts_a_valid_os_and_tools():
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3.10"}}},
        validate=True,
    )
    assert build.build.os == "ubuntu-22.04"
    assert build.build.tools["python"].version == "3.10"


def test_build_resolves_the_full_tool_version():
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3.10"}}},
        validate=True,
    )
    # ``full_version`` is the concrete asdf version, not the alias the user wrote.
    assert build.build.tools["python"].full_version.startswith("3.10.")


def test_build_rejects_a_non_dict():
    build = get_build_config({"build": "ubuntu-22.04"})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_DICT


def test_build_requires_the_os_key():
    build = get_build_config({"build": {"tools": {"python": "3"}}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.VALUE_NOT_FOUND


def test_build_rejects_an_unknown_os():
    build = get_build_config({"build": {"os": "ubuntu-9999", "tools": {"python": "3"}}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


def test_build_rejects_an_unknown_tool():
    build = get_build_config({"build": {"os": "ubuntu-22.04", "tools": {"cobol": "85"}}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


def test_build_rejects_an_unknown_tool_version():
    build = get_build_config({"build": {"os": "ubuntu-22.04", "tools": {"python": "2.6"}}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


def test_build_requires_tools_or_commands():
    build = get_build_config({"build": {"os": "ubuntu-22.04"}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.NOT_BUILD_TOOLS_OR_COMMANDS


@pytest.mark.parametrize(
    "tools",
    [
        {"python": "3"},
        {"nodejs": "20"},
        {"ruby": "3.3"},
        {"rust": "latest"},
        {"golang": "1.20"},
        {"python": "3", "nodejs": "20", "rust": "latest"},
    ],
)
def test_build_accepts_every_supported_tool(tools):
    build = get_build_config({"build": {"os": "ubuntu-22.04", "tools": tools}}, validate=True)
    assert set(build.build.tools.keys()) == set(tools.keys())


def test_docker_image_is_derived_from_the_os():
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}}},
        validate=True,
    )
    assert build.docker_image == "readthedocs/build:ubuntu-22.04"


# ---------------------------------------------------------------------------
# build.commands
# ---------------------------------------------------------------------------


def test_build_commands_are_accepted():
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "commands": ["echo 'hello'"]}},
        validate=True,
    )
    assert build.build.commands == ["echo 'hello'"]


def test_build_commands_may_omit_tools():
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "commands": ["echo 'hello'"]}},
        validate=True,
    )
    assert build.build.commands == ["echo 'hello'"]
    assert build.build.tools == {}


def test_build_commands_rejects_a_non_list():
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "commands": "echo 'hello'"}}
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST


def test_build_commands_rejects_a_non_string_command():
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "commands": [123]}}
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_STRING


def test_build_commands_cannot_be_combined_with_jobs():
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "commands": ["echo 'hello'"],
                "jobs": {"post_install": ["echo 'hello'"]},
            }
        }
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.BUILD_JOBS_AND_COMMANDS


def test_build_commands_sets_a_generic_doctype():
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "commands": ["echo 'hello'"]}},
        validate=True,
    )
    assert build.doctype == "generic"
    assert build.is_using_build_commands is True


# ---------------------------------------------------------------------------
# build.jobs
# ---------------------------------------------------------------------------


def test_build_jobs_defaults_are_empty():
    build = get_build_config({}, validate=True)
    assert build.build.jobs == BuildJobs(build=BuildJobsBuildTypes())


def test_build_jobs_accepts_the_documented_hooks():
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {
                    "pre_checkout": ["echo pre_checkout"],
                    "post_checkout": ["echo post_checkout"],
                    "pre_system_dependencies": ["echo pre_system_dependencies"],
                    "post_system_dependencies": ["echo post_system_dependencies"],
                    "pre_create_environment": ["echo pre_create_environment"],
                    "post_create_environment": ["echo post_create_environment"],
                    "pre_install": ["echo pre_install"],
                    "post_install": ["echo post_install"],
                    "pre_build": ["echo pre_build"],
                    "post_build": ["echo post_build"],
                },
            }
        },
        validate=True,
    )
    assert build.build.jobs.pre_checkout == ["echo pre_checkout"]
    assert build.build.jobs.post_build == ["echo post_build"]


def test_build_jobs_rejects_an_unknown_job():
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"unknown_job": ["echo 'hello'"]},
            }
        }
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


def test_build_jobs_rejects_a_non_list_of_commands():
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"post_install": "echo 'hello'"},
            }
        }
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST


def test_build_jobs_accepts_empty_commands():
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"post_install": []},
            }
        },
        validate=True,
    )
    assert build.build.jobs.post_install == []


def test_build_jobs_build_overrides_html():
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"build": {"html": ["echo html"]}},
            }
        },
        validate=True,
    )
    assert build.build.jobs.build.html == ["echo html"]


def test_build_jobs_build_requires_the_format_to_be_declared():
    # Overriding a non-html build type only makes sense if that format is built.
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"build": {"pdf": ["echo pdf"]}},
            }
        }
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.BUILD_JOBS_BUILD_TYPE_MISSING_IN_FORMATS


def test_build_jobs_build_accepts_a_declared_format():
    build = get_build_config(
        {
            "formats": ["pdf"],
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"build": {"pdf": ["echo pdf"]}},
            },
        },
        validate=True,
    )
    assert build.build.jobs.build.pdf == ["echo pdf"]


def test_build_jobs_build_rejects_an_unknown_build_type():
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"build": {"manpage": ["echo manpage"]}},
            }
        }
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


def test_build_jobs_overriding_a_new_job_sets_a_generic_doctype():
    # Overriding create_environment/install/build without a sphinx or mkdocs
    # key means the user drives the build themselves.
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"build": {"html": ["echo html"]}},
            }
        },
        validate=True,
    )
    assert build.new_jobs_overriden is True
    assert build.doctype == "generic"


# ---------------------------------------------------------------------------
# build.apt_packages
# ---------------------------------------------------------------------------


def test_apt_packages_are_accepted():
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "apt_packages": ["one", "two"],
            }
        },
        validate=True,
    )
    assert build.build.apt_packages == ["one", "two"]


def test_apt_packages_default_to_empty():
    build = get_build_config({}, validate=True)
    assert build.build.apt_packages == []


def test_apt_packages_rejects_a_non_list():
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "apt_packages": "one"}}
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST


@pytest.mark.parametrize("package", ["-quiet", "--force-yes", "/tmp/package.deb", "./package.deb"])
def test_apt_packages_rejects_option_and_path_prefixes(package):
    # These would otherwise smuggle extra options or local files into
    # ``apt-get install``.
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "apt_packages": [package]}}
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.APT_INVALID_PACKAGE_NAME_PREFIX


@pytest.mark.parametrize("package", ["package name", "package;rm -rf /", "package&&ls"])
def test_apt_packages_rejects_names_outside_the_debian_pattern(package):
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "apt_packages": [package]}}
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.APT_INVALID_PACKAGE_NAME


# ---------------------------------------------------------------------------
# python.install
# ---------------------------------------------------------------------------


def test_python_install_defaults_to_empty():
    build = get_build_config({}, validate=True)
    assert build.python.install == []


def test_python_install_rejects_a_non_list():
    build = get_build_config({"python": {"install": "requirements.txt"}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST


def test_python_install_rejects_a_non_dict_python_key():
    build = get_build_config({"python": "3"})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_DICT


def test_python_install_accepts_a_requirements_file(tmpdir):
    apply_fs(tmpdir, {"requirements.txt": ""})
    build = get_build_config(
        {"python": {"install": [{"requirements": "requirements.txt"}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    install = build.python.install[0]
    assert isinstance(install, PythonInstallRequirements)
    assert install.requirements == "requirements.txt"


def test_python_install_rejects_a_null_requirements_file(tmpdir):
    build = get_build_config(
        {"python": {"install": [{"requirements": None}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_STRING


def test_python_install_accepts_a_path_with_pip(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {"python": {"install": [{"path": "package", "method": "pip"}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    install = build.python.install[0]
    assert isinstance(install, PythonInstall)
    assert install.path == "package"
    assert install.method == PIP


def test_python_install_accepts_a_path_with_setuptools(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {"python": {"install": [{"path": "package", "method": "setuptools"}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.python.install[0].method == SETUPTOOLS
    assert build.is_using_setup_py_install is True


def test_python_install_defaults_the_method_to_pip(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {"python": {"install": [{"path": "package"}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.python.install[0].method == PIP


def test_python_install_rejects_an_unknown_method(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {"python": {"install": [{"path": "package", "method": "poetry"}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


def test_python_install_requires_a_path_or_requirements(tmpdir):
    build = get_build_config(
        {"python": {"install": [{"extra_requirements": ["docs"]}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.PIP_PATH_OR_REQUIREMENT_REQUIRED


def test_python_install_accepts_extra_requirements_with_pip(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {
            "python": {
                "install": [{"path": "package", "method": "pip", "extra_requirements": ["docs"]}]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.python.install[0].extra_requirements == ["docs"]


def test_python_install_rejects_extra_requirements_with_setuptools(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {
            "python": {
                "install": [
                    {"path": "package", "method": "setuptools", "extra_requirements": ["docs"]}
                ]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.USE_PIP_FOR_EXTRA_REQUIREMENTS


def test_python_install_preserves_the_order_of_entries(tmpdir):
    apply_fs(tmpdir, {"one": {}, "two": {}, "requirements.txt": ""})
    build = get_build_config(
        {
            "python": {
                "install": [
                    {"path": "one"},
                    {"requirements": "requirements.txt"},
                    {"path": "two"},
                ]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert [type(i) for i in build.python.install] == [
        PythonInstall,
        PythonInstallRequirements,
        PythonInstall,
    ]
    assert build.python.install[0].path == "one"
    assert build.python.install[2].path == "two"


def test_is_using_setup_py_install_is_false_for_pip(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {"python": {"install": [{"path": "package", "method": "pip"}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.is_using_setup_py_install is False


def test_python_install_accepts_an_empty_list():
    build = get_build_config({"python": {"install": []}}, validate=True)
    assert build.python.install == []


# ---------------------------------------------------------------------------
# python.install with uv
# ---------------------------------------------------------------------------


def test_uv_sync_is_accepted():
    build = get_build_config(
        {"python": {"install": [{"method": "uv", "command": "sync"}]}},
        validate=True,
    )
    install = build.python.install[0]
    assert isinstance(install, UvInstall)
    assert install.method == UV
    assert install.command == "sync"
    assert build.is_using_uv is True


def test_uv_sync_accepts_a_path(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {"python": {"install": [{"method": "uv", "command": "sync", "path": "package"}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.python.install[0].path == "package"


def test_uv_sync_accepts_groups():
    build = get_build_config(
        {"python": {"install": [{"method": "uv", "command": "sync", "groups": ["docs"]}]}},
        validate=True,
    )
    assert build.python.install[0].groups == ["docs"]


def test_uv_sync_accepts_all_groups():
    build = get_build_config(
        {"python": {"install": [{"method": "uv", "command": "sync", "groups": "all"}]}},
        validate=True,
    )
    assert build.python.install[0].groups == ALL


def test_uv_sync_accepts_extras():
    build = get_build_config(
        {"python": {"install": [{"method": "uv", "command": "sync", "extras": ["docs"]}]}},
        validate=True,
    )
    assert build.python.install[0].extras == ["docs"]


def test_uv_sync_accepts_all_extras():
    build = get_build_config(
        {"python": {"install": [{"method": "uv", "command": "sync", "extras": "all"}]}},
        validate=True,
    )
    assert build.python.install[0].extras == ALL


def test_uv_sync_rejects_requirements():
    build = get_build_config(
        {
            "python": {
                "install": [
                    {"method": "uv", "command": "sync", "requirements": "requirements.txt"}
                ]
            }
        }
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.UV_SYNC_REQUIREMENTS_INVALID


def test_uv_pip_accepts_requirements(tmpdir):
    apply_fs(tmpdir, {"requirements.txt": ""})
    build = get_build_config(
        {
            "python": {
                "install": [{"method": "uv", "command": "pip", "requirements": "requirements.txt"}]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.python.install[0].requirements == "requirements.txt"


def test_uv_pip_accepts_a_path(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {"python": {"install": [{"method": "uv", "command": "pip", "path": "package"}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.python.install[0].path == "package"


def test_uv_pip_requires_requirements_or_path():
    build = get_build_config({"python": {"install": [{"method": "uv", "command": "pip"}]}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.UV_PIP_REQUIREMENTS_OR_PATH_REQUIRED


def test_uv_pip_rejects_requirements_and_path_together(tmpdir):
    apply_fs(tmpdir, {"package": {}, "requirements.txt": ""})
    build = get_build_config(
        {
            "python": {
                "install": [
                    {
                        "method": "uv",
                        "command": "pip",
                        "path": "package",
                        "requirements": "requirements.txt",
                    }
                ]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.UV_PIP_REQUIREMENTS_AND_PATH_MUTUALLY_EXCLUSIVE


def test_uv_pip_rejects_groups(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {
            "python": {
                "install": [
                    {"method": "uv", "command": "pip", "path": "package", "groups": ["docs"]}
                ]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.UV_PIP_GROUPS_NOT_ALLOWED


def test_uv_pip_accepts_extras(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {
            "python": {
                "install": [
                    {"method": "uv", "command": "pip", "path": "package", "extras": ["docs"]}
                ]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.python.install[0].extras == ["docs"]


def test_uv_requires_a_command():
    build = get_build_config({"python": {"install": [{"method": "uv"}]}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.UV_COMMAND_REQUIRED


def test_uv_rejects_an_unknown_command():
    build = get_build_config({"python": {"install": [{"method": "uv", "command": "install"}]}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


@pytest.mark.parametrize("field", ["groups", "extras"])
def test_uv_sync_rejects_an_empty_group_or_extra_list(field):
    build = get_build_config(
        {"python": {"install": [{"method": "uv", "command": "sync", field: []}]}}
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.UV_GROUPS_EXTRAS_EMPTY


def test_uv_pip_accepts_all_extras(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {
            "python": {
                "install": [{"method": "uv", "command": "pip", "path": "package", "extras": "all"}]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.python.install[0].extras == ALL


def test_uv_pip_rejects_an_empty_extras_list(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {
            "python": {
                "install": [{"method": "uv", "command": "pip", "path": "package", "extras": []}]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.UV_GROUPS_EXTRAS_EMPTY


def test_uv_pip_rejects_a_bad_extras_type(tmpdir):
    apply_fs(tmpdir, {"package": {}})
    build = get_build_config(
        {
            "python": {
                "install": [{"method": "uv", "command": "pip", "path": "package", "extras": "docs"}]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.UV_GROUPS_EXTRAS_INVALID_TYPE


@pytest.mark.parametrize("field", ["groups", "extras"])
def test_uv_sync_rejects_a_bad_group_or_extra_type(field):
    build = get_build_config(
        {"python": {"install": [{"method": "uv", "command": "sync", field: "docs"}]}}
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.UV_GROUPS_EXTRAS_INVALID_TYPE


def test_uv_rejects_multiple_install_entries(tmpdir):
    apply_fs(tmpdir, {"requirements.txt": ""})
    build = get_build_config(
        {
            "python": {
                "install": [
                    {"method": "uv", "command": "sync"},
                    {"requirements": "requirements.txt"},
                ]
            }
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.UV_MULTIPLE_INSTALL_ENTRIES_INVALID


def test_is_using_uv_is_false_for_pip(tmpdir):
    apply_fs(tmpdir, {"requirements.txt": ""})
    build = get_build_config(
        {"python": {"install": [{"requirements": "requirements.txt"}]}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.is_using_uv is False


def test_is_using_uv_is_false_without_any_install():
    build = get_build_config({}, validate=True)
    assert build.is_using_uv is False


# ---------------------------------------------------------------------------
# sphinx
# ---------------------------------------------------------------------------


def test_sphinx_defaults_to_the_html_builder():
    build = get_build_config({}, validate=True)
    assert build.sphinx.builder == "sphinx"
    assert build.doctype == "sphinx"


def test_sphinx_configuration_defaults_to_none():
    build = get_build_config({}, validate=True)
    assert build.sphinx.configuration is None


@pytest.mark.parametrize(
    "builder, expected",
    [
        ("html", "sphinx"),
        ("htmldir", "sphinx_htmldir"),
        ("dirhtml", "sphinx_htmldir"),
        ("singlehtml", "sphinx_singlehtml"),
    ],
)
def test_sphinx_builder_is_mapped_to_the_internal_name(builder, expected):
    build = get_build_config({"sphinx": {"builder": builder}}, validate=True)
    assert build.sphinx.builder == expected


def test_sphinx_rejects_an_unknown_builder():
    build = get_build_config({"sphinx": {"builder": "epub"}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


def test_sphinx_accepts_a_configuration_path(tmpdir):
    apply_fs(tmpdir, {"docs": {"conf.py": ""}})
    build = get_build_config(
        {"sphinx": {"configuration": "docs/conf.py"}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.sphinx.configuration == "docs/conf.py"


def test_sphinx_rejects_a_configuration_that_is_not_conf_py(tmpdir):
    apply_fs(tmpdir, {"docs": {"settings.py": ""}})
    build = get_build_config(
        {"sphinx": {"configuration": "docs/settings.py"}},
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.SPHINX_INVALID_CONFIG_FILE


def test_sphinx_accepts_a_null_configuration():
    build = get_build_config({"sphinx": {"configuration": None}}, validate=True)
    assert build.sphinx.configuration is None


def test_sphinx_fail_on_warning_defaults_to_false():
    build = get_build_config({}, validate=True)
    assert build.sphinx.fail_on_warning is False


def test_sphinx_fail_on_warning_is_accepted():
    build = get_build_config({"sphinx": {"fail_on_warning": True}}, validate=True)
    assert build.sphinx.fail_on_warning is True


def test_sphinx_fail_on_warning_rejects_a_non_bool():
    build = get_build_config({"sphinx": {"fail_on_warning": "yes"}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_BOOL


def test_sphinx_rejects_a_non_dict():
    build = get_build_config({"sphinx": "docs/conf.py"})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_DICT


# ---------------------------------------------------------------------------
# mkdocs
# ---------------------------------------------------------------------------


def test_mkdocs_is_none_by_default():
    build = get_build_config({}, validate=True)
    assert build.mkdocs is None


def test_mkdocs_sets_the_doctype(tmpdir):
    apply_fs(tmpdir, {"mkdocs.yml": ""})
    build = get_build_config(
        {"mkdocs": {"configuration": "mkdocs.yml"}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.mkdocs.configuration == "mkdocs.yml"
    assert build.doctype == "mkdocs"


def test_mkdocs_configuration_defaults_to_none():
    build = get_build_config({"mkdocs": {}}, validate=True)
    assert build.mkdocs.configuration is None


def test_mkdocs_fail_on_warning_defaults_to_false():
    build = get_build_config({"mkdocs": {}}, validate=True)
    assert build.mkdocs.fail_on_warning is False


def test_mkdocs_fail_on_warning_is_accepted():
    build = get_build_config({"mkdocs": {"fail_on_warning": True}}, validate=True)
    assert build.mkdocs.fail_on_warning is True


def test_mkdocs_fail_on_warning_rejects_a_non_bool():
    build = get_build_config({"mkdocs": {"fail_on_warning": "yes"}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_BOOL


def test_mkdocs_rejects_a_non_dict():
    build = get_build_config({"mkdocs": "mkdocs.yml"})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_DICT


def test_mkdocs_and_sphinx_cannot_be_used_together(tmpdir):
    apply_fs(tmpdir, {"mkdocs.yml": "", "docs": {"conf.py": ""}})
    build = get_build_config(
        {
            "mkdocs": {"configuration": "mkdocs.yml"},
            "sphinx": {"configuration": "docs/conf.py"},
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.SPHINX_MKDOCS_CONFIG_TOGETHER


def test_sphinx_is_none_when_mkdocs_is_used(tmpdir):
    apply_fs(tmpdir, {"mkdocs.yml": ""})
    build = get_build_config(
        {"mkdocs": {"configuration": "mkdocs.yml"}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    assert build.sphinx is None


# ---------------------------------------------------------------------------
# submodules
# ---------------------------------------------------------------------------


def test_submodules_default_to_excluding_all():
    build = get_build_config({}, validate=True)
    assert build.submodules.include == []
    assert build.submodules.exclude == ALL
    assert build.submodules.recursive is False


def test_submodules_include_a_list():
    build = get_build_config({"submodules": {"include": ["one", "two"]}}, validate=True)
    assert build.submodules.include == ["one", "two"]
    assert build.submodules.exclude == []


def test_submodules_include_all():
    build = get_build_config({"submodules": {"include": "all"}}, validate=True)
    assert build.submodules.include == ALL
    assert build.submodules.exclude == []


def test_submodules_exclude_a_list():
    build = get_build_config({"submodules": {"exclude": ["one"]}}, validate=True)
    assert build.submodules.exclude == ["one"]
    assert build.submodules.include == []


def test_submodules_exclude_all():
    build = get_build_config({"submodules": {"exclude": "all"}}, validate=True)
    assert build.submodules.exclude == ALL


def test_submodules_cannot_include_and_exclude_together():
    build = get_build_config({"submodules": {"include": ["one"], "exclude": ["two"]}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.SUBMODULES_INCLUDE_EXCLUDE_TOGETHER


def test_submodules_both_empty_is_allowed():
    build = get_build_config({"submodules": {"include": [], "exclude": []}}, validate=True)
    assert build.submodules.include == []
    assert build.submodules.exclude == []


def test_submodules_recursive_is_accepted():
    build = get_build_config(
        {"submodules": {"include": ["one"], "recursive": True}},
        validate=True,
    )
    assert build.submodules.recursive is True


def test_submodules_recursive_rejects_a_non_bool():
    build = get_build_config({"submodules": {"include": ["one"], "recursive": "yes"}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_BOOL


def test_submodules_rejects_a_non_dict():
    build = get_build_config({"submodules": ["one"]})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_DICT


def test_submodules_include_rejects_a_non_list():
    build = get_build_config({"submodules": {"include": "one"}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_defaults():
    build = get_build_config({}, validate=True)
    assert build.search.ranking == {}
    assert build.search.ignore == [
        "search.html",
        "search/index.html",
        "404.html",
        "404/index.html",
    ]


def test_search_ranking_is_accepted():
    build = get_build_config({"search": {"ranking": {"api/*": -5, "index.html": 10}}}, validate=True)
    assert build.search.ranking == {"api/*": -5, "index.html": 10}


def test_search_ranking_normalizes_the_path_pattern():
    build = get_build_config({"search": {"ranking": {"/api//index.html": 5}}}, validate=True)
    assert build.search.ranking == {"api/index.html": 5}


@pytest.mark.parametrize("rank", [-11, 11, 100])
def test_search_ranking_rejects_a_rank_out_of_range(rank):
    build = get_build_config({"search": {"ranking": {"api/*": rank}}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


def test_search_ranking_rejects_a_non_dict():
    build = get_build_config({"search": {"ranking": ["api/*"]}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_DICT


def test_search_ignore_is_accepted():
    build = get_build_config({"search": {"ignore": ["api/*"]}}, validate=True)
    assert build.search.ignore == ["api/*"]


def test_search_ignore_rejects_a_non_list():
    build = get_build_config({"search": {"ignore": "api/*"}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST


def test_search_rejects_a_non_dict():
    build = get_build_config({"search": ["api/*"]})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigValidationError.INVALID_DICT


# ---------------------------------------------------------------------------
# Unknown / removed keys
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_is_rejected():
    build = get_build_config({"unknown": "value"})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.INVALID_KEY_NAME
    assert excinfo.value.format_values["key"] == "unknown"


def test_unknown_nested_key_is_rejected():
    build = get_build_config({"sphinx": {"unknown": "value"}})
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.INVALID_KEY_NAME
    assert excinfo.value.format_values["key"] == "sphinx.unknown"


@pytest.mark.parametrize(
    "config",
    [
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "image": "latest"}},
        {"python": {"version": "3.7"}},
        {"python": {"system_packages": True}},
        {"python": {"use_system_site_packages": True}},
    ],
)
def test_v1_keys_are_rejected_as_unknown(config):
    # The v1 config keys were dropped in the port to the standalone builder;
    # they now fall through to the unknown-key check rather than getting a
    # dedicated deprecation error.
    build = get_build_config(config)
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.INVALID_KEY_NAME


def test_accessing_an_unknown_attribute_raises():
    build = get_build_config({}, validate=True)
    with raises(ConfigError) as excinfo:
        build.not_a_real_key
    assert excinfo.value.message_id == ConfigError.KEY_NOT_SUPPORTED_IN_VERSION


# ---------------------------------------------------------------------------
# pop_config helper
# ---------------------------------------------------------------------------


def test_pop_config_returns_a_top_level_key():
    build = get_build_config({})
    build._raw_config = {"one": 1}
    assert build.pop_config("one") == 1
    assert build._raw_config == {}


def test_pop_config_returns_a_nested_key():
    build = get_build_config({})
    build._raw_config = {"one": {"two": 3}}
    assert build.pop_config("one.two") == 3


def test_pop_config_removes_the_parent_when_it_becomes_empty():
    build = get_build_config({})
    build._raw_config = {"one": {"two": 3}}
    build.pop_config("one.two")
    assert build._raw_config == {}


def test_pop_config_keeps_the_parent_when_siblings_remain():
    build = get_build_config({})
    build._raw_config = {"one": {"two": 3, "four": 5}}
    build.pop_config("one.two")
    assert build._raw_config == {"one": {"four": 5}}


def test_pop_config_defaults_to_none():
    build = get_build_config({})
    build._raw_config = {}
    assert build.pop_config("missing") is None


def test_pop_config_returns_the_given_default():
    build = get_build_config({})
    build._raw_config = {}
    assert build.pop_config("missing", "default") == "default"


def test_pop_config_can_raise_when_missing():
    build = get_build_config({})
    build._raw_config = {}
    with raises(ConfigValidationError) as excinfo:
        build.pop_config("missing", raise_ex=True)
    assert excinfo.value.message_id == ConfigValidationError.VALUE_NOT_FOUND


# ---------------------------------------------------------------------------
# as_dict
# ---------------------------------------------------------------------------


def test_as_dict_serializes_the_public_attributes(tmpdir):
    apply_fs(tmpdir, {"docs": {"conf.py": ""}, "requirements.txt": ""})
    build = get_build_config(
        {
            "formats": ["pdf"],
            "build": {"os": "ubuntu-22.04", "tools": {"python": "3.10"}},
            "python": {"install": [{"requirements": "requirements.txt"}]},
            "sphinx": {"configuration": "docs/conf.py"},
        },
        source_file=str(tmpdir.join("readthedocs.yml")),
        validate=True,
    )
    config = build.as_dict()

    assert config["version"] == "2"
    assert config["formats"] == ["pdf"]
    assert config["build"]["os"] == "ubuntu-22.04"
    assert config["build"]["tools"]["python"]["version"] == "3.10"
    assert config["python"]["install"] == [{"requirements": "requirements.txt"}]
    assert config["sphinx"]["configuration"] == "docs/conf.py"
    assert config["doctype"] == "sphinx"


# ---------------------------------------------------------------------------
# Required doctype configuration
#
# ``sphinx.configuration`` / ``mkdocs.configuration`` are mandatory now that
# the implicit-config deprecation has completed. These tests pass
# ``require_config=True`` because the helper otherwise opts out (see
# ``get_build_config``).
# ---------------------------------------------------------------------------


def test_require_config_is_on_by_default_in_the_class():
    # Production construction (no override) enforces the requirement.
    build = BuildConfigV2(
        {"version": "2", "build": {"os": "ubuntu-22.04", "tools": {"python": "3"}}},
        source_file="readthedocs.yml",
    )
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.SPHINX_CONFIG_MISSING


def test_require_config_can_be_disabled():
    build = get_build_config({}, require_config=False, validate=True)
    assert build.sphinx.configuration is None


def test_config_without_a_doctype_is_rejected():
    build = get_build_config({}, require_config=True)
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.SPHINX_CONFIG_MISSING


def test_sphinx_key_without_a_configuration_is_rejected():
    build = get_build_config({"sphinx": {"fail_on_warning": True}}, require_config=True)
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.SPHINX_CONFIG_MISSING


def test_mkdocs_key_without_a_configuration_is_rejected():
    build = get_build_config({"mkdocs": {"fail_on_warning": True}}, require_config=True)
    with raises(ConfigError) as excinfo:
        build.validate()
    assert excinfo.value.message_id == ConfigError.MKDOCS_CONFIG_MISSING


def test_explicit_sphinx_configuration_satisfies_the_requirement(tmpdir):
    apply_fs(tmpdir, {"docs": {"conf.py": ""}})
    build = get_build_config(
        {"sphinx": {"configuration": "docs/conf.py"}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        require_config=True,
        validate=True,
    )
    assert build.sphinx.configuration == "docs/conf.py"


def test_explicit_mkdocs_configuration_satisfies_the_requirement(tmpdir):
    apply_fs(tmpdir, {"mkdocs.yml": ""})
    build = get_build_config(
        {"mkdocs": {"configuration": "mkdocs.yml"}},
        source_file=str(tmpdir.join("readthedocs.yml")),
        require_config=True,
        validate=True,
    )
    assert build.mkdocs.configuration == "mkdocs.yml"


def test_build_commands_satisfy_the_requirement():
    build = get_build_config(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "commands": ["echo 'hello'"]}},
        require_config=True,
        validate=True,
    )
    assert build.doctype == "generic"


def test_overriding_a_new_job_satisfies_the_requirement():
    build = get_build_config(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"build": {"html": ["echo html"]}},
            }
        },
        require_config=True,
        validate=True,
    )
    assert build.doctype == "generic"
