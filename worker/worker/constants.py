"""Worker-side constants: per-build resource defaults."""

# Applied when ``project.container_mem_limit`` is unset.
BUILD_MEMORY_LIMIT = "7g"

# Seconds. Applied when ``project.container_time_limit`` is unset.
BUILD_TIME_LIMIT = 900

# Image for uploaded builds.
UPLOADED_BUILD_OS = "ubuntu-26.04"

# Seconds between healthcheck pings from the runner.
BUILD_HEALTHCHECK_DELAY = 15

# Bounds the host-side ``git ls-remote`` so a hung remote can't wedge the task.
GIT_CLONE_TIMEOUT_SECONDS = 300

# ``build.os`` of the container the repository is cloned in.
CLONE_BUILD_OS = "ubuntu-lts-latest"

# readthedocs.org Celery task that reconciles ``Version`` rows from the
# tags/branches the worker ls-remotes.
SYNC_VERSIONS_TASK_NAME = "readthedocs.builds.tasks.sync_versions_task"
SYNC_VERSIONS_TASK_QUEUE = "web"

# The only queue this worker consumes; must match ``-Q`` in the systemd unit.
# NOTE: this should eventually be read from an environment variable.
RUN_BUILD_TASK_QUEUE = "build:isolated"

# The build task this worker runs; must match ``RTD_BUILD_ISOLATED_TASK_NAME``
# in readthedocs.org settings.
RUN_BUILD_TASK_NAME = "worker.tasks.run_build"
