"""Worker-side constants: per-build resource defaults."""

# Applied when ``project.container_mem_limit`` is unset.
BUILD_MEMORY_LIMIT = "7g"

# Seconds. Applied when ``project.container_time_limit`` is unset.
BUILD_TIME_LIMIT = 900

# Image for uploaded builds.
UPLOADED_BUILD_OS = "ubuntu-26.04"

# Seconds between healthcheck pings from the runner.
BUILD_HEALTHCHECK_DELAY = 15

# The sparse clone fetches one file with ``--filter=blob:none``; seconds in
# practice. Bounded so a hung clone can't wedge the task forever.
GIT_CLONE_TIMEOUT_SECONDS = 300

# Literal names for ``git sparse-checkout``. Only used when the project has no
# ``readthedocs_yaml_path`` set.
CONFIG_FILENAMES = (
    ".readthedocs.yaml",
    ".readthedocs.yml",
    "readthedocs.yaml",
    "readthedocs.yml",
)

# readthedocs.org Celery task that reconciles ``Version`` rows from the
# tags/branches the worker ls-remotes.
SYNC_VERSIONS_TASK_NAME = "readthedocs.builds.tasks.sync_versions_task"
SYNC_VERSIONS_TASK_QUEUE = "web"
