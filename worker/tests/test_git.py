import os
import subprocess
from unittest import mock

import pytest
from builder.lsremote import parse_lsremote
from builder.ssh import GIT_SSH_COMMAND
from worker.exceptions import BuildUserError
from worker.exceptions import PreContainerFailure
from worker.git import _run_lsremote
from worker.git import _ssh_agent
from worker.git import lsremote

from builder import binaries


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


def test_run_lsremote_does_not_run_shell_code_in_the_repo_url(origin, tmp_path):
    marker = tmp_path / "pwned"

    with pytest.raises(subprocess.CalledProcessError):
        _run_lsremote(auth_url=f"{origin};touch {marker}", ref_args=["--heads"], env={**os.environ})

    assert not marker.exists()


def test_run_lsremote_lists_heads_and_tags(origin):
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", origin]
    subprocess.run([*git, "tag", "v1.0"], check=True)

    stdout = _run_lsremote(auth_url=origin, ref_args=["--heads", "--tags"], env={**os.environ})
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

    stdout = _run_lsremote(auth_url=origin, ref_args=["--heads", "--tags"], env={**os.environ})
    _, tags = parse_lsremote(stdout)

    assert (head, "v2.0") in tags


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


def test_lsremote_over_https_puts_the_token_in_the_url():
    env = {"READTHEDOCS_GIT_CLONE_TOKEN": "s3cr3t-token"}

    with mock.patch("worker.git._run_lsremote", return_value="out") as run:
        result = lsremote(
            repo_url="https://github.com/readthedocs/readthedocs.org.git", ssh_key="", env=env
        )

    assert result == "out"
    auth_url = run.call_args.kwargs["auth_url"]
    assert auth_url == "https://s3cr3t-token@github.com/readthedocs/readthedocs.org.git"


def test_lsremote_over_ssh_runs_inside_an_agent():
    # SSH repos: the URL is used as-is and auth comes from the agent's env.
    agent_env = {"SSH_AUTH_SOCK": "/tmp/agent.42"}
    agent = mock.MagicMock()
    agent.return_value.__enter__.return_value = agent_env

    with (
        mock.patch("worker.git._ssh_agent", agent),
        mock.patch("worker.git._run_lsremote", return_value="out") as run,
    ):
        result = lsremote(
            repo_url="git@github.com:readthedocs/readthedocs.org.git",
            ssh_key="PRIVATE-KEY",
            env={},
        )

    assert result == "out"
    agent.assert_called_once_with("PRIVATE-KEY")
    assert run.call_args.kwargs["auth_url"] == "git@github.com:readthedocs/readthedocs.org.git"
    assert run.call_args.kwargs["env"] == agent_env


# ---------------------------------------------------------------------------
# _ssh_agent
# ---------------------------------------------------------------------------

AGENT_OUT = (
    "SSH_AUTH_SOCK=/tmp/agent.42; export SSH_AUTH_SOCK;\nSSH_AGENT_PID=42; export SSH_AGENT_PID;\n"
)


@pytest.fixture
def fake_run():
    """Stub ``subprocess.run`` in ``worker.git``; fakes ``ssh-agent -s`` and records calls."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return mock.Mock(stdout=AGENT_OUT if cmd[-1] == "-s" else "")

    with mock.patch("worker.git.subprocess.run", run):
        yield calls


@pytest.mark.parametrize("key", ["PRIVATE-KEY", "PRIVATE-KEY\n"])
def test_ssh_agent_feeds_the_key_to_ssh_add_on_stdin(fake_run, key):
    with _ssh_agent(key) as env:
        pass

    ((cmd, kwargs),) = [call for call in fake_run if call[0][0] == binaries.SSH_ADD]
    assert cmd == [binaries.SSH_ADD, "-"]
    # ssh-add needs exactly one trailing newline.
    assert kwargs["input"] == "PRIVATE-KEY\n"
    assert kwargs["env"]["SSH_AUTH_SOCK"] == "/tmp/agent.42"
    assert env["SSH_AUTH_SOCK"] == "/tmp/agent.42"
    assert env["GIT_SSH_COMMAND"] == GIT_SSH_COMMAND


def test_ssh_agent_kills_the_agent_on_exit(fake_run):
    with _ssh_agent("PRIVATE-KEY"):
        pass

    cmd, kwargs = fake_run[-1]
    assert cmd == [binaries.SSH_AGENT, "-k"]
    assert kwargs["env"]["SSH_AGENT_PID"] == "42"
