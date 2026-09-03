"""
End-to-end build runner.

Wraps :class:`builder.director.BuildDirector` with the lifecycle work that
upstream's Celery ``update_docs_task`` does *around* the director:

- Build state transitions (``triggered`` → ``cloning`` → ``installing`` →
  ``building`` → ``uploading`` → ``finished``).
- Post-build artifact validation + upload to the artifacts bucket.
- Final PATCH with success status and length.

The director itself stays focused on doing-the-build; the runner owns
"is the build object in the right state for the world to see it."
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import structlog
from slumber.exceptions import HttpClientError

from builder import settings
from builder.constants import ARTIFACT_TYPES
from builder.constants import ARTIFACT_TYPES_WITHOUT_MULTIPLE_FILES_SUPPORT
from builder.constants import BRANCH
from builder.constants import BUILD_FINAL_STATES
from builder.constants import BUILD_STATE_BUILDING
from builder.constants import BUILD_STATE_CANCELLED
from builder.constants import BUILD_STATE_CLONING
from builder.constants import BUILD_STATE_FINISHED
from builder.constants import BUILD_STATE_INSTALLING
from builder.constants import BUILD_STATE_UPLOADING
from builder.constants import UNDELETABLE_ARTIFACT_TYPES
from builder.director import BuildDirector
from builder.exceptions import BuildAppError
from builder.exceptions import BuildCancelled
from builder.exceptions import BuildUserError
from builder.filesystem import assert_path_is_inside_docroot


log = structlog.get_logger(__name__)


class Runner:
    """
    Orchestrate the full build lifecycle around :class:`BuildDirector`.

    :param data: shared :class:`builder.director.TaskData` carrying the API
        client and the project/version/build.
    """

    def __init__(self, data):
        self.data = data
        self.director = BuildDirector(data=data)
        # The director references back via TaskData for self.vcs_repository, etc.
        self.data.build_director = self.director
        self._start_time = None
        self._success = False
        self._state = None
        # While True, a cancellation signal is deferred so an in-flight
        # ``rclone`` artifact sync isn't interrupted mid-upload.
        self._in_upload = False

    def run(self):
        """Drive the full build. Returns the success boolean."""
        self._install_timeout_handler()
        self._install_cancellation_handler()
        self._start_time = time.monotonic()
        try:
            self._check_project_disabled()
            self._reset_build()
            self._claim_build()
            self._set_build_state(BUILD_STATE_CLONING)

            if self.data.build.get("is_uploaded"):
                self.director.create_build_environment()
                self.director.download_artifacts_from_storage()

                self._set_build_state(BUILD_STATE_BUILDING)
                self.director.extract_artifacts()
            else:
                self.director.create_vcs_environment()
                self.director.setup_vcs()
                self._post_checkout()

                self._set_build_state(BUILD_STATE_INSTALLING)
                self.director.create_build_environment()

                # ``build.commands`` (generic builds) bypass the language
                # environment and doctype builders entirely: install the build
                # tools, then run the user's commands verbatim. Mirrors
                # readthedocs.org's dispatch in ``projects/tasks/builds.py``.
                if getattr(self.data.config.build, "commands", None):
                    self.director.install_build_tools()

                    self._set_build_state(BUILD_STATE_BUILDING)
                    self.director.run_build_commands()
                else:
                    self.director.setup_environment()

                    self._set_build_state(BUILD_STATE_BUILDING)
                    self.director.build()

            # Fail projects still writing to the legacy ``_build/html`` path
            # (their doctool "succeeds" but produces no ``_readthedocs`` output).
            # Upstream runs this after the build in ``execute()``.
            self.director.check_old_output_directory()

            self._set_build_state(BUILD_STATE_UPLOADING)
            valid_artifacts = self._validate_artifacts()
            # Defer cancellation across the storage sync + version update so a
            # signal can't interrupt ``rclone`` and leave partial artifacts.
            self._in_upload = True
            try:
                self._upload_artifacts(valid_artifacts)
                self._update_version(valid_artifacts)
            finally:
                self._in_upload = False

            self._success = True
        except (BuildAppError, BuildCancelled, BuildUserError) as exc:
            # Surface the structured details — message_id and format_values —
            # so production logs (and dev's ``docker logs build-<pk>``) carry
            # the actionable info. The default Python traceback only shows
            # the class message ("Build user exception"), which isn't useful;
            # the user-facing text comes from the notification system, keyed
            # by ``message_id``.
            log.error(
                "Build failed.",
                exception_type=type(exc).__name__,
                message_id=getattr(exc, "message_id", None),
                format_values=getattr(exc, "format_values", None) or {},
            )
            # A cancelled build finishes as ``cancelled``, not ``finished`` —
            # the user stopped it (or a command exited 183), it didn't fail.
            if isinstance(exc, BuildCancelled):
                self._state = BUILD_STATE_CANCELLED
            self._attach_failure_notification(exc)
            # ``BuildAppError`` represents *our* bugs (AWS failures, dispatch
            # errors, etc.). Re-raise so the traceback ends up in New Relic /
            # ``docker logs`` for debugging. ``BuildCancelled`` and
            # ``BuildUserError`` are expected failure modes — the structured
            # log above + the attached notification already carry everything
            # actionable, so the traceback would just be noise.
            if isinstance(exc, BuildAppError):
                raise
        finally:
            # Always finalize, even on failure: the build needs to leave
            # ``cloning`` / ``installing`` etc. so the API doesn't show it
            # stuck in flight.
            self._finalize()
        return self._success

    # ---- Wall-clock timeout (cooperates with worker.tasks._time_limit) ----

    def _install_timeout_handler(self):
        """
        Convert SIGALRM into a :class:`BuildUserError(BUILD_TIME_OUT)`.

        The worker waits out the wall-clock budget and then sends SIGALRM
        to this process. We translate it into a controlled exception so
        the lifecycle's ``try/except/finally`` runs (notification +
        finalize PATCH) before exiting, rather than the process being
        killed abruptly mid-step.

        If Python is wedged somewhere that never runs bytecode the alarm
        can't fire at all; Celery's own soft and hard task time limits (see
        ``worker.celery``) are the backstop for that. Timing is **entirely**
        owned by the worker; this handler just receives the signal.
        """

        def _on_timeout(signum, frame):
            log.warning("SIGALRM received — build time limit reached.")
            raise BuildUserError(BuildUserError.BUILD_TIME_OUT)

        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, _on_timeout)

    # ---- User cancellation ----

    def _install_cancellation_handler(self):
        """
        Convert SIGINT/SIGTERM into a :class:`BuildCancelled` exception.

        ``cancel_build`` in readthedocs.org revokes the worker's Celery task
        with ``signal="SIGINT", terminate=True``, and the build runs in that
        very process — so the signal lands here. Translating it into an
        exception is what lets the lifecycle's ``try/except/finally`` run
        (cancellation notification + finalize PATCH with success=False)
        instead of leaving the build stuck in ``cloning`` / ``installing`` /
        etc. forever. Python's default would raise ``KeyboardInterrupt``,
        finalizing the Build as a plain failure with no notification.

        The worker installs equivalent handlers around the whole task
        (``worker.tasks._install_cancellation_handlers``) so a cancellation
        during the bootstrap is caught too. These override them for the
        duration of the build to add the ``_in_upload`` deferral, which is the
        only difference between the two.
        """

        signal.signal(signal.SIGTERM, self._on_cancel)
        signal.signal(signal.SIGINT, self._on_cancel)

    def _on_cancel(self, signum, frame):
        """Cancel the build, but defer while uploading."""
        if self._in_upload:
            # Upstream refuses to cancel a build in the ``uploading`` state;
            # interrupting ``rclone`` mid-sync would leave partially published
            # docs in storage. Let the upload finish.
            log.warning(
                "Cancellation signal received during upload; deferring.",
                signal=signum,
            )
            return
        log.warning("Cancellation signal received.", signal=signum)
        raise BuildCancelled(BuildCancelled.CANCELLED_BY_USER)

    # ---- API state transitions ----

    def _check_project_disabled(self):
        """
        Refuse to build a project whose builds are disabled.

        ``skip`` comes from the API and is broader than the model field: it's
        ``not Project.objects.is_active(project)``, so it also covers spam and
        deleted/inactive owners.
        """
        if self.data.project.skip:
            log.warning("Project build skipped.")
            raise BuildAppError(BuildAppError.BUILDS_DISABLED)

    def _reset_build(self):
        """
        Clear any prior state, commands, and notifications on the build.

        Mirrors upstream's ``UpdateDocsTask._reset_build``: a re-run of the
        same build pk would otherwise stack new commands/notifications on
        top of the previous attempt. The reset call is idempotent.
        """
        log.info("Resetting build.")
        self.data.api_client.build(self.data.build["id"]).reset.post()

    def _claim_build(self):
        """
        Claim the build for this instance by PATCHing ``Build.builder``.

        Must run *after* ``_reset_build``: ``Build.reset()`` upstream sets
        ``builder = ""``, so claiming any earlier gets wiped.
        """
        if not settings.RTD_BUILDER_HOSTNAME:
            log.warning("No builder hostname set; build will not be healthchecked.")
            return

        log.info("Claiming build.", builder=settings.RTD_BUILDER_HOSTNAME)
        self.data.api_client.build(self.data.build["id"]).patch(
            {"builder": settings.RTD_BUILDER_HOSTNAME}
        )

    def _set_build_state(self, state):
        """PATCH the build's state."""
        log.info("Build state.", state=state)
        self.data.api_client.build(self.data.build["id"]).patch({"state": state})

    def _post_checkout(self):
        """
        PATCH the build fields resolved during checkout.

        ``setup_vcs`` records the commit, the parsed ``config`` (``as_dict``),
        and the ``readthedocs_yaml_path`` on ``self.data.build`` after the clone
        + config load.
        """
        payload = {
            field: self.data.build[field]
            for field in ("commit", "config", "readthedocs_yaml_path")
            if self.data.build.get(field) is not None
        }
        if payload:
            self.data.api_client.build(self.data.build["id"]).patch(payload)

    def _update_version(self, valid_artifacts):
        """Mark the Version built and record which formats it produced."""
        # Upstream only updates the Version when HTML was produced.
        if "html" not in valid_artifacts:
            return

        data = {
            "built": True,
            "documentation_type": self.data.version.documentation_type,
            "has_pdf": "pdf" in valid_artifacts,
            "has_epub": "epub" in valid_artifacts,
            "has_htmlzip": "htmlzip" in valid_artifacts,
        }
        # ``readthedocs-build.yaml`` data, when the doctool emitted it and
        # ``store_readthedocs_build_yaml`` parsed it. Only sent when populated so
        # we never overwrite stored data with ``None``. It feeds addons via the
        # ``/_/readthedocs-config.json`` endpoint. ``addons`` stays omitted —
        # upstream only echoes it back, never modifies it during a build.
        if self.data.version.build_data is not None:
            data["build_data"] = self.data.version.build_data
        # Point ``latest`` at the VCS default branch when the project has no
        # explicit one; the director resolves it during the clone.
        if self.data.default_branch:
            data["identifier"] = self.data.default_branch
            data["type"] = BRANCH

        log.info("Updating version.", **data)
        try:
            self.data.api_client.version(self.data.version.pk).patch(data)
        except HttpClientError:
            # Same call as upstream: the artifacts are already in storage, so a
            # failure here leaves the Version inconsistent but the build usable.
            log.exception(
                "Updating version db object failed. "
                "Files are synced in the storage, but 'Version' object is not updated."
            )

    def _finalize(self):
        """Final PATCH with state + success + length."""
        state = self._state if self._state in BUILD_FINAL_STATES else BUILD_STATE_FINISHED
        length = int(time.monotonic() - self._start_time) if self._start_time else 0
        log.info("Build finished.", state=state, success=self._success, length=length)
        self.data.api_client.build(self.data.build["id"]).patch(
            {
                "state": state,
                "success": self._success,
                "length": length,
            }
        )

    def _attach_failure_notification(self, exc):
        """
        POST a notification to the failed build via the API.

        The ``message_id`` carried on the exception is the canonical
        identifier the API/UI uses to render a user-facing message.
        ``format_values`` provides interpolation fields (e.g. the missing
        config-file path).
        """
        # Pick the message_id, falling back to a generic id per exception
        # category (mirrors upstream's ``on_failure`` mapping).
        message_id = getattr(exc, "message_id", None)
        if not message_id:
            if isinstance(exc, BuildAppError):
                message_id = BuildAppError.GENERIC_WITH_BUILD_ID
            else:
                message_id = BuildUserError.GENERIC

        format_values = getattr(exc, "format_values", None) or {}

        attached_to = f"build/{self.data.build['id']}"
        try:
            self.director.attach_notification(
                attached_to=attached_to,
                message_id=message_id,
                format_values=format_values,
            )
            log.info(
                "Notification attached.",
                attached_to=attached_to,
                message_id=message_id,
            )
        except Exception:
            log.exception("Failed to attach notification to the build.")

    # ---- Artifact validation ----

    def _validate_artifacts(self):
        """
        Walk every output format directory under ``_readthedocs/``.

        Mirrors upstream's :meth:`UpdateDocsTask.get_valid_artifact_types`:

        - HTML must contain ``index.html``.
        - PDF / EPUB / HTMLZip directories must contain *exactly one* file;
          that file is renamed to ``<project_slug>.<ext>`` (the name Proxito
          serves under download URLs).
        - Missing directories are silently skipped (the format wasn't built).

        Returns the list of valid artifact types.
        """
        valid_artifacts = []
        for artifact_type in ARTIFACT_TYPES:
            artifact_directory = self.data.project.artifact_path(
                version=self.data.version.slug,
                type_=artifact_type,
            )

            if artifact_type == "html":
                index_html = os.path.join(artifact_directory, "index.html")
                if not os.path.exists(index_html):
                    log.info(
                        "Failing the build. HTML output does not contain an 'index.html'.",
                        index_html=index_html,
                    )
                    raise BuildUserError(BuildUserError.BUILD_OUTPUT_HTML_NO_INDEX_FILE)

            if not os.path.exists(artifact_directory):
                continue

            if not os.path.isdir(artifact_directory):
                log.debug("Output path is not a directory.", output_format=artifact_type)
                raise BuildUserError(
                    BuildUserError.BUILD_OUTPUT_IS_NOT_A_DIRECTORY,
                    format_values={"artifact_type": artifact_type},
                )

            if artifact_type in ARTIFACT_TYPES_WITHOUT_MULTIPLE_FILES_SUPPORT:
                files = os.listdir(artifact_directory)
                if len(files) > 1:
                    log.debug(
                        "Multiple files in single-file format directory.",
                        output_format=artifact_type,
                    )
                    raise BuildUserError(
                        BuildUserError.BUILD_OUTPUT_HAS_MULTIPLE_FILES,
                        format_values={"artifact_type": artifact_type},
                    )
                if not files:
                    raise BuildUserError(
                        BuildUserError.BUILD_OUTPUT_HAS_0_FILES,
                        format_values={"artifact_type": artifact_type},
                    )

                # Rename the single file to ``<project_slug>.<ext>`` so
                # Proxito can serve it from a stable URL.
                filename = files[0]
                _, extension = filename.rsplit(".", 1)
                source = Path(artifact_directory) / filename
                destination = Path(artifact_directory) / f"{self.data.project.slug}.{extension}"
                assert_path_is_inside_docroot(source)
                assert_path_is_inside_docroot(destination)
                if source != destination:
                    shutil.move(source, destination)

            valid_artifacts.append(artifact_type)

        return valid_artifacts

    # ---- Artifact upload ----

    def _upload_artifacts(self, valid_artifacts):
        """
        Upload each valid artifact type, delete formats no longer produced.

        - Each ``valid_artifact`` directory is uploaded to
          ``<media_type>/<project_slug>/<version_slug>/`` in the artifacts
          bucket.
        - Artifact types not produced this build are deleted from storage,
          *except* HTML and JSON (search index) — those are
          ``UNDELETABLE_ARTIFACT_TYPES`` upstream and shouldn't disappear if
          a build doesn't re-emit them (e.g. partial rebuilds).
        """
        from builder.storage import StorageType
        from builder.storage import get_storage

        build_media_storage = get_storage(
            build_id=self.data.build["id"],
            api_client=self.data.api_client,
            storage_type=StorageType.build_media,
            endpoint_url=self.data.s3_endpoint_url,
        )

        types_to_copy = []
        types_to_delete = []
        for artifact_type in ARTIFACT_TYPES:
            if artifact_type in valid_artifacts:
                types_to_copy.append(artifact_type)
            elif artifact_type not in UNDELETABLE_ARTIFACT_TYPES:
                types_to_delete.append(artifact_type)

        for media_type in types_to_copy:
            from_path = self.data.project.artifact_path(
                version=self.data.version.slug,
                type_=media_type,
            )
            to_path = self.data.version.get_storage_path(media_type=media_type)
            log.info(
                "Uploading artifact.", media_type=media_type, from_path=from_path, to_path=to_path
            )
            self._log_directory_size(from_path, media_type)
            try:
                build_media_storage.rclone_sync_directory(from_path, to_path)
            except BuildCancelled, BuildAppError, BuildUserError:
                raise
            except Exception as exc:
                log.exception(
                    "Error copying to storage.",
                    media_type=media_type,
                    from_path=from_path,
                    to_path=to_path,
                )
                raise BuildAppError(
                    BuildAppError.UPLOAD_FAILED,
                    exception_message="Error uploading files to the storage.",
                ) from exc

        for media_type in types_to_delete:
            media_path = self.data.version.get_storage_path(media_type=media_type)
            try:
                build_media_storage.delete_directory(media_path)
            except Exception:
                # Non-fatal: a missing prefix is fine, real errors will be
                # noisy in the next upload attempt.
                log.exception(
                    "Error deleting from storage.",
                    media_type=media_type,
                    media_path=media_path,
                )

    def _log_directory_size(self, directory, media_type):
        """
        Log the size of an artifact directory before uploading it.
        """
        try:
            output = subprocess.check_output(["du", "--summarize", "-m", "--", directory])
            # The output is something like: "5\t/path/to/directory".
            directory_size = int(output.decode().split()[0])
            log.info(
                "Build artifacts directory size.",
                directory=directory,
                size=directory_size,  # Size in mega bytes
                media_type=media_type,
            )
        except Exception:
            log.info(
                "Error getting build artifacts directory size.",
                exc_info=True,
            )
