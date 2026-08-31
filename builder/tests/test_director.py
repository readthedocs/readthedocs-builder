"""
Tests for the build director (the orchestrator).

Fresh tests (upstream's ``projects/tests/test_build_tasks.py`` leans on Django
fixtures + ``mockers.py``): the director is mocked at its collaborator seams —
the VCS repo, build environments, language environment, storage, and API
client — and each decomposed method is asserted independently. See
``make_director`` in conftest.
"""

import os
import stat
from pathlib import Path
from unittest import mock

import pytest
from conftest import make_director

from builder import settings
from builder.constants import GENERIC
from builder.exceptions import BuildAppError
from builder.exceptions import BuildUserError
from builder.exceptions import RepositoryError
from builder.storage import StorageType


SPHINX = {"sphinx": {"configuration": "conf.py"}}

# Overriding ``build.jobs.build.html`` without a ``sphinx:``/``mkdocs:`` key
# makes the doctype generic. See https://github.com/readthedocs/readthedocs.org/issues/13192
UV_GENERIC = {
    "build": {
        "os": "ubuntu-24.04",
        "tools": {"python": "3.12"},
        "jobs": {"build": {"html": ["uv run zensical build --clean"]}},
    },
    "python": {"install": [{"method": "uv", "command": "sync", "groups": ["docs"]}]},
}


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------


def test_rtd_env_vars(docroot):
    director = make_director(SPHINX)
    env = director.get_rtd_env_vars()
    assert env["READTHEDOCS"] == "True"
    assert env["READTHEDOCS_VERSION"] == "latest"
    assert env["READTHEDOCS_VERSION_TYPE"] == "branch"
    assert env["READTHEDOCS_PROJECT"] == "pip"
    assert env["READTHEDOCS_LANGUAGE"] == "en"
    assert env["READTHEDOCS_OUTPUT"].endswith("_readthedocs/")


def test_vcs_env_vars_disable_git_prompt(docroot):
    director = make_director(SPHINX)
    env = director.get_vcs_env_vars()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "READTHEDOCS_GIT_CLONE_TOKEN" in env


def test_git_ssh_command_added_only_for_private_repos(docroot):
    env = {}
    make_director(SPHINX)._add_git_ssh_command_env_var(env)
    assert "GIT_SSH_COMMAND" not in env

    make_director(SPHINX, allow_private_repos=True)._add_git_ssh_command_env_var(env)
    assert "StrictHostKeyChecking=no" in env["GIT_SSH_COMMAND"]


# ---------------------------------------------------------------------------
# SSH deploy-key agent
# ---------------------------------------------------------------------------


def _run_creating_dirs(director, output=""):
    """
    Make the mocked VCS environment carry out ``mkdir`` for real.

    ``_write_ssh_key`` creates the key's directory *through the environment*,
    so that it belongs to the build user rather than to whoever the runner is.
    A mock that only records the call leaves nowhere to write the key.
    """
    result = mock.MagicMock()
    result.output = output

    def _run(*args, **kwargs):
        if args and args[0] == "mkdir":
            os.makedirs(args[-1], exist_ok=True)
        return result

    director.vcs_environment.run.side_effect = _run
    return result


def _calls_named(director, name):
    """Every recorded command whose first argument is ``name``."""
    return [c for c in director.vcs_environment.run.call_args_list if c.args and c.args[0] == name]


def _agent_output():
    return (
        "SSH_AUTH_SOCK=/tmp/ssh-xyz/agent.42; export SSH_AUTH_SOCK;\n"
        "SSH_AGENT_PID=42; export SSH_AGENT_PID;\n"
        "echo Agent pid 42;\n"
    )


def test_setup_ssh_agent_noop_when_private_repos_disabled(docroot):
    director = make_director(SPHINX, project={"repo": "git@github.com:rtd/private.git"})
    director.setup_ssh_agent()
    assert director.ssh_agent_env == {}
    director.vcs_environment.run.assert_not_called()


def test_setup_ssh_agent_noop_for_https_repo(docroot, monkeypatch):
    director = make_director(SPHINX, project={"repo": "https://github.com/rtd/public"}, allow_private_repos=True)
    director.setup_ssh_agent()
    assert director.ssh_agent_env == {}
    director.vcs_environment.run.assert_not_called()


def test_setup_ssh_agent_skips_when_project_has_no_key(docroot, monkeypatch):
    director = make_director(SPHINX, project={"repo": "git@github.com:rtd/private.git"}, allow_private_repos=True)
    director.data.api_client.project.return_value.key.get.return_value = {"private_key": ""}
    director.setup_ssh_agent()
    assert director.ssh_agent_env == {}
    director.vcs_environment.run.assert_not_called()


def test_setup_ssh_agent_loads_key_and_injects_env(docroot, monkeypatch):
    monkeypatch.setenv("RTD_BUILD_TIME_LIMIT_SECONDS", "900")
    director = make_director(SPHINX, project={"repo": "git@github.com:rtd/private.git"}, allow_private_repos=True)
    director.data.api_client.project.return_value.key.get.return_value = {
        "private_key": "PRIVATE-KEY-CONTENT",
    }
    _run_creating_dirs(director, output=_agent_output())

    director.setup_ssh_agent()

    assert director.ssh_agent_env == {
        "SSH_AUTH_SOCK": "/tmp/ssh-xyz/agent.42",
        "SSH_AGENT_PID": "42",
    }
    # The socket is injected into the VCS environment for the clone.
    director.vcs_environment._environment.update.assert_called_with(director.ssh_agent_env)

    # ssh-agent started, then the key added with a TTL that outlasts the limit.
    assert _calls_named(director, "ssh-agent")[0].args == ("ssh-agent", "-s")
    add_args = _calls_named(director, "ssh-add")[0].args
    assert add_args[1] == "-t"
    assert int(add_args[2]) > 900


