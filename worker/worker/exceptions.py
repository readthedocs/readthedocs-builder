"""Exceptions and notification message-ids for the worker."""


class BuildAppError:
    GENERIC_WITH_BUILD_ID = "build:app:generic-with-build-id"


class BuildUserError:
    GENERIC = "build:user:generic"
    BUILD_TIME_OUT = "build:user:time-out"

    NO_CONFIG_FILE_DEPRECATED = "build:user:config:no-config-file"
    BUILD_OS_REQUIRED = "build:user:config:build-os-required"


class RepositoryError:
    DUPLICATED_RESERVED_VERSIONS = "project:repository:duplicated-reserved-versions"


class PreContainerFailure(Exception):
    """
    Raised when the worker can't get to ``docker run`` — bad clone, bad yaml,
    missing build.os, etc.

    ``run_build`` catches this at the top level, PATCHes the Build to
    finished/success=False and POSTs a notification, then returns normally so
    ``task_postrun`` still fires and the instance self-terminates.
    """

    def __init__(
        self,
        message_id: str,
        *,
        format_values: dict | None = None,
        log_message: str | None = None,
    ):
        self.message_id = message_id
        self.format_values = format_values or {}
        super().__init__(log_message or message_id)
