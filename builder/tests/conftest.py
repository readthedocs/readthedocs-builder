"""Shared helpers for the builder test suite."""

import subprocess
import textwrap
import types

import pytest

from builder.api_models import APIProject
from builder.api_models import APIVersion
from builder.config import BuildConfigV2
from builder.environments import BuildEnvironment


def apply_fs(tmpdir, contents):
    """
    Create the directory structure specified in ``contents``.

    It's a dict of filenames as keys and the file contents as values. If the
    value is another dict, it's a subdirectory.
    """
    for filename, content in contents.items():
        if hasattr(content, "items"):
            apply_fs(tmpdir.mkdir(filename), content)
        else:
            file = tmpdir.join(filename)
            file.write(content)
    return tmpdir


def get_build_config(
    config, source_file="readthedocs.yml", validate=False, require_config=False, **kwargs
):
    """
    Build a :class:`BuildConfigV2` from a partial config dict.

    ``version`` and ``build`` are filled in with valid defaults so individual
    tests only have to declare the key under test.

    ``require_config`` defaults to ``False`` here (unlike production) so tests
    focused on other keys don't have to carry a ``sphinx.configuration``; the
    tests that exercise the requirement pass ``require_config=True``.
    """
    final_config = {
        "version": "2",
        "build": {
            "os": "ubuntu-22.04",
            "tools": {
                "python": "3",
            },
        },
    }
    final_config.update(config)

    build_config = BuildConfigV2(
        final_config,
        source_file=source_file,
        require_config=require_config,
        **kwargs,
    )
    if validate:
        build_config.validate()

    return build_config


# ---------------------------------------------------------------------------
# Git VCS helpers
#
# Ported from ``readthedocs/rtd_tests/utils.py`` (``make_test_git`` &
# friends), but driven with ``git -C <dir>`` instead of the upstream
# ``chdir`` + ``GIT_DIR`` dance so there's no global CWD state to restore.
# ---------------------------------------------------------------------------


def git(directory, *args):
    """Run a git command inside ``directory`` and return its stdout."""
    return subprocess.check_output(
        ["git", "-C", str(directory), *args],
        stderr=subprocess.STDOUT,
    ).decode()


def init_git_repo(directory):
    """Initialize an empty git repo on ``master`` with a single commit."""
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "--initial-branch=master")
    git(directory, "config", "user.email", "dev@readthedocs.org")
    git(directory, "config", "user.name", "Read the Docs")
    (directory / "README").write_text("Sample repo\n")
    git(directory, "add", ".")
    git(directory, "commit", "-m", "init")
    return directory


def add_submodule_without_cloning(directory, submodule, url):
    """
    Register a submodule by writing ``.gitmodules`` + a gitlink to the index.

    Avoids a real clone (which would need network / a real submodule repo) by
    writing a fake commit gitlink straight into the index, exactly as upstream
    does. The commit hash is arbitrary and never dereferenced by these tests.
    """
    (directory / submodule).mkdir(exist_ok=True)
    gitmodules = directory / ".gitmodules"
    gitmodules.write_text(
        textwrap.dedent(
            f"""
            [submodule "{submodule}"]
                path = {submodule}
                url = {url}
            """
        )
    )
    git(
        directory,
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        "233febf4846d7a0aeb95b6c28962e06e21d13688",
        submodule,
    )


def current_commit(directory):
    """Return the full HEAD commit hash of ``directory``."""
    return git(directory, "rev-parse", "HEAD").strip()


@pytest.fixture
def git_repo(tmp_path):
    """
    A remote-like git repo with ``master`` + a ``submodule`` branch.

    Mirrors ``make_test_git``:

    - ``master`` carries a ``README`` and an ``only-on-default-branch`` marker.
    - ``submodule`` adds one valid submodule (``foobar``).
    """
    repo = init_git_repo(tmp_path / "remote_repo")

    # A branch carrying a valid submodule.
    git(repo, "checkout", "-b", "submodule", "master")
    add_submodule_without_cloning(repo, "foobar", "https://foobar.com/git")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Add submodule")

    # Back to master, add a file unique to the default branch.
    git(repo, "checkout", "master")
    (repo / "only-on-default-branch").write_text("only on the default branch\n")
    git(repo, "add", "only-on-default-branch")
    git(repo, "commit", "-m", "Add default-branch marker")

    return repo


@pytest.fixture
def docroot(tmp_path, monkeypatch):
    """Point ``settings.DOCROOT`` at a writable temp dir for checkout paths."""
    root = tmp_path / "docroot"
    root.mkdir()
    monkeypatch.setattr("builder.settings.DOCROOT", str(root))
    return root


