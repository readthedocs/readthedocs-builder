"""
Celery app for the build-isolated worker.

Configuration comes entirely from environment variables (baked into the
AMI at ``/etc/readthedocs-celery-worker.env`` and loaded by the systemd
unit's ``EnvironmentFile`` directive). No Django, no settings module.

Environment variables (all required unless noted):

- ``RTD_BROKER_URL`` — the Redis URL the readthedocs.org
  web/celery containers also use. The ``build-isolated`` ASG lives
  in the same VPC as the broker so this is a private address.
- ``RTD_BUILDS_TASK_TIME_LIMIT`` — absolute last-resort task timeout in
  seconds, for a worker hung outside the per-build timeouts. The real ceiling
  is the per-build ``project.container_time_limit``; this is only a backstop.
  Default: ``7200`` (2h). Fixed across platforms, so it is no longer set in
  the env file — this default is the value.

Ephemeral worker semantics:

- ``task_acks_late = True`` — the broker keeps the task until it's
  acknowledged at *completion*. If the worker / EC2 instance dies
  mid-build, the broker redelivers to another instance.
- ``worker_prefetch_multiplier = 1`` — never reserve a second task while
  one is in flight. With ``--max-tasks-per-child=1`` the worker exits
  after the first task; this prevents an already-prefetched second
  task from being abandoned.
"""

import os

from celery import Celery
from celery.signals import setup_logging

from worker.logs import configure_logging


configure_logging()


@setup_logging.connect
def _setup_logging(**kwargs):
    """
    Keep our own logging config.

    Any receiver on this signal makes Celery skip ``setup_logging_subsystem``,
    which otherwise hijacks the root logger and redirects stdout into the
    ``celery.redirected`` logger at WARNING — the accidental path that used to
    carry our logs to New Relic as unstructured text.
    """
    configure_logging()


app = Celery("readthedocs-builder-worker")

app.conf.broker_url = os.environ["RTD_BROKER_URL"]

# The result backend is *not* configured: we don't need to store results.
app.conf.task_ignore_result = True

# Ephemeral semantics — see module docstring.
app.conf.task_acks_late = True
app.conf.worker_prefetch_multiplier = 1

# Wall-clock ceiling on ANY build, whatever the project asks for.
#
# The per-build limit is ``project.container_time_limit``, enforced by the
# SIGALRM the task arms around the build (``worker.tasks._time_limit``). That
# one is per-build; this is the flat cap on top.
#
# Projects configured ABOVE this are cut off here instead.
# That's the intended policy for now.
#
# Soft, not hard: the soft limit raises SoftTimeLimitExceeded *inside* the task,
# so run_build stops the container, fails the Build, and lets task_postrun fire
# — which is what self-terminates the instance. A hard limit SIGKILLs the child
# instead, so postrun never runs and the EC2 instance leaks. The hard limit is
# kept 20% above as a backstop for a task that ignores the soft one.
TASK_TIME_LIMIT = int(os.environ.get("RTD_BUILDS_TASK_TIME_LIMIT", "7200"))
app.conf.task_soft_time_limit = TASK_TIME_LIMIT
app.conf.task_time_limit = int(TASK_TIME_LIMIT * 1.2)

# Make sure ``worker.tasks`` is registered when the app starts.
app.autodiscover_tasks(["worker"])