def test_ssh_key_is_written_inside_the_docroot(docroot, monkeypatch):
    """
    ``ssh-add`` runs inside the build container; the runner writes the key here.

    The docroot is the only path mounted into the container, and it's mounted
    at the same path on both sides — so a key written anywhere else (a host
    temp file, say) simply doesn't exist for ``ssh-add``.
    """
    director = make_director(SPHINX, project={"repo": "git@github.com:rtd/private.git"}, allow_private_repos=True)
    _run_creating_dirs(director)

    key_path = Path(director._write_ssh_key("PRIVATE-KEY-CONTENT"))

    assert key_path.is_relative_to(Path(settings.DOCROOT))
    assert key_path.read_text() == "PRIVATE-KEY-CONTENT"


def test_ssh_key_is_never_readable_by_anyone_else(docroot, monkeypatch):
    """It lands on a path the build container can see, so keep it 0400."""
    director = make_director(SPHINX, project={"repo": "git@github.com:rtd/private.git"}, allow_private_repos=True)
    _run_creating_dirs(director)

    key_path = Path(director._write_ssh_key("PRIVATE-KEY-CONTENT"))

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o400


def test_setup_ssh_agent_passes_the_docroot_path_to_ssh_add(docroot, monkeypatch):
    """The path handed to ``ssh-add`` has to resolve inside the container."""
    director = make_director(SPHINX, project={"repo": "git@github.com:rtd/private.git"}, allow_private_repos=True)
    director.data.api_client.project.return_value.key.get.return_value = {
        "private_key": "PRIVATE-KEY-CONTENT",
    }
    _run_creating_dirs(director, output=_agent_output())

    director.setup_ssh_agent()

    ssh_add_path = Path(_calls_named(director, "ssh-add")[0].args[3])
    assert ssh_add_path.is_relative_to(Path(settings.DOCROOT))


def test_setup_ssh_agent_deletes_the_key_file(docroot, monkeypatch):
    director = make_director(SPHINX, project={"repo": "git@github.com:rtd/private.git"}, allow_private_repos=True)
    director.data.api_client.project.return_value.key.get.return_value = {
        "private_key": "PRIVATE-KEY-CONTENT",
    }
    _run_creating_dirs(director, output=_agent_output())

    written = []
    real_write = director._write_ssh_key

    def _spy(private_key):
        path = real_write(private_key)
        written.append(path)
        return path

    monkeypatch.setattr(director, "_write_ssh_key", _spy)
    director.setup_ssh_agent()

    assert written and not os.path.exists(written[0])


def test_ssh_agent_env_flows_into_build_env_vars(docroot):
    director = make_director(SPHINX)
    director.ssh_agent_env = {"SSH_AUTH_SOCK": "/tmp/ssh-xyz/agent.42"}
    env = director.get_build_env_vars()
    assert env["SSH_AUTH_SOCK"] == "/tmp/ssh-xyz/agent.42"


def test_build_env_vars_non_conda_sets_virtualenv_path(docroot):
    director = make_director(SPHINX)
    env = director.get_build_env_vars()
    assert env["NO_COLOR"] == "1"
    assert "READTHEDOCS_VIRTUALENV_PATH" in env
    assert "CONDA_ENVS_PATH" not in env


def test_build_env_vars_conda_sets_conda_paths(docroot):
    director = make_director(
        {
            "build": {"os": "ubuntu-22.04", "tools": {"python": "miniconda-latest"}},
            "conda": {"environment": "environment.yml"},
        },
    )
    env = director.get_build_env_vars()
    assert "CONDA_ENVS_PATH" in env
    assert env["CONDA_DEFAULT_ENV"] == "latest"
    assert env["BIN_PATH"].endswith(os.path.join("conda", "latest", "bin"))


def test_build_env_vars_uv_sets_uv_project(docroot):
    (docroot / "pkg").mkdir()
    director = make_director(
        {"python": {"install": [{"method": "uv", "command": "pip", "path": "pkg"}]}, **SPHINX},
    )
    env = director.get_build_env_vars()
    # Absolute: ``uv venv`` and ``uv run`` run from different cwds.
    checkout_path = director.data.project.checkout_path(director.data.version.slug)
    assert env["UV_PROJECT"] == os.path.join(checkout_path, "pkg")
    assert os.path.isabs(env["UV_PROJECT"])
    assert env["UV_PROJECT_ENVIRONMENT"] == env["READTHEDOCS_VIRTUALENV_PATH"]
    assert env["READTHEDOCS_VIRTUALENV_PATH"].endswith(os.path.join("envs", "latest"))


def test_build_env_vars_uv_project_falls_back_to_the_checkout_path(docroot):
    # ``command: sync`` has no ``path``, so the join raises and we fall back.
    director = make_director(
        {"python": {"install": [{"method": "uv", "command": "sync"}]}, **SPHINX},
    )
    env = director.get_build_env_vars()
    assert env["UV_PROJECT"] == director.data.project.checkout_path(director.data.version.slug)


def test_build_env_vars_uv_python_points_inside_the_venv(docroot):
    # The venv's python, not the asdf one: ``uv pip install`` would otherwise
    # install the packages system-wide.
    director = make_director(
        {"python": {"install": [{"method": "uv", "command": "sync"}]}, **SPHINX},
    )
    env = director.get_build_env_vars()
    assert env["UV_PYTHON"] == os.path.join(env["UV_PROJECT_ENVIRONMENT"], "bin", "python")


def test_build_env_vars_no_uv_vars_without_uv(docroot):
    director = make_director(SPHINX)
    env = director.get_build_env_vars()
    assert "UV_PYTHON" not in env
    assert "UV_PROJECT" not in env
    assert "UV_PROJECT_ENVIRONMENT" not in env


def test_build_env_vars_strips_private_vars_on_external_builds(docroot):
    director = make_director(
        SPHINX,
        project={
            "environment_variables": {
                "PUBLIC": {"value": "pub", "public": True},
                "PRIVATE": {"value": "priv", "public": False},
            }
        },
        version={"type": "external", "slug": "123"},
    )
    env = director.get_build_env_vars()
    assert env.get("PUBLIC") == "pub"
    assert "PRIVATE" not in env


