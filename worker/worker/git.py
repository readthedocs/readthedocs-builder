"""Sparse clone of a project's config file, over HTTPS or SSH."""

import os
import shlex
import subprocess
import tempfile
from contextlib import contextmanager
from urllib.parse import urlparse

from builder.ssh import GIT_SSH_COMMAND
from builder.ssh import parse_ssh_agent_env

from worker import constants
from worker.config import find_config_file
from worker.exceptions import BuildAppError
from worker.exceptions import BuildUserError
from worker.exceptions import PreContainerFailure


def sparse_clone_yaml(
    *, repo_url: str, refspec: str, ssh_key: str, dest: str, env: dict, yaml_path: str | None = None
) -> str | None:
    """
    Clone just the config file from a remote repo into ``dest``.

    Uses ``--filter=blob:none --no-checkout`` so only commit / tree
    metadata is downloaded, then ``sparse-checkout`` to pull just the
    candidate config filenames. Returns the absolute path to the
    downloaded config file, or ``None`` if none of the candidate
    filenames were present.

    ``yaml_path`` is the project's ``readthedocs_yaml_path`` (repo-root
    relative, e.g. ``subpath/docs/.readthedocs.yaml``). When set it's the only
    file fetched and the only one looked for — matching ``builder.config.load``,
    which uses it exclusively and never falls back to the default names.

    Auth:
      - HTTPS repos: the caller puts the token into
        ``env["READTHEDOCS_GIT_CLONE_TOKEN"]`` and the URL string we
        build carries the LITERAL text ``$READTHEDOCS_GIT_CLONE_TOKEN``.
        The shell (``shell=True`` below) expands it at exec time. If
        git echoes the URL on failure, only the placeholder is
        visible in logs — the raw token stays out of stderr and
        argv. Matches ``builder/vcs.py:_get_clone_url``.
      - SSH repos: ``ssh_key`` (from ``/api/v2/project/<pk>/key/``) is
        loaded into a temp ssh-agent, matching
        ``readthedocsinc/projects/ssh.py:setup_ssh_agent``.
        TODO: this can be simplified with ``GIT_SSH_COMMAND=-i <path>``
        for a one-shot clone. Left as ssh-agent for now to match the
        existing pattern so the code shape is copy-pastable when we
        refactor the two clones into one shared helper.
    """
    if not repo_url:
        raise PreContainerFailure(BuildUserError.GENERIC, log_message="Empty repo_url")

    is_ssh = repo_url.startswith("git@") or repo_url.startswith("ssh://")

    if is_ssh:
        return _sparse_clone_yaml_ssh(
            repo_url=repo_url, refspec=refspec, ssh_key=ssh_key, dest=dest, yaml_path=yaml_path
        )
    return _sparse_clone_yaml_https(
        repo_url=repo_url, refspec=refspec, dest=dest, env=env, yaml_path=yaml_path
    )


def _sparse_clone_yaml_https(
    *, repo_url: str, refspec: str, dest: str, env: dict, yaml_path: str | None = None
) -> str | None:
    """
    HTTPS sparse clone.

    The URL carries the literal placeholder ``$READTHEDOCS_GIT_CLONE_TOKEN``;
    the caller sets the real token in ``env`` and the shell (via
    ``shell=True``) expands it at exec time. For public repos with no
    token, ``env["READTHEDOCS_GIT_CLONE_TOKEN"]`` is empty, the URL
    expands to ``https://@host/…``, and git falls back to anonymous.
    """
    parsed = urlparse(repo_url)
    auth_url = f"{parsed.scheme}://$READTHEDOCS_GIT_CLONE_TOKEN@{parsed.netloc}{parsed.path}"
    _run_sparse_clone(auth_url=auth_url, refspec=refspec, dest=dest, env=env, yaml_path=yaml_path)
    return find_config_file(dest, yaml_path=yaml_path)


