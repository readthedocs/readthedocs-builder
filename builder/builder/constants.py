"""
Constants used by the build runner.

Cherry-picked from ``readthedocs.builds.constants``,
``readthedocs.projects.constants`` and ``readthedocs.projects.notifications``,
with all Django-translated UI label tuples dropped.
"""

import os
import re


# Build state machine. Internal representation reported back to the API.
BUILD_STATE_TRIGGERED = "triggered"
BUILD_STATE_CLONING = "cloning"
BUILD_STATE_INSTALLING = "installing"
BUILD_STATE_BUILDING = "building"
BUILD_STATE_UPLOADING = "uploading"
BUILD_STATE_FINISHED = "finished"
BUILD_STATE_CANCELLED = "cancelled"

BUILD_FINAL_STATES = (
    BUILD_STATE_FINISHED,
    BUILD_STATE_CANCELLED,
)

# External-provider build status (sent back to GitHub/GitLab/etc).
BUILD_STATUS_FAILURE = "failed"
BUILD_STATUS_PENDING = "pending"
BUILD_STATUS_SUCCESS = "success"

# Version manager names.
INTERNAL = "internal"
EXTERNAL = "external"

# Version types.
BRANCH = "branch"
TAG = "tag"
UNKNOWN = "unknown"

# Magic version slugs. Upstream pulls these from ``settings.RTD_LATEST`` /
# ``settings.RTD_STABLE``; we hardcode the defaults since they are stable.
LATEST = "latest"
STABLE = "stable"

# Doctype identifiers used by the config file and builder loader.
SPHINX = "sphinx"
SPHINX_HTMLDIR = "sphinx_htmldir"
SPHINX_SINGLEHTML = "sphinx_singlehtml"
MKDOCS = "mkdocs"
MKDOCS_HTML = "mkdocs_html"
GENERIC = "generic"

# Output paths the runner expects after a build, relative to the repo checkout.
BUILD_COMMANDS_OUTPUT_PATH = "_readthedocs/"
BUILD_COMMANDS_OUTPUT_PATH_HTML = os.path.join(BUILD_COMMANDS_OUTPUT_PATH, "html")

# Maximum size of a single recorded build command's output, in bytes.
# Keeps recorded output under Azure Blob Storage's upload limit upstream.
MAX_BUILD_COMMAND_SIZE = 1_000_000

# Magic exit code a user command can use to abort the build "successfully"
# (the build reports as cancelled, but the external Git status is success).
# Mirrors ``readthedocs.doc_builder.constants.RTD_SKIP_BUILD_EXIT_CODE``.
RTD_SKIP_BUILD_EXIT_CODE = 183

# Upper bound for a single command's recorded output payload sent to the API.
# Upstream pulls the headroom from ``settings.DATA_UPLOAD_MAX_MEMORY_SIZE``
# (Django default 2.5 MB) minus 512 KB of request overhead. We hardcode the
# resulting threshold.
DATA_UPLOAD_MAX_OUTPUT_BYTES = 2_621_440 - 512 * 1024  # ~2 MB

# Artifact types the runner produces and uploads to S3.
ARTIFACT_TYPES = (
    "html",
    "json",
    "htmlzip",
    "pdf",
    "epub",
)
# Artifact types not deleted from storage even if not re-built this run.
UNDELETABLE_ARTIFACT_TYPES = (
    "html",
    "json",
)
# Artifact types that expect exactly one file in the output directory.
ARTIFACT_TYPES_WITHOUT_MULTIPLE_FILES_SUPPORT = (
    "htmlzip",
    "epub",
    "pdf",
)

# Notification message id used when an SSH key with write access is detected.
# Ported from readthedocs.projects.notifications.
MESSAGE_PROJECT_SSH_KEY_WITH_WRITE_ACCESS = "project:ssh-key-with-write-access"

# Refspec patterns to fetch a pull/merge request HEAD from the provider as a
# local ``external-<id>`` branch. Defined in ``builder.refspec`` so the
# worker's bootstrap clone and the build agree on the refspec; re-exported
# here for the existing imports.
from builder.refspec import GITHUB_PR_PULL_PATTERN  # noqa: E402,F401
from builder.refspec import GITLAB_MR_PULL_PATTERN  # noqa: E402,F401


# Sentinel used by config-file ``submodules.include`` / ``submodules.exclude``
# to mean "everything". Matches upstream's ``readthedocs.config.ALL``.
ALL = "all"


class Feature:
    """
    Feature-flag identifiers for ``APIProject.has_feature(name)``.

    Mirrors upstream's ``readthedocs.projects.models.Feature`` class
    constants; we only port the identifiers actually checked by the build
    pipeline. When a new feature flag is referenced during a port, add the
    corresponding string here.
    """

    PIP_ALWAYS_UPGRADE = "pip_always_upgrade"
    BUILD_IN_PARALLEL = "build_in_parallel"


# LaTeX output detector. Used by the PDF builder to override LaTeX's exit code
# when an output file was written despite a non-zero exit.
PDF_RE = re.compile("Output written on (.*?)")


# Old-style locale codes (e.g. ``zh_CN``) that historically appeared in
# project URLs. Sphinx still wants them in their original ``xx_YY`` form,
# so we map our normalized ``xx-yy`` keys back to the originals.
_OLD_LANGUAGE_CODES = [
    "nb_NO",
    "pt_BR",
    "es_MX",
    "uk_UA",
    "zh_CN",
    "zh_TW",
]
OLD_LANGUAGES_CODE_MAPPING = {code.lower().replace("_", "-"): code for code in _OLD_LANGUAGE_CODES}
