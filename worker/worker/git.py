"""Host-side git: ``ls-remote`` over HTTPS or SSH, for the version sync."""

import os
import subprocess
from contextlib import contextmanager
from urllib.parse import urlparse

from builder.ssh import GIT_SSH_COMMAND
from builder.ssh import parse_ssh_agent_env

from builder import binaries
from worker import constants
from worker.exceptions import BuildAppError
from worker.exceptions import BuildUserError
from worker.exceptions import PreContainerFailure


def _with_clone_token(repo_url: str, env: dict) -> str:
    """
    Put ``env["READTHEDOCS_GIT_CLONE_TOKEN"]`` into the URL's userinfo.

    For public repos the token is empty, the URL becomes ``https://@host/…``
    and git falls back to anonymous access. Never log the result.
    """
    parsed = urlparse(repo_url)
    token = env.get("READTHEDOCS_GIT_CLONE_TOKEN", "")
    return f"{parsed.scheme}://{token}@{parsed.netloc}{parsed.path}"


@contextmanager
def _ssh_agent(ssh_key: str):
    """
    Start an ssh-agent with ``ssh_key`` loaded; yield an env dict for git.

    Matches ``readthedocsinc/projects/ssh.py:setup_ssh_agent``: start an
    ssh-agent, feed the private key to ``ssh-add`` on stdin (it never touches
    disk), yield an env carrying the agent's ``SSH_AUTH_SOCK`` (+ a prompt-free
    ``GIT_SSH_COMMAND``), then tear the agent down.
    """
    if not ssh_key:
        raise PreContainerFailure(
            BuildUserError.GENERIC,
            log_message="SSH repo but the project has no ssh key set",
        )

    agent_env = {}
    agent_started = False
    try:
        # ssh-agent -s prints ``export`` lines. Parse them so we can
        # forward SSH_AUTH_SOCK / SSH_AGENT_PID to git.
        agent_out = subprocess.run(
            [binaries.SSH_AGENT, "-s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        agent_env = parse_ssh_agent_env(agent_out)
        if not agent_env.get("SSH_AUTH_SOCK"):
            raise PreContainerFailure(
                BuildAppError.GENERIC_WITH_BUILD_ID,
                log_message=f"ssh-agent -s did not report SSH_AUTH_SOCK: {agent_out!r}",
            )
        agent_started = True

        env = {**os.environ, **agent_env}
        # ssh-add rejects a key without a trailing newline.
        key_stdin = ssh_key if ssh_key.endswith("\n") else ssh_key + "\n"
        subprocess.run(
            [binaries.SSH_ADD, "-"],
            input=key_stdin,
            text=True,
            check=True,
            capture_output=True,
            env=env,
        )

        # Skip host-key prompts — this runs unattended.
        env["GIT_SSH_COMMAND"] = GIT_SSH_COMMAND
        yield env
    finally:
        # Kill the agent if we managed to start it.
        pid = agent_env.get("SSH_AGENT_PID")
        if agent_started and pid:
            subprocess.run(
                [binaries.SSH_AGENT, "-k"],
                env={**os.environ, **agent_env},
                check=False,
                capture_output=True,
            )


def lsremote(*, repo_url: str, ssh_key: str, env: dict, include_tags=True, include_branches=True):
    """
    Run ``git ls-remote`` host-side and return its stdout.

    Used by the worker to sync tags/branches. Auth: the clone token from
    ``env`` in the HTTPS URL, or an ssh-agent for SSH repos. Returns ``""`` when neither tags nor
    branches are requested.
    """
    if not repo_url:
        raise PreContainerFailure(BuildUserError.GENERIC, log_message="Empty repo_url")

    ref_args = []
    if include_branches:
        ref_args.append("--heads")
    if include_tags:
        ref_args.append("--tags")
    if not ref_args:
        return ""

    if repo_url.startswith("git@") or repo_url.startswith("ssh://"):
        with _ssh_agent(ssh_key) as agent_env:
            return _run_lsremote(auth_url=repo_url, ref_args=ref_args, env=agent_env)

    auth_url = _with_clone_token(repo_url, env)
    return _run_lsremote(auth_url=auth_url, ref_args=ref_args, env=env)


def _run_lsremote(*, auth_url: str, ref_args: list, env: dict) -> str:
    """Run ``git ls-remote`` and return its stdout."""
    result = subprocess.run(
        [binaries.GIT, "ls-remote", *ref_args, "--", auth_url],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=constants.GIT_CLONE_TIMEOUT_SECONDS,
    )
    return result.stdout
