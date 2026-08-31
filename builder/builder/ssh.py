"""
Shared SSH helpers for cloning private repositories over SSH.

Used by both the worker (host-side sparse clone / ls-remote in
``worker/git.py``) and the runner (in-container clone in
``builder/director.py``). Keeping the ``GIT_SSH_COMMAND`` string and the
``ssh-agent -s`` output parsing here stops the two from diverging.
"""

import re


# Passed as ``GIT_SSH_COMMAND`` so git's ssh runs unattended: no host-key
# prompt (the clone is non-interactive) and no known_hosts writes.
GIT_SSH_COMMAND = "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"


# ``ssh-agent -s`` prints shell ``export`` lines, e.g.::
#
#     SSH_AUTH_SOCK=/tmp/ssh-XXXX/agent.123; export SSH_AUTH_SOCK;
#     SSH_AGENT_PID=124; export SSH_AGENT_PID;
#     echo Agent pid 124;
_AGENT_ENV_LINE = re.compile(r"^([A-Z_]+)=([^;]+);")


def parse_ssh_agent_env(output: str) -> dict:
    """Parse ``ssh-agent -s`` output into an env dict (``SSH_AUTH_SOCK`` etc.)."""
    env = {}
    for line in output.splitlines():
        match = _AGENT_ENV_LINE.match(line.strip())
        if match:
            env[match.group(1)] = match.group(2)
    return env
