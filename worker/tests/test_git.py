import os
import subprocess
import tempfile
from unittest import mock

import pytest

from builder.lsremote import parse_lsremote
from builder.ssh import GIT_SSH_COMMAND

from worker.config import read_build_os
from worker.exceptions import BuildAppError
from worker.exceptions import BuildUserError
from worker.exceptions import PreContainerFailure
from worker.git import _run_lsremote
from worker.git import _run_sparse_clone
from worker.git import _sparse_clone_yaml_https
from worker.git import _ssh_agent
from worker.git import lsremote
from worker.git import sparse_clone_yaml


@pytest.fixture
def origin(tmp_path, write_config):
    """A real git repo with a root config and one in a subpath."""
    repo = tmp_path / "origin"
    repo.mkdir()
    write_config(repo / ".readthedocs.yaml", {"version": 2, "build": {"os": "ubuntu-22.04"}})
    write_config(
        repo / "subpath" / "docs" / ".readthedocs.yaml",
        {"version": 2, "build": {"os": "ubuntu-24.04"}},
    )
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run([*git, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-qm", "init"], check=True)
    return str(repo)


def clone(origin, dest, yaml_path=None, refspec="main"):
    _run_sparse_clone(
        auth_url=origin, refspec=refspec, dest=str(dest), env={**os.environ}, yaml_path=yaml_path
    )


def test_sparse_clone_fetches_the_root_config(origin, tmp_path):
    dest = tmp_path / "dest"
    clone(origin, dest)

    assert read_build_os(str(dest / ".readthedocs.yaml")) == "ubuntu-22.04"


def test_sparse_clone_fetches_a_non_default_ref(origin, tmp_path, write_config):
    """
    A refspec that isn't the default branch (here a tag, standing in for a
    tag/external version whose identifier is a commit hash) must be fetched and
    checked out.
    """
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", origin]
    write_config(f"{origin}/.readthedocs.yaml", {"version": 2, "build": {"os": "ubuntu-24.04"}})
    subprocess.run([*git, "commit", "-aqm", "bump os"], check=True)
    subprocess.run([*git, "tag", "v1"], check=True)
    # Move ``main`` forward so the tag is no longer the tip of the default branch.
    write_config(f"{origin}/.readthedocs.yaml", {"version": 2, "build": {"os": "ubuntu-20.04"}})
    subprocess.run([*git, "commit", "-aqm", "move main"], check=True)

    dest = tmp_path / "dest"
    clone(origin, dest, refspec="refs/tags/v1:refs/tags/v1")

    # We got the tag's config (24.04), not the default branch tip (20.04).
    assert read_build_os(str(dest / ".readthedocs.yaml")) == "ubuntu-24.04"


def test_sparse_clone_fetches_only_the_custom_yaml_path(origin, tmp_path):
    dest = tmp_path / "dest"
    clone(origin, dest, yaml_path="subpath/docs/.readthedocs.yaml")

    assert (dest / "subpath" / "docs" / ".readthedocs.yaml").is_file()
    assert not (dest / ".readthedocs.yaml").exists()


def test_sparse_clone_does_not_fetch_the_whole_repo(origin, tmp_path):
    """``--filter=blob:none`` + sparse-checkout: only the config lands."""
    dest = tmp_path / "dest"
    clone(origin, dest)

    files = {
        os.path.relpath(os.path.join(root, f), dest)
        for root, _, fs in os.walk(dest)
        if ".git" not in root
        for f in fs
    }
    assert files == {".readthedocs.yaml", "subpath/docs/.readthedocs.yaml"}


def test_sparse_clone_quotes_the_yaml_path_against_shell_injection(origin, tmp_path):
    """
    ``yaml_path`` is user-controlled (``Project.readthedocs_yaml_path``) and the
    clone runs under ``shell=True`` on the host, outside the build container.

    This value passes readthedocs.org's ``validate_build_config_file``: no
    leading/trailing '/', no '..', none of ``[]{}()`'"\\%&<>|,`` and it ends
    with '/.readthedocs.yaml'.

    Quoted, git just receives a sparse-checkout pattern that matches nothing —
    the clone succeeds and the injected command never runs.
    """
    dest = tmp_path / "dest"
    marker = tmp_path / "pwned"
    evil = f"a;touch {marker};b/.readthedocs.yaml"

    clone(origin, dest, yaml_path=evil)

    assert not marker.exists()


def test_run_lsremote_lists_heads_and_tags(origin):
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", origin]
    subprocess.run([*git, "tag", "v1.0"], check=True)

    stdout = _run_lsremote(
        auth_url=origin, ref_args=["--heads", "--tags"], env={**os.environ}
    )
    branches, tags = parse_lsremote(stdout)

    assert ("main", "main") in branches
    assert "v1.0" in [name for _, name in tags]


def test_run_lsremote_resolves_an_annotated_tag_to_its_commit(origin):
    """An annotated tag is listed as both ``<tag>`` and ``<tag>^{}``; the parser
    must resolve it to the dereferenced commit, not the tag object's own hash."""
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", origin]
    subprocess.run([*git, "tag", "-a", "v2.0", "-m", "release"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    stdout = _run_lsremote(
        auth_url=origin, ref_args=["--heads", "--tags"], env={**os.environ}
    )
    _, tags = parse_lsremote(stdout)

    assert (head, "v2.0") in tags


def test_sparse_clone_yaml_raises_on_empty_repo_url(tmp_path):
    with pytest.raises(PreContainerFailure) as excinfo:
        sparse_clone_yaml(
            repo_url="", refspec="main", ssh_key="", dest=str(tmp_path), env={}
        )

    assert excinfo.value.message_id == BuildUserError.GENERIC


def test_sparse_clone_yaml_requires_an_ssh_key_for_ssh_repos(tmp_path):
    with pytest.raises(PreContainerFailure) as excinfo:
        sparse_clone_yaml(
            repo_url="git@github.com:readthedocs/readthedocs.org.git",
            refspec="main",
            ssh_key="",
            dest=str(tmp_path),
            env={},
        )

    assert excinfo.value.message_id == BuildUserError.GENERIC


# ---------------------------------------------------------------------------
# _sparse_clone_yaml_https
# ---------------------------------------------------------------------------


def test_https_clone_keeps_the_token_out_of_the_command(tmp_path):
    """
    The URL must carry the LITERAL ``$READTHEDOCS_GIT_CLONE_TOKEN`` placeholder.

    The real token only ever reaches git through the environment, expanded by
    the shell at exec time, so it can't leak into argv or into git's stderr if
    the clone fails.
    """
    env = {"READTHEDOCS_GIT_CLONE_TOKEN": "s3cr3t-token"}

    with mock.patch("worker.git._run_sparse_clone") as run:
        _sparse_clone_yaml_https(
            repo_url="https://github.com/readthedocs/readthedocs.org.git",
            refspec="main",
            dest=str(tmp_path),
            env=env,
        )

    auth_url = run.call_args.kwargs["auth_url"]
    assert auth_url == (
        "https://$READTHEDOCS_GIT_CLONE_TOKEN@github.com/readthedocs/readthedocs.org.git"
    )
    assert "s3cr3t-token" not in auth_url
    # The token travels in the environment instead.
    assert run.call_args.kwargs["env"] == env


def test_https_clone_forwards_refspec_dest_and_yaml_path(tmp_path):
    with mock.patch("worker.git._run_sparse_clone") as run:
        _sparse_clone_yaml_https(
            repo_url="https://gitlab.com/group/project",
            refspec="refs/tags/v1:refs/tags/v1",
            dest=str(tmp_path),
            env={},
            yaml_path="docs/.readthedocs.yaml",
        )

    kwargs = run.call_args.kwargs
    assert kwargs["auth_url"] == "https://$READTHEDOCS_GIT_CLONE_TOKEN@gitlab.com/group/project"
    assert kwargs["refspec"] == "refs/tags/v1:refs/tags/v1"
    assert kwargs["dest"] == str(tmp_path)
    assert kwargs["yaml_path"] == "docs/.readthedocs.yaml"


def test_https_clone_returns_the_config_it_downloaded(tmp_path, write_config):
    write_config(tmp_path / ".readthedocs.yaml", {"version": 2})

    with mock.patch("worker.git._run_sparse_clone"):
        found = _sparse_clone_yaml_https(
            repo_url="https://github.com/rtd/pip.git",
            refspec="main",
            dest=str(tmp_path),
            env={},
        )

    assert found == str(tmp_path / ".readthedocs.yaml")


def test_https_clone_returns_none_when_the_repo_has_no_config(tmp_path):
    with mock.patch("worker.git._run_sparse_clone"):
        found = _sparse_clone_yaml_https(
            repo_url="https://github.com/rtd/pip.git",
            refspec="main",
            dest=str(tmp_path),
            env={},
        )

    assert found is None


# ---------------------------------------------------------------------------
# lsremote
# ---------------------------------------------------------------------------


def test_lsremote_raises_on_empty_repo_url():
    with pytest.raises(PreContainerFailure) as excinfo:
        lsremote(repo_url="", ssh_key="", env={})

    assert excinfo.value.message_id == BuildUserError.GENERIC


def test_lsremote_returns_empty_when_nothing_is_requested():
    # Nothing to ask git for(no tags, no branches) -> don't shell out at all.
    with mock.patch("worker.git._run_lsremote") as run:
        result = lsremote(
            repo_url="https://github.com/rtd/pip.git",
            ssh_key="",
            env={},
            include_tags=False,
            include_branches=False,
        )

    assert result == ""
    run.assert_not_called()


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, ["--heads", "--tags"]),
        ({"include_tags": False}, ["--heads"]),
        ({"include_branches": False}, ["--tags"]),
    ],
)
def test_lsremote_selects_the_requested_refs(kwargs, expected):
    with mock.patch("worker.git._run_lsremote") as run:
        lsremote(repo_url="https://github.com/rtd/pip.git", ssh_key="", env={}, **kwargs)

    assert run.call_args.kwargs["ref_args"] == expected


def test_lsremote_over_https_uses_the_token_placeholder():
    env = {"READTHEDOCS_GIT_CLONE_TOKEN": "s3cr3t-token"}

    with mock.patch("worker.git._run_lsremote", return_value="out") as run:
        result = lsremote(
            repo_url="https://github.com/readthedocs/readthedocs.org.git", ssh_key="", env=env
        )

    assert result == "out"
    auth_url = run.call_args.kwargs["auth_url"]
    assert auth_url == (
        "https://$READTHEDOCS_GIT_CLONE_TOKEN@github.com/readthedocs/readthedocs.org.git"
    )
    assert "s3cr3t-token" not in auth_url


def test_lsremote_over_ssh_runs_inside_an_agent():
    # SSH repos: the URL is used as-is and auth comes from the agent's env.
    agent_env = {"SSH_AUTH_SOCK": "/tmp/agent.42"}
    agent = mock.MagicMock()
    agent.return_value.__enter__.return_value = agent_env

    with mock.patch("worker.git._ssh_agent", agent), mock.patch(
        "worker.git._run_lsremote", return_value="out"
    ) as run:
        result = lsremote(
            repo_url="git@github.com:readthedocs/readthedocs.org.git",
            ssh_key="PRIVATE-KEY",
            env={},
        )

    assert result == "out"
    agent.assert_called_once_with("PRIVATE-KEY")
    assert run.call_args.kwargs["auth_url"] == "git@github.com:readthedocs/readthedocs.org.git"
    assert run.call_args.kwargs["env"] == agent_env


def test_sparse_clone_yaml_dispatches_https_repos(tmp_path, write_config):
    write_config(tmp_path / ".readthedocs.yaml", {"version": 2})

    with mock.patch("worker.git._run_sparse_clone") as run:
        found = sparse_clone_yaml(
            repo_url="https://github.com/rtd/pip.git",
            refspec="main",
            ssh_key="",
            dest=str(tmp_path),
            env={},
        )

    assert found == str(tmp_path / ".readthedocs.yaml")
    assert run.call_args.kwargs["auth_url"].startswith("https://$READTHEDOCS_GIT_CLONE_TOKEN@")


def test_sparse_clone_yaml_dispatches_ssh_repos(tmp_path, write_config):
    # SSH: the URL goes through untouched and auth comes from the agent's env.
    write_config(tmp_path / ".readthedocs.yaml", {"version": 2})
    agent_env = {"SSH_AUTH_SOCK": "/tmp/agent.42"}
    agent = mock.MagicMock()
    agent.return_value.__enter__.return_value = agent_env

    with mock.patch("worker.git._ssh_agent", agent), mock.patch(
        "worker.git._run_sparse_clone"
    ) as run:
        found = sparse_clone_yaml(
            repo_url="git@github.com:rtd/pip.git",
            refspec="main",
            ssh_key="PRIVATE-KEY",
            dest=str(tmp_path),
            env={},
        )

    assert found == str(tmp_path / ".readthedocs.yaml")
    agent.assert_called_once_with("PRIVATE-KEY")
    assert run.call_args.kwargs["auth_url"] == "git@github.com:rtd/pip.git"
    assert run.call_args.kwargs["env"] == agent_env