@contextmanager
def _ssh_agent(ssh_key: str):
    """
    Start an ssh-agent with ``ssh_key`` loaded; yield an env dict for git.

    Matches ``readthedocsinc/projects/ssh.py:setup_ssh_agent``: start an
    ssh-agent, add the private key from a temp file, yield an env carrying the
    agent's ``SSH_AUTH_SOCK`` (+ a prompt-free ``GIT_SSH_COMMAND``), then tear
    the agent down and remove the key file. Shared by the SSH sparse clone and
    the host-side ``lsremote``.
    """
    if not ssh_key:
        raise PreContainerFailure(
            BuildUserError.GENERIC,
            log_message="SSH repo but the project has no ssh key set",
        )

    with tempfile.NamedTemporaryFile("w", delete=False, prefix="rtd-ssh-key-") as key_file:
        key_file.write(ssh_key)
        key_path = key_file.name
    os.chmod(key_path, 0o600)

    agent_env = {}
    agent_started = False
    try:
        # ssh-agent -s prints ``export`` lines. Parse them so we can
        # forward SSH_AUTH_SOCK / SSH_AGENT_PID to git.
        agent_out = subprocess.run(
            ["ssh-agent", "-s"],
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
        subprocess.run(
            ["ssh-add", key_path],
            check=True,
            capture_output=True,
            env=env,
        )

        # Skip host-key prompts — this runs unattended.
        env["GIT_SSH_COMMAND"] = GIT_SSH_COMMAND
        yield env
    finally:
        # Remove the key file first (regardless of what happens next).
        try:
            os.unlink(key_path)
        except FileNotFoundError:
            pass
        # Kill the agent if we managed to start it.
        pid = agent_env.get("SSH_AGENT_PID")
        if agent_started and pid:
            subprocess.run(
                ["ssh-agent", "-k"],
                env={**os.environ, **agent_env},
                check=False,
                capture_output=True,
            )


def _sparse_clone_yaml_ssh(
    *, repo_url: str, refspec: str, ssh_key: str, dest: str, yaml_path: str | None = None
) -> str | None:
    """SSH sparse clone via ssh-agent + ssh-add."""
    with _ssh_agent(ssh_key) as env:
        _run_sparse_clone(
            auth_url=repo_url, refspec=refspec, dest=dest, env=env, yaml_path=yaml_path
        )
    return find_config_file(dest, yaml_path=yaml_path)


def lsremote(*, repo_url: str, ssh_key: str, env: dict, include_tags=True, include_branches=True):
    """
    Run ``git ls-remote`` host-side and return its stdout.

    Used by the worker before the build to sync tags/branches. Auth mirrors
    :func:`sparse_clone_yaml`: an HTTPS URL carrying the
    ``$READTHEDOCS_GIT_CLONE_TOKEN`` placeholder (expanded by the shell from
    ``env``), or an ssh-agent for SSH repos. Returns ``""`` when neither tags
    nor branches are requested.
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

    parsed = urlparse(repo_url)
    auth_url = f"{parsed.scheme}://$READTHEDOCS_GIT_CLONE_TOKEN@{parsed.netloc}{parsed.path}"
    return _run_lsremote(auth_url=auth_url, ref_args=ref_args, env=env)


def _run_lsremote(*, auth_url: str, ref_args: list, env: dict) -> str:
    """Run ``git ls-remote`` under a shell so the token placeholder expands."""
    cmd = f"git ls-remote {' '.join(ref_args)} {auth_url}"
    result = subprocess.run(
        cmd,
        shell=True,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=constants.GIT_CLONE_TIMEOUT_SECONDS,
    )
    return result.stdout


def _run_sparse_clone(
    *,
    auth_url: str,
    refspec: str,
    dest: str,
    env: dict,
    yaml_path: str | None = None,
) -> None:
    """
    The four git commands that make up a config-file-only clone.

    Runs with ``shell=True`` so ``$READTHEDOCS_GIT_CLONE_TOKEN`` in
    ``auth_url`` expands against ``env`` when the caller is the HTTPS
    path. Same shape works for SSH (no ``$`` in the URL, auth comes
    from ``SSH_AUTH_SOCK`` in ``env``). Matches the shell-based
    invocation pattern used in ``readthedocs.doc_builder.environments``.
    """
    # shlex.quote: ``yaml_path`` and ``refspec`` are user-controlled (a project
    # setting / branch / PR number) and this runs under ``shell=True`` on the
    # HOST, outside the build container.
    paths = (yaml_path,) if yaml_path else constants.CONFIG_FILENAMES
    files = " ".join(shlex.quote(path) for path in paths)
    # Fetch-based rather than ``git clone -b <ref>``: ``-b`` only accepts a
    # branch/tag *name*, but for tags and external (PR/MR) versions the ref is
    # a commit hash or a ``pull/<id>/head`` refspec. We clone the default branch
    # (cheaply — blob:none, depth 1, no checkout) to set up the partial-clone
    # promisor, then fetch the exact refspec and check out ``FETCH_HEAD``.
    for cmd in (
        f"git clone --filter=blob:none --no-checkout --depth=1 {auth_url} {dest}",
        f"git -C {dest} fetch --filter=blob:none --depth=1 origin {shlex.quote(refspec)}",
        f"git -C {dest} sparse-checkout init --no-cone",
        f"git -C {dest} sparse-checkout set {files}",
        f"git -C {dest} checkout FETCH_HEAD",
    ):
        subprocess.run(
            cmd,
            shell=True,
            check=True,
            capture_output=True,
            env=env,
            timeout=constants.GIT_CLONE_TIMEOUT_SECONDS,
        )
