"""
Exceptions raised during a documentation build.

Ported from ``readthedocs.doc_builder.exceptions`` and
``readthedocs.projects.exceptions``. The Django-backed
``NotificationBaseException`` is replaced by a plain ``Exception`` subclass
that carries the same ``message_id`` and ``format_values`` attributes used by
the notification system.

Categories:

- responsibility (user vs application)
- special cases handled differently in the lifecycle (e.g. concurrency limit, cancellation)
- topic groupings (e.g. MkDocs errors)
"""


class NotificationBaseException(Exception):
    """
    Base for exceptions that carry a ``message_id`` for notification lookup.

    The upstream notification system maps ``message_id`` to user-facing
    header/body text stored elsewhere; the runner just needs to attach the id
    to the build via the API.
    """

    default_message = "Undefined error"

    def __init__(self, message_id, format_values=None, exception_message=None, **kwargs):
        self.message_id = message_id
        self.format_values = format_values
        super().__init__(exception_message or self.default_message, **kwargs)


class BuildBaseException(NotificationBaseException):
    default_message = "Build user exception"


class BuildAppError(BuildBaseException):
    default_message = "Build application exception"

    GENERIC_WITH_BUILD_ID = "build:app:generic-with-build-id"
    UPLOAD_FAILED = "build:app:upload-failed"
    BUILDS_DISABLED = "build:app:project-builds-disabled"
    BUILD_DOCKER_UNKNOWN_ERROR = "build:app:docker:unknown-error"
    BUILD_TERMINATED_DUE_INACTIVITY = "build:app:terminated-due-inactivity"


class BuildUserError(BuildBaseException):
    GENERIC = "build:user:generic"

    BUILD_COMMANDS_WITHOUT_OUTPUT = "build:user:output:no-html"
    BUILD_OUTPUT_IS_NOT_A_DIRECTORY = "build:user:output:is-no-a-directory"
    BUILD_OUTPUT_HAS_0_FILES = "build:user:output:has-0-files"
    BUILD_OUTPUT_HAS_NO_PDF_FILES = "build:user:output:has-no-pdf-files"
    BUILD_OUTPUT_HAS_MULTIPLE_FILES = "build:user:output:has-multiple-files"
    BUILD_OUTPUT_HTML_NO_INDEX_FILE = "build:user:output:html-no-index-file"
    BUILD_OUTPUT_OLD_DIRECTORY_USED = "build:user:output:old-directory-used"
    BUILD_ARTIFACTS_ZIP_INVALID = "build:user:artifacts-zip-invalid"
    FILE_TOO_LARGE = "build:user:output:file-too-large"
    TEX_FILE_NOT_FOUND = "build:user:tex-file-not-found"
    PDF_NOT_FOUND = "build:user:pdf-not-found"

    NO_CONFIG_FILE_DEPRECATED = "build:user:config:no-config-file"
    BUILD_IMAGE_CONFIG_KEY_DEPRECATED = "build:user:config:build-image-deprecated"
    BUILD_OS_REQUIRED = "build:user:config:build-os-required"

    BUILD_COMMANDS_IN_BETA = "build:user:build-commands-config-key-in-beta"
    BUILD_TIME_OUT = "build:user:time-out"
    BUILD_EXCESSIVE_MEMORY = "build:user:excessive-memory"
    VCS_DEPRECATED = "build:vcs:deprecated"

    SSH_KEY_WITH_WRITE_ACCESS = "build:user:ssh-key-with-write-access"


class BuildMaxConcurrencyError(BuildUserError):
    LIMIT_REACHED = "build:user:concurrency-limit-reached"


class BuildCancelled(BuildUserError):
    CANCELLED_BY_USER = "build:user:cancelled"
    SKIPPED_EXIT_CODE_183 = "build:user:exit-code-183"


class MkDocsYAMLParseError(BuildUserError):
    GENERIC_WITH_PARSE_EXCEPTION = "build:user:mkdocs:yaml-parse"
    INVALID_DOCS_DIR_CONFIG = "build:user:mkdocs:invalid-dir-config"
    INVALID_DOCS_DIR_PATH = "build:user:mkdocs:invalid-dir-path"
    INVALID_EXTRA_CONFIG = "build:user:mkdocs:invalid-extra-config"
    EMPTY_CONFIG = "build:user:mkdocs:empty-config"
    NOT_FOUND = "build:user:mkdocs:config-not-found"
    CONFIG_NOT_DICT = "build:user:mkdocs:invalid-yaml"
    SYNTAX_ERROR = "build:user:mkdocs:syntax-error"


class UnsupportedSymlinkFileError(BuildUserError):
    SYMLINK_USED = "build:user:symlink:used"


class FileIsNotRegularFile(UnsupportedSymlinkFileError):
    pass


class SymlinkOutsideBasePath(UnsupportedSymlinkFileError):
    pass


# Ported from readthedocs.projects.exceptions.


class ProjectConfigurationError(BuildUserError):
    """Error raised trying to configure a project for build."""

    NOT_FOUND = "project:sphinx:conf-py-not-found"
    MULTIPLE_CONF_FILES = "project:sphinx:multiple-conf-py-files-found"


class UserFileNotFound(BuildUserError):
    FILE_NOT_FOUND = "project:file:not-found"


class RepositoryError(BuildUserError):
    """Failure during repository operation."""

    CLONE_ERROR_WITH_PRIVATE_REPO_ALLOWED = "project:repository:private-clone-error"
    CLONE_ERROR_WITH_PRIVATE_REPO_NOT_ALLOWED = "project:repository:public-clone-error"
    DUPLICATED_RESERVED_VERSIONS = "project:repository:duplicated-reserved-versions"
    FAILED_TO_CHECKOUT = "project:repository:checkout-failed"
    GENERIC = "project:repository:generic-error"
    UNSUPPORTED_VCS = "project:repository:unsupported-vcs"
    FAILED_TO_GET_VERSIONS = "project:repository:failed-to-get-versions"
