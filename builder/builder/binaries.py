"""
Absolute paths of the host binaries the worker and runner execute.

Explicit paths so a build never depends on ``PATH`` lookup on the host.
All of them come from apt on the builder AMI, hence ``/usr/bin``.
"""

GIT = "/usr/bin/git"
SSH = "/usr/bin/ssh"
SSH_AGENT = "/usr/bin/ssh-agent"
SSH_ADD = "/usr/bin/ssh-add"
DU = "/usr/bin/du"
RCLONE = "/usr/bin/rclone"