def test_build_env_vars_keeps_private_vars_on_internal_builds(docroot):
    director = make_director(
        SPHINX,
        project={
            "environment_variables": {
                "PRIVATE": {"value": "priv", "public": False},
            }
        },
    )
    assert director.get_build_env_vars()["PRIVATE"] == "priv"


# ---------------------------------------------------------------------------
# is_type_sphinx
# ---------------------------------------------------------------------------


def test_is_type_sphinx_true_for_sphinx_doctype(docroot):
    director = make_director(SPHINX)
    assert director.is_type_sphinx() is True


def test_is_type_sphinx_false_for_mkdocs(docroot):
    (docroot / "mkdocs.yml").write_text("")
    director = make_director({"mkdocs": {"configuration": "mkdocs.yml"}})
    assert director.is_type_sphinx() is False


# ---------------------------------------------------------------------------
# run_build_job
# ---------------------------------------------------------------------------


def test_run_build_job_noop_without_commands(docroot):
    director = make_director(SPHINX)
    director.run_build_job("post_install")
    director.build_environment.run.assert_not_called()


def test_run_build_job_runs_in_build_env(docroot):
    director = make_director(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"post_install": ["echo hi", "echo bye"]},
            },
            **SPHINX,
        }
    )
    director.run_build_job("post_install")
    assert director.build_environment.run.call_count == 2
    _, kwargs = director.build_environment.run.call_args
    assert kwargs["escape_command"] is False


def test_run_build_job_checkout_runs_in_vcs_env(docroot):
    director = make_director(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"post_checkout": ["echo hi"]},
            },
            **SPHINX,
        }
    )
    director.run_build_job("post_checkout")
    director.vcs_environment.run.assert_called_once()
    director.build_environment.run.assert_not_called()


# ---------------------------------------------------------------------------
# Build format dispatch
# ---------------------------------------------------------------------------


def test_build_html_calls_the_doctype_builder(docroot):
    director = make_director(SPHINX)
    with mock.patch.object(director, "build_docs_class") as build_docs:
        director.build_html()
    build_docs.assert_called_once_with("sphinx")


def test_build_html_job_override_runs_the_job(docroot):
    director = make_director(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"build": {"html": ["echo build"]}},
            },
            **SPHINX,
        }
    )
    with mock.patch.object(director, "build_docs_class") as build_docs, mock.patch.object(
        director, "run_build_job"
    ) as run_job:
        director.build_html()
    run_job.assert_called_once_with("build.html")
    build_docs.assert_not_called()


def test_build_pdf_skipped_when_format_absent(docroot):
    director = make_director(SPHINX)
    assert director.build_pdf() is False


def test_build_pdf_skipped_for_external_versions(docroot):
    director = make_director({"formats": ["pdf"], **SPHINX}, version={"type": "external", "slug": "1"})
    assert director.build_pdf() is False


def test_build_pdf_builds_for_sphinx(docroot):
    director = make_director({"formats": ["pdf"], **SPHINX})
    with mock.patch.object(director, "build_docs_class") as build_docs:
        director.build_pdf()
    build_docs.assert_called_once_with("sphinx_pdf")


def test_build_pdf_not_built_for_mkdocs(docroot):
    (docroot / "mkdocs.yml").write_text("")
    director = make_director({"formats": ["pdf"], "mkdocs": {"configuration": "mkdocs.yml"}})
    assert director.build_pdf() is False


def test_build_htmlzip_builds_localmedia_for_sphinx(docroot):
    director = make_director({"formats": ["htmlzip"], **SPHINX})
    with mock.patch.object(director, "build_docs_class") as build_docs:
        director.build_htmlzip()
    build_docs.assert_called_once_with("sphinx_singlehtmllocalmedia")


def test_build_epub_builds_for_sphinx(docroot):
    director = make_director({"formats": ["epub"], **SPHINX})
    with mock.patch.object(director, "build_docs_class") as build_docs:
        director.build_epub()
    build_docs.assert_called_once_with("sphinx_epub")


def test_build_runs_all_formats_and_hooks(docroot):
    director = make_director(SPHINX)
    with mock.patch.object(director, "build_html") as html, mock.patch.object(
        director, "build_htmlzip"
    ) as htmlzip, mock.patch.object(director, "build_pdf") as pdf, mock.patch.object(
        director, "build_epub"
    ) as epub, mock.patch.object(director, "run_build_job") as run_job, mock.patch.object(
        director, "store_readthedocs_build_yaml"
    ) as store:
        director.build()
    html.assert_called_once()
    htmlzip.assert_called_once()
    pdf.assert_called_once()
    epub.assert_called_once()
    store.assert_called_once()
    assert run_job.call_args_list == [mock.call("pre_build"), mock.call("post_build")]


# ---------------------------------------------------------------------------
# build_docs_class
# ---------------------------------------------------------------------------


def test_build_docs_class_generic_is_a_noop(docroot):
    director = make_director(SPHINX)
    assert director.build_docs_class(GENERIC) is None


def test_build_docs_class_runs_the_builder(docroot):
    director = make_director(SPHINX)
    builder = mock.MagicMock()
    builder.build.return_value = True
    builder_cls = mock.MagicMock(return_value=builder)
    with mock.patch("builder.director.get_builder_class", return_value=builder_cls):
        # A non-canonical builder class: no show_conf / doctype update.
        result = director.build_docs_class("sphinx_pdf")
    assert result is True
    builder.build.assert_called_once()
    builder.show_conf.assert_not_called()


def test_build_docs_class_canonical_shows_conf_and_records_doctype(docroot):
    director = make_director(SPHINX)
    builder = mock.MagicMock()
    builder.get_final_doctype.return_value = "sphinx"
    builder_cls = mock.MagicMock(return_value=builder)
    with mock.patch("builder.director.get_builder_class", return_value=builder_cls):
        director.build_docs_class("sphinx")  # == config.doctype
    builder.show_conf.assert_called_once()
    assert director.data.version.documentation_type == "sphinx"


