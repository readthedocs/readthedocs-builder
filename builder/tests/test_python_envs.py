"""
Tests for the python (language) environments.

No dedicated upstream test file exists for
``readthedocs.doc_builder.python_environments`` — it's covered end-to-end in
``projects/tests/test_build_tasks.py``. These are fresh command-sequence tests
in the same mock-``run`` style used for the doc backends: patch
``build_env.run`` and assert the argv each method emits.
"""

from unittest import mock

import pytest
from conftest import make_python_env

from builder.config.models import PythonInstall
from builder.config.models import PythonInstallRequirements
from builder.config.models import UvInstall
from builder.exceptions import SymlinkOutsideBasePath
from builder.exceptions import UserFileNotFound
from builder.python_envs import Conda
from builder.python_envs import PythonEnvironment
from builder.python_envs import UvEnv
from builder.python_envs import Virtualenv


SPHINX = {"sphinx": {"configuration": "conf.py"}}


def _capture_run(env):
    """Patch ``env.build_env.run`` with a mock and return it for inspection."""
    fake = mock.MagicMock(return_value=mock.MagicMock(successful=True, output=""))
    return mock.patch.object(env.build_env, "run", fake), fake


def _argvs(fake):
    """All positional argv tuples captured across ``run`` calls."""
    return [call.args for call in fake.call_args_list]


# ---------------------------------------------------------------------------
# PythonEnvironment base
# ---------------------------------------------------------------------------


def test_python_environment_requires_a_config(docroot):
    from builder.api_models import APIVersion
    from builder.environments import BuildEnvironment

    version = APIVersion(slug="latest", type="branch", project={"slug": "pip", "name": "Pip"})
    env = BuildEnvironment(project=version.project, version=version, record=False)
    with pytest.raises(ValueError):
        PythonEnvironment(version=version, build_env=env, config=None)


# ---------------------------------------------------------------------------
# venv_bin
# ---------------------------------------------------------------------------


def test_virtualenv_venv_bin(docroot):
    env = make_python_env(Virtualenv, SPHINX)
    assert env.venv_bin() == "$READTHEDOCS_VIRTUALENV_PATH/bin"
    assert env.venv_bin(filename="python") == "$READTHEDOCS_VIRTUALENV_PATH/bin/python"


def test_conda_venv_bin(docroot):
    (docroot / "environment.yml").write_text("")
    env = make_python_env(
        Conda,
        {
            "build": {"os": "ubuntu-22.04", "tools": {"python": "miniconda-latest"}},
            "conda": {"environment": "environment.yml"},
        },
        source_file=str(docroot / "readthedocs.yml"),
    )
    assert env.venv_bin(filename="python") == "$CONDA_ENVS_PATH/$CONDA_DEFAULT_ENV/bin/python"


# ---------------------------------------------------------------------------
# Virtualenv
# ---------------------------------------------------------------------------


def test_virtualenv_setup_base(docroot):
    env = make_python_env(Virtualenv, SPHINX)
    patcher, fake = _capture_run(env)
    with patcher:
        env.setup_base()
    argv = fake.call_args[0]
    assert argv == ("python", "-mvirtualenv", "$READTHEDOCS_VIRTUALENV_PATH")


def test_virtualenv_core_requirements_installs_pip_then_sphinx(docroot):
    env = make_python_env(Virtualenv, SPHINX)
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_core_requirements()
    argvs = _argvs(fake)
    # Two passes: upgrade pip+setuptools, then install sphinx.
    assert argvs[0][-2:] == ("pip", "setuptools")
    assert argvs[1][-1] == "sphinx"


def test_virtualenv_core_requirements_installs_mkdocs_for_mkdocs_doctype(docroot):
    (docroot / "mkdocs.yml").write_text("")
    env = make_python_env(
        Virtualenv,
        {"mkdocs": {"configuration": "mkdocs.yml"}},
        source_file=str(docroot / "readthedocs.yml"),
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_core_requirements()
    assert _argvs(fake)[1][-1] == "mkdocs"


def test_virtualenv_core_requirements_generic_only_upgrades_pip(docroot):
    env = make_python_env(
        Virtualenv,
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "commands": ["echo hi"]}},
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_core_requirements()
    # Generic builds bring their own deps: only the pip/setuptools upgrade runs.
    argvs = _argvs(fake)
    assert len(argvs) == 1
    assert argvs[0][-2:] == ("pip", "setuptools")


