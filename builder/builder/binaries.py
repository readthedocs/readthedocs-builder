"""
Absolute paths of the binaries the worker and runner execute.

Explicit paths so no command depends on ``PATH`` lookup. On the host, all of
them come from apt on the builder AMI. In the build container, the image puts
``/home/docs/.asdf/{shims,bin}`` first in ``PATH`` and ``docker exec`` hands
that to every user, root included, so a ``build.jobs`` hook could shadow a
bare name that a later super-user exec runs.
"""

# Host (worker + in-process runner).
GIT = "/usr/bin/git"
SSH = "/usr/bin/ssh"
SSH_AGENT = "/usr/bin/ssh-agent"
SSH_ADD = "/usr/bin/ssh-add"
DU = "/usr/bin/du"
RCLONE = "/usr/bin/rclone"

# Build container.
NICE = "/usr/bin/nice"
APT_GET = "/usr/bin/apt-get"
CHOWN = "/usr/bin/chown"
# PATH for super-user execs: system directories only.
SUPER_USER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