# ---------------------------------------------------------------------------
# create_environment / install
# ---------------------------------------------------------------------------


def test_create_environment_sets_up_the_language_env(docroot):
    director = make_director(SPHINX)
    director.create_environment()
    director.language_environment.setup_base.assert_called_once()


def test_create_environment_runs_the_job_when_overridden(docroot):
    director = make_director(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"create_environment": ["echo mkenv"]},
            },
            **SPHINX,
        }
    )
    with mock.patch.object(director, "run_build_job") as run_job:
        director.create_environment()
    run_job.assert_called_once_with("create_environment")
    director.language_environment.setup_base.assert_not_called()


def test_create_environment_generic_skips_setup(docroot):
    director = make_director(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "commands": ["echo hi"]}},
    )
    director.create_environment()
    director.language_environment.setup_base.assert_not_called()


def test_create_environment_generic_using_uv_sets_up_base(docroot):
    # No ``sphinx:``/``mkdocs:`` key makes the doctype generic, but uv still
    # needs its environment created (``uv venv``).
    director = make_director(UV_GENERIC)
    director.create_environment()
    director.language_environment.setup_base.assert_called_once()


def test_install_installs_core_and_user_requirements(docroot):
    director = make_director(SPHINX)
    director.install()
    director.language_environment.install_core_requirements.assert_called_once()
    director.language_environment.install_requirements.assert_called_once()


def test_install_runs_the_job_when_overridden(docroot):
    director = make_director(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"install": ["pip install ."]},
            },
            **SPHINX,
        }
    )
    with mock.patch.object(director, "run_build_job") as run_job:
        director.install()
    run_job.assert_called_once_with("install")
    director.language_environment.install_core_requirements.assert_not_called()


def test_install_generic_skips(docroot):
    director = make_director(
        {"build": {"os": "ubuntu-22.04", "tools": {"python": "3"}, "commands": ["echo hi"]}},
    )
    director.install()
    director.language_environment.install_core_requirements.assert_not_called()


def test_install_generic_using_uv_installs_requirements(docroot):
    # Generic doctype, but ``uv sync`` still has to run to install the deps
    # the user's commands need.
    director = make_director(UV_GENERIC)
    director.install()
    director.language_environment.install_core_requirements.assert_called_once()
    director.language_environment.install_requirements.assert_called_once()


# ---------------------------------------------------------------------------
# system_dependencies (apt)
# ---------------------------------------------------------------------------


def test_system_dependencies_installs_apt_packages(docroot):
    director = make_director(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "apt_packages": ["libfoo", "libbar"],
            },
            **SPHINX,
        }
    )
    director.system_dependencies()
    # apt-get update, then install with a ``--`` guard before package names.
    calls = director.build_environment.run.call_args_list
    assert calls[0].args[:2] == ("apt-get", "update")
    install_args = calls[1].args
    assert "--" in install_args
    assert install_args[install_args.index("--") + 1 :] == ("libfoo", "libbar")


def test_system_dependencies_noop_without_packages(docroot):
    director = make_director(SPHINX)
    director.system_dependencies()
    director.build_environment.run.assert_not_called()


# ---------------------------------------------------------------------------
# run_build_commands (generic)
# ---------------------------------------------------------------------------


def test_run_build_commands_runs_each_command(docroot):
    director = make_director(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "commands": ["echo one", "echo two"],
            },
        }
    )
    # The HTML output dir must exist or run_build_commands raises.
    html_out = os.path.join(director.data.project.checkout_path("latest"), "_readthedocs/html")
    os.makedirs(html_out, exist_ok=True)
    director.run_build_commands()
    argvs = [c.args[0] for c in director.build_environment.run.call_args_list]
    assert "echo one" in argvs
    assert "echo two" in argvs


def test_run_build_commands_reshims_after_pip_install(docroot):
    director = make_director(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "commands": ["pip install sphinx"],
            },
        }
    )
    html_out = os.path.join(director.data.project.checkout_path("latest"), "_readthedocs/html")
    os.makedirs(html_out, exist_ok=True)
    director.run_build_commands()
    # A reshim call follows the pip install.
    reshims = [
        c for c in director.build_environment.run.call_args_list if c.args[:2] == ("asdf", "reshim")
    ]
    assert reshims


def test_run_build_commands_raises_without_output(docroot):
    director = make_director(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "commands": ["echo hi"],
            },
        }
    )
    # No _readthedocs/html directory created -> error.
    with pytest.raises(BuildUserError) as excinfo:
        director.run_build_commands()
    assert excinfo.value.message_id == BuildUserError.BUILD_COMMANDS_WITHOUT_OUTPUT


# ---------------------------------------------------------------------------
# check_old_output_directory
# ---------------------------------------------------------------------------


def test_check_old_output_directory_raises_when_present(docroot):
    director = make_director(SPHINX)
    director.build_environment.run.return_value = mock.MagicMock(exit_code=0)
    with pytest.raises(BuildUserError) as excinfo:
        director.check_old_output_directory()
    assert excinfo.value.message_id == BuildUserError.BUILD_OUTPUT_OLD_DIRECTORY_USED


def test_check_old_output_directory_passes_when_absent(docroot):
    director = make_director(SPHINX)
    director.build_environment.run.return_value = mock.MagicMock(exit_code=1)
    director.check_old_output_directory()  # must not raise


# ---------------------------------------------------------------------------
# setup_environment orchestration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config, expected_cls_name",
    [
        (SPHINX, "Virtualenv"),
        ({"python": {"install": [{"method": "uv", "command": "sync"}]}, **SPHINX}, "UvEnv"),
    ],
)
def test_setup_environment_selects_language_env(docroot, config, expected_cls_name):
    director = make_director(config)
    with mock.patch.object(director, "run_build_job"), mock.patch.object(
        director, "system_dependencies"
    ), mock.patch.object(director, "install_build_tools"), mock.patch.object(
        director, "create_environment"
    ), mock.patch.object(director, "install"):
        director.setup_environment()
    assert type(director.language_environment).__name__ == expected_cls_name