def test_virtualenv_install_requirements_file(docroot):
    (docroot / "requirements.txt").write_text("")
    env = make_python_env(
        Virtualenv,
        {"python": {"install": [{"requirements": "requirements.txt"}]}, **SPHINX},
        source_file=str(docroot / "readthedocs.yml"),
    )
    install = env.config.python.install[0]
    assert isinstance(install, PythonInstallRequirements)
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_requirements_file(install)
    argv = fake.call_args[0]
    assert argv[-2:] == ("-r", "requirements.txt")
    assert "--upgrade" not in argv


def test_virtualenv_install_requirements_file_upgrades_with_feature(docroot):
    (docroot / "requirements.txt").write_text("")
    env = make_python_env(
        Virtualenv,
        {"python": {"install": [{"requirements": "requirements.txt"}]}, **SPHINX},
        source_file=str(docroot / "readthedocs.yml"),
        features=["pip_always_upgrade"],
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_requirements_file(env.config.python.install[0])
    assert "--upgrade" in fake.call_args[0]


def test_virtualenv_install_package_pip(docroot):
    (docroot / "pkg").mkdir()
    env = make_python_env(
        Virtualenv,
        {"python": {"install": [{"path": "pkg", "method": "pip"}]}, **SPHINX},
        source_file=str(docroot / "readthedocs.yml"),
    )
    install = env.config.python.install[0]
    assert isinstance(install, PythonInstall)
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_package(install)
    argv = fake.call_args[0]
    assert argv[-1] == "./pkg"
    assert "install" in argv


def test_virtualenv_install_package_pip_with_extras(docroot):
    (docroot / "pkg").mkdir()
    env = make_python_env(
        Virtualenv,
        {
            "python": {
                "install": [{"path": "pkg", "method": "pip", "extra_requirements": ["docs"]}]
            },
            **SPHINX,
        },
        source_file=str(docroot / "readthedocs.yml"),
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_package(env.config.python.install[0])
    assert fake.call_args[0][-1] == "./pkg[docs]"


def test_virtualenv_install_package_setuptools(docroot):
    (docroot / "pkg").mkdir()
    env = make_python_env(
        Virtualenv,
        {"python": {"install": [{"path": "pkg", "method": "setuptools"}]}, **SPHINX},
        source_file=str(docroot / "readthedocs.yml"),
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_package(env.config.python.install[0])
    argv = fake.call_args[0]
    assert argv[1].endswith("setup.py")
    assert "install" in argv
    assert "--force" in argv


def test_install_requirements_dispatches_by_type(docroot):
    (docroot / "requirements.txt").write_text("")
    (docroot / "pkg").mkdir()
    env = make_python_env(
        Virtualenv,
        {
            "python": {
                "install": [
                    {"requirements": "requirements.txt"},
                    {"path": "pkg", "method": "pip"},
                ]
            },
            **SPHINX,
        },
        source_file=str(docroot / "readthedocs.yml"),
    )
    with mock.patch.object(env, "install_requirements_file") as req, mock.patch.object(
        env, "install_package"
    ) as pkg:
        env.install_requirements()
    req.assert_called_once()
    pkg.assert_called_once()


# ---------------------------------------------------------------------------
# UvEnv
# ---------------------------------------------------------------------------


def test_uv_setup_base(docroot):
    env = make_python_env(
        UvEnv,
        {"python": {"install": [{"method": "uv", "command": "sync"}]}, **SPHINX},
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.setup_base()
    assert fake.call_args[0] == ("uv", "venv", "$READTHEDOCS_VIRTUALENV_PATH")


def test_uv_setup_base_drops_uv_python_while_creating_the_venv(docroot):
    # UV_PYTHON points inside the venv, which doesn't exist yet when creating it.
    env = make_python_env(
        UvEnv,
        {"python": {"install": [{"method": "uv", "command": "sync"}]}, **SPHINX},
    )
    env.build_env._environment["UV_PYTHON"] = "/envs/latest/bin/python"

    environment_during_run = {}
    patcher, fake = _capture_run(env)
    fake.side_effect = lambda *args, **kwargs: environment_during_run.update(
        env.build_env._environment
    )
    with patcher:
        env.setup_base()

    assert "UV_PYTHON" not in environment_during_run
    assert env.build_env._environment["UV_PYTHON"] == "/envs/latest/bin/python"


def test_uv_core_requirements_is_a_noop(docroot):
    env = make_python_env(
        UvEnv,
        {"python": {"install": [{"method": "uv", "command": "sync"}]}, **SPHINX},
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_core_requirements()
    fake.assert_not_called()


def test_uv_sync(docroot):
    env = make_python_env(
        UvEnv,
        {"python": {"install": [{"method": "uv", "command": "sync"}]}, **SPHINX},
    )
    install = env.config.python.install[0]
    assert isinstance(install, UvInstall)
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_uv(install)
    assert fake.call_args[0] == ("uv", "sync")


def test_uv_sync_with_groups(docroot):
    env = make_python_env(
        UvEnv,
        {"python": {"install": [{"method": "uv", "command": "sync", "groups": ["docs", "test"]}]}, **SPHINX},
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_uv(env.config.python.install[0])
    argv = fake.call_args[0]
    assert "--group" in argv
    assert "docs" in argv and "test" in argv


def test_uv_sync_with_all_groups(docroot):
    env = make_python_env(
        UvEnv,
        {"python": {"install": [{"method": "uv", "command": "sync", "groups": "all"}]}, **SPHINX},
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_uv(env.config.python.install[0])
    assert "--all-groups" in fake.call_args[0]


def test_uv_sync_with_all_extras(docroot):
    env = make_python_env(
        UvEnv,
        {"python": {"install": [{"method": "uv", "command": "sync", "extras": "all"}]}, **SPHINX},
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_uv(env.config.python.install[0])
    assert "--all-extras" in fake.call_args[0]


def test_uv_pip_with_requirements(docroot):
    (docroot / "requirements.txt").write_text("")
    env = make_python_env(
        UvEnv,
        {
            "python": {
                "install": [{"method": "uv", "command": "pip", "requirements": "requirements.txt"}]
            },
            **SPHINX,
        },
        source_file=str(docroot / "readthedocs.yml"),
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_uv(env.config.python.install[0])
    argv = fake.call_args[0]
    assert argv[:3] == ("uv", "pip", "install")
    assert argv[-2:] == ("-r", "requirements.txt")


def test_uv_pip_with_path_and_extras(docroot):
    (docroot / "pkg").mkdir()
    env = make_python_env(
        UvEnv,
        {
            "python": {
                "install": [{"method": "uv", "command": "pip", "path": "pkg", "extras": ["docs"]}]
            },
            **SPHINX,
        },
        source_file=str(docroot / "readthedocs.yml"),
    )
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_uv(env.config.python.install[0])
    assert fake.call_args[0][-1] == "pkg[docs]"


# ---------------------------------------------------------------------------
# Conda
# ---------------------------------------------------------------------------


def _conda_env(docroot, environment_yml="name: test\ndependencies: []\n", tool="miniconda-latest"):
    (docroot / "environment.yml").write_text(environment_yml)
    return make_python_env(
        Conda,
        {
            "build": {"os": "ubuntu-22.04", "tools": {"python": tool}},
            "conda": {"environment": "environment.yml"},
        },
        source_file=str(docroot / "readthedocs.yml"),
    )


def test_conda_bin_name_is_conda_for_miniconda(docroot):
    env = _conda_env(docroot, tool="miniconda-latest")
    assert env.conda_bin_name() == "conda"


def test_conda_bin_name_is_mamba_for_mambaforge(docroot):
    env = _conda_env(docroot, tool="mambaforge-latest")
    assert env.conda_bin_name() == "mamba"


def test_conda_setup_base_creates_the_env(docroot):
    env = _conda_env(docroot)
    patcher, fake = _capture_run(env)
    with patcher:
        env.setup_base()
    # setup_base appends core deps, cats the file, then creates the env.
    create = next(argv for argv in _argvs(fake) if "create" in argv)
    assert create[0] == "conda"
    assert "--name" in create
    assert "latest" in create  # version slug


def test_conda_core_requirements_are_appended_to_environment_yml(docroot):
    import os

    env = _conda_env(docroot, environment_yml="name: test\ndependencies:\n  - numpy\n")
    # ``_append_core_requirements`` reads the file relative to ``checkout_path``.
    os.makedirs(env.checkout_path, exist_ok=True)
    env_yml = os.path.join(env.checkout_path, "environment.yml")
    with open(env_yml, "w") as fh:
        fh.write("name: test\ndependencies:\n  - numpy\n")

    with mock.patch.object(env.build_env, "run"):
        env._append_core_requirements()
    import yaml

    with open(env_yml) as fh:
        written = yaml.safe_load(fh)
    # sphinx is injected as a conda dependency; a pip section is added.
    assert "sphinx" in written["dependencies"]
    assert any(isinstance(d, dict) and "pip" in d for d in written["dependencies"])


def test_conda_core_requirements_uses_mkdocs_via_pip_for_mkdocs(docroot):
    (docroot / "environment.yml").write_text("name: t\ndependencies: []\n")
    (docroot / "mkdocs.yml").write_text("")
    env = make_python_env(
        Conda,
        {
            "build": {"os": "ubuntu-22.04", "tools": {"python": "miniconda-latest"}},
            "conda": {"environment": "environment.yml"},
            "mkdocs": {"configuration": "mkdocs.yml"},
        },
        source_file=str(docroot / "readthedocs.yml"),
    )
    pip_reqs, conda_reqs = env._get_core_requirements()
    assert "mkdocs" in pip_reqs
    assert "sphinx" not in conda_reqs


def test_uv_install_is_dispatched_from_install_requirements(docroot):
    env = make_python_env(
        UvEnv,
        {"python": {"install": [{"method": "uv", "command": "sync"}]}, **SPHINX},
    )
    with mock.patch.object(env, "install_uv") as install_uv:
        env.install_requirements()
    install_uv.assert_called_once()


def test_conda_append_core_requirements_is_quiet_when_the_file_is_missing(docroot):
    # The environment.yml passed validation (it exists next to the config) but
    # isn't in the checkout path at build time -> read fails, warn, return.
    env = _conda_env(docroot)
    with mock.patch.object(env.build_env, "run"):
        env._append_core_requirements()  # must not raise


def test_conda_append_core_requirements_merges_an_existing_pip_section(docroot):
    import os

    env = _conda_env(docroot)
    os.makedirs(env.checkout_path, exist_ok=True)
    env_yml = os.path.join(env.checkout_path, "environment.yml")
    with open(env_yml, "w") as fh:
        fh.write("name: test\ndependencies:\n  - numpy\n  - pip:\n    - requests\n")

    with mock.patch.object(env.build_env, "run"):
        env._append_core_requirements()

    import yaml

    with open(env_yml) as fh:
        written = yaml.safe_load(fh)
    pip_section = next(d["pip"] for d in written["dependencies"] if isinstance(d, dict))
    # The user's pip dep survives alongside our injected ones.
    assert "requests" in pip_section


def test_conda_install_core_requirements_is_a_noop(docroot):
    env = _conda_env(docroot)
    patcher, fake = _capture_run(env)
    with patcher:
        env.install_core_requirements()
    fake.assert_not_called()


def test_conda_environment_yml_may_symlink_inside_the_checkout(docroot):
    """Users legitimately keep the file elsewhere in their repo and symlink it."""
    import os

    env = _conda_env(docroot, environment_yml="name: test\ndependencies:\n  - numpy\n")
    os.makedirs(env.checkout_path, exist_ok=True)
    real = os.path.join(env.checkout_path, "conf", "environment.yml")
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w") as fh:
        fh.write("name: test\ndependencies:\n  - numpy\n")
    os.symlink(real, os.path.join(env.checkout_path, "environment.yml"))

    with mock.patch.object(env.build_env, "run"):
        env._append_core_requirements()

    import yaml

    with open(real) as fh:
        assert "sphinx" in str(yaml.safe_load(fh))


def test_conda_environment_yml_cannot_symlink_out_of_the_checkout(docroot, tmp_path):
    """
    The runner reads and writes this from the *host*.

    A symlink escaping the checkout would resolve against the instance's own
    filesystem, so it must be refused on read rather than followed.
    """
    import os

    env = _conda_env(docroot, environment_yml="name: test\ndependencies:\n  - numpy\n")
    os.makedirs(env.checkout_path, exist_ok=True)
    outside = tmp_path / "outside.yml"
    outside.write_text("name: evil\ndependencies: []\n")
    os.symlink(str(outside), os.path.join(env.checkout_path, "environment.yml"))

    with mock.patch.object(env.build_env, "run"):
        # Only IOError/ParseError are handled, so this fails the build rather
        # than being quietly followed — same as upstream.
        with pytest.raises(SymlinkOutsideBasePath):
            env._append_core_requirements()

    assert outside.read_text() == "name: evil\ndependencies: []\n"