def make_backend(
    remote_repo,
    *,
    version_type="branch",
    slug="master",
    verbose_name="master",
    identifier="master",
    machine=False,
    default_branch=None,
    repo=None,
    record=False,
    allow_private_repos=False,
    **project_extra,
):
    """
    Build a git :class:`Backend` bound to a real repo and a non-recording env.

    ``record=False`` means the environment needs no API client, yet commands
    that don't pass ``record=False`` still raise on failure (the per-command
    ``record`` kwarg defaults to ``True``) — the same effect upstream gets by
    mocking ``BuildCommand.save``.
    """
    project = APIProject(
        slug="test-project",
        name="Test Project",
        repo=repo if repo is not None else str(remote_repo),
        repo_type="git",
        default_branch=default_branch,
        **project_extra,
    )
    version = APIVersion(
        slug=slug,
        type=version_type,
        verbose_name=verbose_name,
        identifier=identifier,
        machine=machine,
    )
    environment = BuildEnvironment(
        project=project,
        version=version,
        record=record,
        allow_private_repos=allow_private_repos,
    )
    return project.vcs_repo(environment=environment, version=version)


def make_builder(
    builder_cls,
    *,
    config=None,
    env_cls=None,
    language="en",
    features=None,
):
    """
    Build a doc backend (Sphinx/MkDocs) wired to a non-recording environment.

    ``config`` is a partial config dict fed through ``get_build_config``;
    ``env_cls`` is the python-environment class (``Virtualenv`` by default,
    ``UvEnv`` for uv-managed builds).
    """
    from builder.python_envs import Virtualenv

    env_cls = env_cls or Virtualenv
    version = APIVersion(
        slug="latest",
        type="branch",
        verbose_name="latest",
        project={
            "slug": "pip",
            "name": "Pip",
            "language": language,
            "features": features or [],
        },
    )
    project = version.project
    environment = BuildEnvironment(project=project, version=version, record=False)
    build_config = get_build_config(config or {}, validate=True)
    python_env = env_cls(version=version, build_env=environment, config=build_config)
    return builder_cls(build_env=environment, python_env=python_env)


def make_python_env(env_cls, config, *, language="en", features=None, source_file="readthedocs.yml"):
    """
    Build a python-environment (Virtualenv/UvEnv/Conda) wired to a real config.

    ``config`` is a partial config dict fed through ``get_build_config``. Tests
    patch ``env.build_env.run`` to capture the emitted command sequences.
    """
    version = APIVersion(
        slug="latest",
        type="branch",
        verbose_name="latest",
        project={
            "slug": "pip",
            "name": "Pip",
            "language": language,
            "features": features or [],
        },
    )
    environment = BuildEnvironment(project=version.project, version=version, record=False)
    build_config = get_build_config(config, source_file=source_file, validate=True)
    return env_cls(version=version, build_env=environment, config=build_config)


def make_director(
    config=None, *, project=None, version=None, build=None, allow_private_repos=False
):
    """
    Build a :class:`BuildDirector` with real project/version/config and mocked
    collaborators.

    ``config`` is a partial config dict (defaults to a minimal sphinx config).
    The VCS repo, both build environments, and the language environment are
    pre-attached as ``MagicMock``s; tests override whichever they exercise.
    Requires the ``docroot`` fixture to be active for checkout paths.
    """
    from unittest import mock

    from builder.director import BuildDirector
    from builder.director import TaskData

    project_payload = {
        "slug": "pip",
        "name": "Pip",
        "language": "en",
        "repo": "https://github.com/readthedocs/pip",
        "clone_token": None,
        "features": [],
    }
    project_payload.update(project or {})
    version_payload = {
        "slug": "latest",
        "type": "branch",
        "verbose_name": "latest",
        "identifier": "abc123",
        "git_identifier": "abc123",
        "canonical_url": "https://pip.readthedocs.io/en/latest/",
        "machine": False,
    }
    version_payload.update(version or {})
    version_payload["project"] = project_payload
    api_version = APIVersion(**version_payload)

    data = TaskData(
        project=api_version.project,
        version=api_version,
        build=build if build is not None else {"id": 1},
        api_client=mock.MagicMock(),
        production_domain="readthedocs.org",
        allow_private_repos=allow_private_repos,
    )
    if config is not None:
        data.config = get_build_config(config, validate=True)

    director = BuildDirector(data)
    data.build_director = director
    director.vcs_repository = mock.MagicMock()
    director.vcs_environment = mock.MagicMock()
    director.build_environment = mock.MagicMock()
    director.language_environment = mock.MagicMock()
    return director


def submodules_config(include=None, exclude=None, recursive=False):
    """A minimal stand-in for the parsed ``submodules`` config block."""
    from builder.constants import ALL

    return types.SimpleNamespace(
        submodules=types.SimpleNamespace(
            include=ALL if include is None else include,
            exclude=[] if exclude is None else exclude,
            recursive=recursive,
        )
    )