def test_setup_environment_conda_selects_conda(docroot):
    director = make_director(
        {
            "build": {"os": "ubuntu-22.04", "tools": {"python": "miniconda-latest"}},
            "conda": {"environment": "environment.yml"},
        },
    )
    with mock.patch.object(director, "run_build_job"), mock.patch.object(
        director, "system_dependencies"
    ), mock.patch.object(director, "install_build_tools"), mock.patch.object(
        director, "create_environment"
    ), mock.patch.object(director, "install"):
        director.setup_environment()
    assert type(director.language_environment).__name__ == "Conda"


def test_setup_environment_runs_phases_in_order(docroot):
    director = make_director(SPHINX)
    calls = []
    with mock.patch.object(director, "run_build_job", side_effect=lambda j: calls.append(j)), \
        mock.patch.object(director, "system_dependencies", side_effect=lambda: calls.append("sysdeps")), \
        mock.patch.object(director, "install_build_tools", side_effect=lambda: calls.append("tools")), \
        mock.patch.object(director, "create_environment", side_effect=lambda: calls.append("createenv")), \
        mock.patch.object(director, "install", side_effect=lambda: calls.append("install")):
        director.setup_environment()
    assert calls == [
        "pre_system_dependencies",
        "sysdeps",
        "post_system_dependencies",
        "tools",
        "pre_create_environment",
        "createenv",
        "post_create_environment",
        "pre_install",
        "install",
        "post_install",
    ]


# ---------------------------------------------------------------------------
# create_vcs_environment / create_build_environment
# ---------------------------------------------------------------------------


def test_create_vcs_environment_builds_a_recording_env(docroot):
    director = make_director(SPHINX)
    director.create_vcs_environment()
    assert director.vcs_environment.project is director.data.project
    assert director.vcs_environment.record is True
    assert director.vcs_environment.config is None  # no config during VCS


def test_create_build_environment_carries_the_config(docroot):
    director = make_director(SPHINX)
    director.create_build_environment()
    assert director.build_environment.config is director.data.config
    assert director.build_environment.record is True


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------


def _prime_checkout(director, docroot, config_yaml="version: 2\nbuild:\n  os: ubuntu-22.04\n  tools:\n    python: '3'\nsphinx:\n  configuration: conf.py\n"):
    """Write a config file into the checkout path so ``load_config`` succeeds."""
    checkout = director.data.project.checkout_path(director.data.version.slug)
    os.makedirs(checkout, exist_ok=True)
    with open(os.path.join(checkout, ".readthedocs.yaml"), "w") as fh:
        fh.write(config_yaml)
    return checkout


def test_checkout_loads_config_and_updates_submodules(docroot):
    director = make_director(SPHINX)
    _prime_checkout(director, docroot)
    director.vcs_repository.commit = "deadbeef"

    director.checkout()

    director.vcs_repository.update.assert_called_once()
    director.vcs_repository.checkout.assert_called_once()
    director.vcs_repository.update_submodules.assert_called_once()
    assert director.data.config is not None
    assert director.data.build["config"]["build"]["os"] == "ubuntu-22.04"


def test_checkout_machine_latest_detects_default_branch(docroot):
    director = make_director(SPHINX, version={"slug": "latest", "machine": True})
    _prime_checkout(director, docroot)
    director.vcs_repository.get_default_branch.return_value = "main"

    director.checkout()

    # Machine latest without a project default branch resolves the remote HEAD
    # and skips the explicit checkout.
    assert director.data.default_branch == "main"
    director.vcs_repository.checkout.assert_not_called()


def test_checkout_propagates_config_validation_errors(docroot):
    # A legacy ``build.image`` config is rejected by config validation
    # (build.os required) inside load_config; checkout() surfaces that error.
    from builder.config.exceptions import ConfigError

    director = make_director(SPHINX)
    _prime_checkout(
        director,
        docroot,
        config_yaml="version: 2\nbuild:\n  image: latest\nsphinx:\n  configuration: conf.py\n",
    )
    with pytest.raises(ConfigError):
        director.checkout()


# ---------------------------------------------------------------------------
# setup_vcs
# ---------------------------------------------------------------------------


def test_setup_vcs_unsupported_vcs_raises(docroot):
    director = make_director(SPHINX, project={"repo_type": "svn"}, allow_private_repos=True)
    with pytest.raises(RepositoryError) as excinfo:
        director.setup_vcs()
    assert excinfo.value.message_id == RepositoryError.UNSUPPORTED_VCS


def test_setup_vcs_creates_the_checkout_dir_via_the_environment(docroot):
    # The checkout dir must be created through the build environment (so it's
    # ``runuser`` wrapped and owned by the build user), not via ``os.makedirs``
    # in the root runner process. Guards the ownership regression.
    director = make_director(SPHINX)
    with mock.patch.object(director, "checkout"), mock.patch.object(
        director, "run_build_job"
    ), mock.patch.object(
        director.data.project, "vcs_repo", return_value=director.vcs_repository
    ):
        director.setup_vcs()
    mkdir_calls = [
        c for c in director.vcs_environment.run.call_args_list if c.args[:1] == ("mkdir",)
    ]
    assert len(mkdir_calls) == 1
    assert director.data.project.doc_path in mkdir_calls[0].args


def test_setup_vcs_checks_out_and_records_commit(docroot):
    director = make_director(SPHINX)
    with mock.patch.object(director, "checkout") as checkout, mock.patch.object(
        director, "run_build_job"
    ) as run_job:
        director.vcs_repository.commit = "abc123"
        # setup_vcs rebuilds vcs_repository from the project; capture the commit
        # by having the real vcs_repo return our mock.
        with mock.patch.object(director.data.project, "vcs_repo", return_value=director.vcs_repository):
            director.setup_vcs()
    checkout.assert_called_once()
    run_job.assert_called_once_with("post_checkout")
    assert director.data.build["commit"] == "abc123"


# ---------------------------------------------------------------------------
# install_build_tools (S3 cache)
# ---------------------------------------------------------------------------


def test_install_build_tools_cache_miss_uses_asdf_install(docroot):
    director = make_director(SPHINX)
    storage = mock.MagicMock()
    storage.exists.return_value = False
    with mock.patch("builder.storage.get_storage", return_value=storage):
        director.install_build_tools()
    argvs = [c.args for c in director.build_environment.run.call_args_list]
    assert ("asdf", "install", "python", mock.ANY) in argvs
    assert any(a[:2] == ("asdf", "global") for a in argvs)


def test_install_build_tools_cache_miss_bootstraps_virtualenv(docroot):
    # A compiled-on-the-fly python needs virtualenv + setuptools installed.
    director = make_director(SPHINX)
    storage = mock.MagicMock()
    storage.exists.return_value = False
    with mock.patch("builder.storage.get_storage", return_value=storage):
        director.install_build_tools()
    argvs = [c.args for c in director.build_environment.run.call_args_list]
    assert any(a[:4] == ("python", "-mpip", "install", "-U") for a in argvs)


def test_install_build_tools_cache_hit_skips_virtualenv_bootstrap(docroot):
    # Cached tarballs already ship virtualenv + setuptools, so we must not
    # reinstall them — this is what upstream does and what ``build.commands``
    # builds rely on to keep their environment untouched.
    director = make_director(SPHINX)
    storage = mock.MagicMock()
    storage.exists.return_value = True
    storage.open.return_value = mock.MagicMock()
    with mock.patch("builder.storage.get_storage", return_value=storage), mock.patch(
        "builder.storage.extract_tarball_to"
    ):
        director.install_build_tools()
    argvs = [c.args for c in director.build_environment.run.call_args_list]
    assert not any(a[:2] == ("python", "-mpip") for a in argvs)


def test_install_build_tools_cache_hit_extracts_tarball(docroot):
    director = make_director(SPHINX)
    storage = mock.MagicMock()
    storage.exists.return_value = True
    storage.open.return_value = mock.MagicMock()
    with mock.patch("builder.storage.get_storage", return_value=storage), mock.patch(
        "builder.storage.extract_tarball_to"
    ) as extract:
        director.install_build_tools()
    extract.assert_called_once()
    calls = director.build_environment.run.call_args_list
    argvs = [c.args for c in calls]
    # The root-owned extracted tree is handed to the build user (as root)
    # before the docs-user ``mv`` can rename out of it.
    chown_idx = next(i for i, a in enumerate(argvs) if a and a[0] == "chown")
    mv_idx = next(i for i, a in enumerate(argvs) if a and a[0] == "mv")
    assert calls[chown_idx].kwargs["user"] == "root"
    assert "--recursive" in calls[chown_idx].args
    assert chown_idx < mv_idx
    # asdf install is NOT called on a hit.
    assert not any(a[:2] == ("asdf", "install") for a in argvs)


# ---------------------------------------------------------------------------
# store_readthedocs_build_yaml
# ---------------------------------------------------------------------------


def test_store_build_yaml_noop_when_absent(docroot):
    director = make_director(SPHINX)
    director.store_readthedocs_build_yaml()
    assert director.data.version.build_data is None


def test_store_build_yaml_loads_the_file(docroot):
    director = make_director(SPHINX)
    artifact_dir = director.data.project.artifact_path(version="latest", type_="html")
    os.makedirs(artifact_dir, exist_ok=True)
    with open(os.path.join(artifact_dir, "readthedocs-build.yaml"), "w") as fh:
        fh.write("version: 1\n")
    director.store_readthedocs_build_yaml()
    assert director.data.version.build_data == {"version": 1}


# ---------------------------------------------------------------------------
# attach_notification
# ---------------------------------------------------------------------------


def test_attach_notification_posts_to_the_api(docroot):
    director = make_director(SPHINX)
    director.attach_notification(attached_to="build/1", message_id="some:id")
    director.data.api_client.notifications.post.assert_called_once()
    payload = director.data.api_client.notifications.post.call_args[0][0]
    assert payload["attached_to"] == "build/1"
    assert payload["message_id"] == "some:id"


# ---------------------------------------------------------------------------
# checkout — SSH-key-write-access policy & custom config path
# ---------------------------------------------------------------------------


def test_checkout_ssh_key_write_access_is_a_hard_failure(docroot, monkeypatch):
    monkeypatch.setattr("builder.settings.RTD_ENFORCE_BROWNOUTS_FOR_DEPRECATIONS", True)
    director = make_director(SPHINX, allow_private_repos=True)
    _prime_checkout(director, docroot)
    director.vcs_repository.has_ssh_key_with_write_access.return_value = True

    with pytest.raises(BuildUserError) as excinfo:
        director.checkout()
    assert excinfo.value.message_id == BuildUserError.SSH_KEY_WITH_WRITE_ACCESS
    # The mismatch with the API's stored flag is patched back.
    director.data.api_client.project(director.data.project.pk).patch.assert_called()


def test_checkout_ssh_key_write_access_attaches_notification(docroot, monkeypatch):
    monkeypatch.setattr("builder.settings.RTD_ENFORCE_BROWNOUTS_FOR_DEPRECATIONS", False)
    director = make_director(SPHINX, allow_private_repos=True)
    _prime_checkout(director, docroot)
    director.vcs_repository.has_ssh_key_with_write_access.return_value = True

    with mock.patch.object(director, "attach_notification") as notify:
        director.checkout()
    notify.assert_called_once()


def test_checkout_blocks_post_checkout_with_ssh_write_access(docroot, monkeypatch):
    monkeypatch.setattr("builder.settings.RTD_ENFORCE_BROWNOUTS_FOR_DEPRECATIONS", False)
    director = make_director(SPHINX, allow_private_repos=True)
    _prime_checkout(
        director,
        docroot,
        config_yaml=(
            "version: 2\nbuild:\n  os: ubuntu-22.04\n  tools:\n    python: '3'\n"
            "  jobs:\n    post_checkout:\n      - echo hi\nsphinx:\n  configuration: conf.py\n"
        ),
    )
    director.vcs_repository.has_ssh_key_with_write_access.return_value = True

    with mock.patch.object(director, "attach_notification"):
        with pytest.raises(BuildUserError) as excinfo:
            director.checkout()
    # A post_checkout job + write access could push to the repo -> blocked.
    assert excinfo.value.message_id == BuildUserError.SSH_KEY_WITH_WRITE_ACCESS


def test_checkout_uses_a_custom_yaml_path(docroot):
    director = make_director(SPHINX, project={"readthedocs_yaml_path": "docs/custom.yaml"}, allow_private_repos=True)
    checkout = director.data.project.checkout_path(director.data.version.slug)
    os.makedirs(os.path.join(checkout, "docs"), exist_ok=True)
    with open(os.path.join(checkout, "docs/custom.yaml"), "w") as fh:
        fh.write(
            "version: 2\nbuild:\n  os: ubuntu-22.04\n  tools:\n    python: '3'\n"
            "sphinx:\n  configuration: conf.py\n"
        )
    director.checkout()
    assert director.data.build["readthedocs_yaml_path"] == "docs/custom.yaml"


# ---------------------------------------------------------------------------
# Build format job overrides & rust reshim
# ---------------------------------------------------------------------------


def test_build_pdf_job_override_runs_the_job(docroot):
    director = make_director(
        {
            "formats": ["pdf"],
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"build": {"pdf": ["echo pdf"]}},
            },
            **SPHINX,
        }
    )
    with mock.patch.object(director, "run_build_job") as run_job, mock.patch.object(
        director, "build_docs_class"
    ) as build_docs:
        director.build_pdf()
    run_job.assert_called_once_with("build.pdf")
    build_docs.assert_not_called()


@pytest.mark.parametrize(
    "fmt, job_key, builder_cls",
    [
        ("htmlzip", "htmlzip", "sphinx_singlehtmllocalmedia"),
        ("epub", "epub", "sphinx_epub"),
    ],
)
def test_build_format_job_override_runs_the_job(docroot, fmt, job_key, builder_cls):
    director = make_director(
        {
            "formats": [fmt],
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "jobs": {"build": {job_key: ["echo build"]}},
            },
            **SPHINX,
        }
    )
    method = getattr(director, f"build_{fmt}")
    with mock.patch.object(director, "run_build_job") as run_job, mock.patch.object(
        director, "build_docs_class"
    ) as build_docs:
        method()
    run_job.assert_called_once_with(f"build.{job_key}")
    build_docs.assert_not_called()


def test_run_build_commands_reshims_after_cargo_install(docroot):
    director = make_director(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3", "rust": "latest"},
                "commands": ["cargo install mdbook"],
            },
        }
    )
    html_out = os.path.join(director.data.project.checkout_path("latest"), "_readthedocs/html")
    os.makedirs(html_out, exist_ok=True)
    director.run_build_commands()
    reshims = [
        c.args
        for c in director.build_environment.run.call_args_list
        if c.args[:2] == ("asdf", "reshim")
    ]
    assert ("asdf", "reshim", "rust") in reshims


# ---------------------------------------------------------------------------
# install_build_tools — ubuntu-lts-latest
# ---------------------------------------------------------------------------


def test_install_build_tools_caps_setuptools_for_setup_py(docroot):
    # Projects using ``setup.py install`` need setuptools<58.3.0 (issue #8659).
    director = make_director(
        {"python": {"install": [{"path": "pkg", "method": "setuptools"}]}, **SPHINX},
    )
    storage = mock.MagicMock()
    storage.exists.return_value = False
    with mock.patch("builder.storage.get_storage", return_value=storage):
        director.install_build_tools()
    argvs = [c.args for c in director.build_environment.run.call_args_list]
    assert any("setuptools<58.3.0" in a for a in argvs)


def test_install_build_tools_resolves_ubuntu_lts_latest(docroot):
    # The cache key uses the concrete OS, not the rolling ``ubuntu-lts-latest``
    # alias, so the alias can move forward without invalidating the cache.
    director = make_director(
        {"build": {"os": "ubuntu-lts-latest", "tools": {"python": "3"}}, **SPHINX},
    )
    storage = mock.MagicMock()
    storage.exists.return_value = False
    with mock.patch("builder.storage.get_storage", return_value=storage):
        director.install_build_tools()
    tool_path = storage.exists.call_args[0][0]
    assert not tool_path.startswith("ubuntu-lts-latest")
    assert tool_path.startswith("ubuntu-")


# ---------------------------------------------------------------------------
# store_readthedocs_build_yaml — error handling
# ---------------------------------------------------------------------------


def test_store_build_yaml_ignores_malformed_yaml(docroot):
    director = make_director(SPHINX)
    artifact_dir = director.data.project.artifact_path(version="latest", type_="html")
    os.makedirs(artifact_dir, exist_ok=True)
    with open(os.path.join(artifact_dir, "readthedocs-build.yaml"), "w") as fh:
        fh.write("a: b: c: invalid")
    director.store_readthedocs_build_yaml()  # must not raise
    assert director.data.version.build_data is None


def test_ssh_key_directory_is_created_by_the_build_user(docroot):
    """
    Through the environment, not from Python.

    The runner and the build container are the same user in production but not
    in dev, so a directory created here would be root-owned there — and the
    checkout that follows writes into it as ``docs``.
    """
    director = make_director(
        SPHINX, project={"repo": "git@github.com:rtd/private.git"}, allow_private_repos=True
    )
    _run_creating_dirs(director)

    director._write_ssh_key("PRIVATE-KEY-CONTENT")

    mkdir = _calls_named(director, "mkdir")[0]
    assert mkdir.args[-1].endswith("/checkouts")
    # A swallowed failure here surfaces much later, as a checkout that can't
    # create its working directory.
    assert mkdir.kwargs["warn_only"] is False


def test_ssh_key_is_chowned_to_the_build_user_by_id(docroot, monkeypatch):
    """
    By numeric id, because the name may not resolve here.

    The dev compose service has no ``docs`` in its passwd db; a failed lookup
    used to skip the chown silently, leaving ``ssh-add`` unable to read the key.
    """
    director = make_director(
        SPHINX, project={"repo": "git@github.com:rtd/private.git"}, allow_private_repos=True
    )
    _run_creating_dirs(director)

    monkeypatch.setattr("builder.director.pwd.getpwnam", mock.Mock(side_effect=KeyError))
    monkeypatch.setattr("builder.settings.RTD_DOCKER_UID", 1005)
    monkeypatch.setattr("builder.settings.RTD_DOCKER_GID", 205)
    chowned = []
    monkeypatch.setattr("builder.director.os.chown", lambda p, u, g: chowned.append((u, g)))

    director._write_ssh_key("PRIVATE-KEY-CONTENT")

    assert chowned == [(1005, 205)]


# ---------------------------------------------------------------------------
# Uploaded builds (direct artifact upload)
#
# The user uploads a ZIP of an already-built ``_readthedocs/`` tree; the build
# only downloads it from the uploads bucket and unzips it. There is no clone,
# no config file and no doctool run, so the director must not touch anything
# that depends on ``data.config``.
# ---------------------------------------------------------------------------


UPLOADED_BUILD = {
    "id": 42,
    "is_uploaded": True,
    "uploaded_artifacts_storage_path": "uploads/pip/42/artifacts.zip",
}


def test_create_build_environment_uses_only_rtd_env_vars_for_uploaded_builds(docroot):
    # ``get_build_env_vars`` dereferences ``data.config`` (``.conda``,
    # ``.is_using_uv``), which is None for an uploaded build because the config
    # file is never parsed. Only the ``READTHEDOCS_*`` vars are exposed.
    uploaded = make_director(config=None, build=dict(UPLOADED_BUILD))
    uploaded.data.environment_class = mock.MagicMock()
    regular = make_director(SPHINX)
    regular.data.environment_class = mock.MagicMock()

    uploaded.create_build_environment()
    regular.create_build_environment()

    uploaded_env = uploaded.data.environment_class.call_args.kwargs["environment"]
    assert uploaded_env == uploaded.get_rtd_env_vars()
    assert "BIN_PATH" not in uploaded_env
    assert regular.data.environment_class.call_args.kwargs["environment"]["READTHEDOCS"] == "True"


def test_download_artifacts_from_storage(docroot):
    director = make_director(config=None, build=dict(UPLOADED_BUILD))
    storage = mock.MagicMock()
    storage.download_to_path.side_effect = lambda path, destination: Path(
        destination
    ).write_bytes(b"zip-bytes")
    checkout = Path(director.data.project.checkout_path("latest"))
    # Stand in for the real ``mkdir``, which is what gives the write below
    # somewhere to land.
    director.build_environment.run.side_effect = lambda *args, **kwargs: os.makedirs(
        args[2], exist_ok=True
    )

    with mock.patch("builder.storage.get_storage", return_value=storage) as get_storage:
        director.download_artifacts_from_storage()

    assert get_storage.call_args.kwargs["storage_type"] == StorageType.build_uploads
    assert get_storage.call_args.kwargs["build_id"] == 42
    storage.download_to_path.assert_called_once_with(
        "uploads/pip/42/artifacts.zip", checkout / "artifacts.zip"
    )
    # ``_validate_artifacts`` and ``extract_artifacts`` both expect the ZIP at
    # the root of the checkout.
    assert (checkout / "artifacts.zip").read_bytes() == b"zip-bytes"
    # The dir is created through the build environment so it's owned by the
    # build user rather than root.
    call = director.build_environment.run.call_args
    assert call.args == ("mkdir", "-p", str(checkout))
    assert call.kwargs["record"] is False


def test_download_artifacts_propagates_credential_errors(docroot):
    director = make_director(config=None, build=dict(UPLOADED_BUILD))

    with mock.patch(
        "builder.storage.get_storage",
        side_effect=BuildAppError(BuildAppError.GENERIC_WITH_BUILD_ID),
    ):
        with pytest.raises(BuildAppError):
            director.download_artifacts_from_storage()


def test_extract_artifacts_unzips_into_the_output_directory(docroot):
    director = make_director(config=None, build=dict(UPLOADED_BUILD))
    director.build_environment.run.return_value = mock.MagicMock(exit_code=0)

    director.extract_artifacts()

    argvs = [call.args for call in director.build_environment.run.call_args_list]
    # The commands are recorded and shown in the build UI, so they reference the
    # ``READTHEDOCS_*`` variables instead of the expanded paths.
    mkdir = ("mkdir", "-p", "$READTHEDOCS_OUTPUT")
    unzip = ("unzip", "$READTHEDOCS_REPOSITORY_PATH/artifacts.zip", "-d", "$READTHEDOCS_OUTPUT")
    assert argvs.index(mkdir) < argvs.index(unzip)
    # ``run`` raises ``BuildUserError(GENERIC)`` on a failed command unless the
    # caller opts out; without that the specific notification below would be
    # unreachable.
    unzip_call = director.build_environment.run.call_args_list[argvs.index(unzip)]
    assert unzip_call.kwargs.get("warn_only") is True


def test_extract_artifacts_fails_the_build_on_a_corrupt_zip(docroot):
    director = make_director(config=None, build=dict(UPLOADED_BUILD))
    director.build_environment.run.return_value = mock.MagicMock(exit_code=9, output="not a zip")

    with pytest.raises(BuildUserError) as excinfo:
        director.extract_artifacts()

    assert excinfo.value.message_id == BuildUserError.BUILD_ARTIFACTS_ZIP_INVALID
