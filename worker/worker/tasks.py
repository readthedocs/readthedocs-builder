"""
``run_build`` — the one task the build-isolated worker accepts.

``trigger_build`` in readthedocs.org sends one of these to the
``build:isolated`` queue with just:

- ``build_pk`` — the Build to run.
- ``build_api_key`` — 24h-scoped token this worker uses to hit the API.
- ``environment`` — where this build's Read the Docs lives: API URL,
  domain, whether private repos exist. Handed to both the worker's API
  client and the runner.
- ``no_self_terminate`` — whether the task_postrun handler should skip
  the AWS terminate call (debug flag, sourced from the
  ``KEEP_BUILD_ISOLATED_INSTANCE`` project feature flag).
- ``build_os_hint`` — ``build.os`` of the version's last successful build,
  or ``None``. The container starts from it (or the latest LTS) so the
  real clone can run right away; the runner switches containers if the
  checked-out config disagrees.

Memory and time limit come from the project via the API.
"""

import contextlib
import os
import signal
import socket
import subprocess

import structlog
from builder.api_client import get_build
from builder.api_client import get_project
from builder.api_client import get_project_ssh_key
from builder.api_client import get_version
from builder.api_client import setup_api
from builder.entrypoint import run_build as run_builder
from builder.exceptions import BuildCancelled
from builder.lsremote import find_duplicate_reserved_versions
from builder.lsremote import parse_lsremote
from builder.refspec import EXTERNAL
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import task_postrun
from celery.signals import task_received

from worker import constants
from worker.celery import app
from worker.config import resolve_build_os
from worker.constants import UPLOADED_BUILD_OS
from worker.docker import get_client
from worker.docker import start_container
from worker.docker import start_healthcheck
from worker.docker import stop_container
from worker.ec2 import self_terminate
from worker.ec2 import set_scale_in_protection
from worker.exceptions import BuildAppError
from worker.exceptions import BuildUserError
from worker.exceptions import PreContainerFailure
from worker.exceptions import RepositoryError
from worker.git import lsremote


log = structlog.get_logger(__name__)


def _start_healthcheck(docker_client, container, environment, build_pk):
    """Start the in-container healthcheck loop, if we were told where to ping."""
    host = environment.get("RTD_HEALTHCHECK_API_HOST")
    if not host:
        log.warning("No healthcheck host; build will not be healthchecked.")
        return

    start_healthcheck(
        docker_client,
        container,
        url=f"{host}/api/v2/build/{build_pk}/healthcheck/?builder={socket.gethostname()}",
        host_header=environment.get("RTD_PRODUCTION_DOMAIN", ""),
        delay=constants.BUILD_HEALTHCHECK_DELAY,
    )


@contextlib.contextmanager
def _time_limit(seconds):
    """
    Bound the build's wall clock, raising ``BUILD_TIME_OUT`` when it runs out.

    ``signal.alarm`` delivers SIGALRM to this process, and the runner installs
    a handler that converts it into an exception — so the build fails through
    the normal path, with a notification attached and the Build finalized,
    rather than being killed.

    If the process is wedged somewhere that never runs Python bytecode, the
    alarm can't fire; Celery's own soft and hard task time limits (see
    ``worker.celery``) are the backstop for that.
    """
    if not seconds:
        yield
        return

    signal.alarm(int(seconds))
    log.info("Build clock armed.", seconds=int(seconds))
    try:
        yield
    finally:
        signal.alarm(0)


@contextlib.contextmanager
def _cancellation_handlers():
    """
    Turn SIGINT/SIGTERM into :class:`BuildCancelled` for the whole task.

    ``cancel_build`` in readthedocs.org revokes with ``terminate=True``, so the
    signal can land at any point — including the bootstrap, before the runner
    installs its own (upload-aware) handlers. Without this it would surface as
    a bare ``KeyboardInterrupt`` and the build would be reported as a plain
    failure, with no cancellation notification.

    The previous handlers are restored on exit. Leaving them installed
    would log a false "Cancellation signal received." on every build when
    Celery recycle SIGTERM after the task due to ``--max-tasks-per-child=1``.
    """

    def _on_cancel(signum, frame):
        log.warning("Cancellation signal received.", signal=signum)
        raise BuildCancelled(BuildCancelled.CANCELLED_BY_USER)

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        signal.signal(sig, _on_cancel)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _to_bool(raw) -> bool:
    """Coerce the strings readthedocs.org sends for boolean flags."""
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() in ("1", "true", "yes", "on")


def _fail_build(api_client, build_pk: int, exc: Exception) -> None:
    """
    Mark a Build as failed via the API + POST a notification.

    Called on any pre-container error. Mirrors the runner's
    ``_attach_failure_notification`` + finalize-PATCH sequence.
    """
    # Pick the message_id, falling back to a generic id per category.
    if isinstance(exc, PreContainerFailure):
        message_id = exc.message_id
        format_values = exc.format_values
    else:
        # Anything unexpected → app-error. User sees a generic message,
        # ops see the traceback in the worker log.
        message_id = BuildAppError.GENERIC_WITH_BUILD_ID
        format_values = {}

    log.error(
        "Failing build at bootstrap.",
        exception_type=type(exc).__name__,
        message_id=message_id,
        format_values=format_values,
    )

    _post_notification(api_client, build_pk, message_id, format_values)
    _finalize_build(api_client, build_pk, state="finished")


def _cancel_build(api_client, build_pk: int) -> None:
    """
    Mark a Build as cancelled via the API + POST a notification.

    Only called when the signal lands outside the runner: once the runner is
    driving the build it catches ``BuildCancelled`` itself and reports it.
    """
    log.warning("Build cancelled.", build_pk=build_pk)
    _post_notification(api_client, build_pk, BuildCancelled.CANCELLED_BY_USER, {})
    _finalize_build(api_client, build_pk, state="cancelled")


def _post_notification(api_client, build_pk: int, message_id: str, format_values: dict) -> None:
    """
    POST a notification to a build.

    Any failure here is logged but doesn't stop the finalize PATCH — the build
    still needs to leave the ``triggered`` state or it stays stuck.
    """
    try:
        api_client.notifications.post(
            {
                "attached_to": f"build/{build_pk}",
                "message_id": message_id,
                "state": "unread",
                "dismissable": False,
                "news": False,
                "format_values": format_values,
            }
        )
    except Exception:
        log.exception("Failed to POST notification for build.", build_pk=build_pk)


def _finalize_build(api_client, build_pk: int, *, state: str) -> None:
    """PATCH a build to a final state so it doesn't stay stuck in ``triggered``."""
    try:
        api_client.build(build_pk).patch(
            {
                "state": state,
                "success": False,
                "length": 0,
            }
        )
    except Exception:
        log.exception("Failed to PATCH build to final state.", build_pk=build_pk, state=state)


@app.task(name=constants.RUN_BUILD_TASK_NAME, bind=True, acks_late=True)
def run_build(
    self, *, build_pk, build_api_key, environment, no_self_terminate=False, build_os_hint=None
):
    """
    Run a single Read the Docs build.

    Steps:

    1. Set up an API client using ``build_api_key``.
    2. Fetch Build → Version → Project via the API.
    3. Resolve ``memory`` + ``time_limit_seconds`` from project fields, falling
       back to ``worker.constants``, and the starting image from
       ``build_os_hint``.
    4. Start the build container, run the build in this process, stop it. The
       runner clones inside the container and switches it if the config asks
       for a different ``build.os``.

    The build runs here, in the Celery task, and reaches into the container
    with ``docker exec`` for every build command — so the container never
    holds our credentials or runs our code.

    Any failure before the runner starts is a "pre-container" failure:
    the runner never got to attach its own notification, so we do it
    here — PATCH the build to finished/success=False and POST a
    notification. Then return normally so ``task_postrun`` still fires
    and the instance self-terminates.
    """
    structlog.contextvars.bind_contextvars(build_pk=build_pk)
    log.info("Received run_build task.", no_self_terminate=no_self_terminate)

    # Keep the ASG from scaling this instance out from under the build.
    # Released in task_postrun, which must happen before self_terminate.
    set_scale_in_protection(True)

    _run_build(
        build_pk=build_pk,
        build_api_key=build_api_key,
        environment=environment,
        build_os_hint=build_os_hint,
    )


def _run_build(*, build_pk, build_api_key, environment, build_os_hint):
    # We need the API client for both the happy path AND the fail path,
    # so build it before entering the try/except.
    api_url = environment["RTD_API_URL"]
    production_domain = environment["RTD_PRODUCTION_DOMAIN"]

    api_client = setup_api(
        api_url=api_url,
        build_api_key=build_api_key,
        production_domain=production_domain,
    )

    # Installed here rather than in the runner: a cancellation can arrive while
    # the bootstrap below is still running.
    with _cancellation_handlers():
        try:
            build, version = _fetch_build(api_client, build_pk)
            build_os, memory, time_limit_seconds = _prepare_build(
                build=build,
                version=version,
                build_os_hint=build_os_hint,
            )
            _sync_versions_for_build(api_client=api_client, build=build, version=version)
        except BuildCancelled:
            _cancel_build(api_client, build_pk)
            return
        except Exception as exc:
            _fail_build(api_client, build_pk, exc)
            return

        structlog.contextvars.bind_contextvars(build_os=build_os)
        log.info(
            "Running build.",
            memory=memory,
            time_limit=time_limit_seconds,
            build_os_hint=build_os_hint,
        )

        # One client for the whole build: the worker starts and stops the
        # container with it, and the runner execs into it with the same one.
        docker_client = get_client()

        def switch_container(new_build_os):
            return _switch_container(
                docker_client,
                build_pk=build_pk,
                build_os=new_build_os,
                memory=memory,
                environment=environment,
            )

        try:
            try:
                container = start_container(
                    docker_client, build_pk=build_pk, build_os=build_os, memory=memory
                )
                _start_healthcheck(docker_client, container, environment, build_pk)
            except BuildCancelled:
                _cancel_build(api_client, build_pk)
                return
            except Exception as exc:
                # The container never came up, so the runner can't report anything.
                _fail_build(api_client, build_pk, exc)
                return

            with _time_limit(time_limit_seconds):
                run_builder(
                    api_client=api_client,
                    docker_client=docker_client,
                    build=build,
                    version=version,
                    container_name=container,
                    build_os=build_os,
                    switch_container=switch_container,
                    production_domain=production_domain,
                    allow_private_repos=_to_bool(environment.get("RTD_ALLOW_PRIVATE_REPOS")),
                    s3_endpoint_url=environment.get("AWS_S3_ENDPOINT_URL") or None,
                )
        except BuildCancelled:
            # Cancelled between the container starting and the runner installing its
            # own handlers; from there on the runner reports its own cancellation.
            _cancel_build(api_client, build_pk)
        except SoftTimeLimitExceeded:
            # The flat ceiling in worker.celery, hit by a project whose
            # container_time_limit is above it. The runner never got to finalize the
            # Build, so do it here. Returning normally keeps task_postrun firing, so
            # the instance still self-terminates.
            log.warning("Task soft time limit exceeded.")
            _fail_build(api_client, build_pk, PreContainerFailure(BuildUserError.BUILD_TIME_OUT))
        finally:
            # The container outlives the runner by design — nothing else reads it,
            # and leaving it behind would strand the instance's memory budget.
            stop_container(docker_client, build_pk)


def _switch_container(docker_client, *, build_pk, build_os, memory, environment):
    """
    Replace the build container with one running ``build_os``.

    Called by the runner after the clone when the checked-out config asks for
    a different image than the one we guessed. The checkout lives on the
    host's docroot bind mount, so nothing is lost. Returns the new name.
    """
    log.info("Switching build container.", build_os=build_os)
    stop_container(docker_client, build_pk)
    container = start_container(docker_client, build_pk=build_pk, build_os=build_os, memory=memory)
    _start_healthcheck(docker_client, container, environment, build_pk)
    structlog.contextvars.bind_contextvars(build_os=build_os)
    return container


def _sync_versions(*, project, repo_url, ssh_key, git_env):
    """
    Reconcile the project's tags/branches into the database.

    Host-side ``git ls-remote`` → validate reserved names (fail the build on a
    duplicate ``latest``/``stable``, like upstream) → dispatch
    ``sync_versions_task`` so readthedocs.org updates the ``Version`` rows.

    Runs before the container starts so a reserved-name conflict fails the
    build early (the post-build server-side tasks can't). Every error *except*
    that conflict is non-fatal — the webhook path also syncs versions — so a
    flaky ``ls-remote`` never blocks a build.
    """
    features = project.get("features") or []
    include_tags = "skip_sync_tags" not in features
    include_branches = "skip_sync_branches" not in features
    if not include_tags and not include_branches:
        return

    try:
        stdout = lsremote(
            repo_url=repo_url,
            ssh_key=ssh_key,
            env=git_env,
            include_tags=include_tags,
            include_branches=include_branches,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.warning("git ls-remote failed; skipping version sync.", error=str(exc))
        return

    branches, tags = parse_lsremote(stdout)
    log.info("Synchronizing versions.", branches=len(branches), tags=len(tags))

    duplicates = find_duplicate_reserved_versions(branches, tags)
    if duplicates:
        raise PreContainerFailure(
            RepositoryError.DUPLICATED_RESERVED_VERSIONS,
            log_message=f"Duplicated reserved versions: {sorted(duplicates)}",
        )

    app.send_task(
        constants.SYNC_VERSIONS_TASK_NAME,
        kwargs={
            "project_pk": project.get("id"),
            "tags_data": [{"identifier": i, "verbose_name": n} for i, n in tags],
            "branches_data": [{"identifier": i, "verbose_name": n} for i, n in branches],
        },
        queue=constants.SYNC_VERSIONS_TASK_QUEUE,
    )


def _fetch_build(api_client, build_pk):
    """
    Fetch Build → Version → Project, the three objects the build runs on.
    """
    # Fetch build → project via the API.
    build = get_build(api_client, build_pk)
    if not build:
        raise PreContainerFailure(
            BuildAppError.GENERIC_WITH_BUILD_ID,
            log_message=f"Build {build_pk} not found via API.",
        )

    # Check for builds that were cancelled in the DB while queued in Redis.
    if build.get("state") == "cancelled":
        log.info("Build already cancelled. Skipping.", build_pk=build_pk)
        raise BuildCancelled(BuildCancelled.CANCELLED_BY_USER)

    version_pk = build.get("version")
    if not version_pk:
        raise PreContainerFailure(
            BuildAppError.GENERIC_WITH_BUILD_ID,
            log_message=f"Build {build_pk} has no version pk.",
        )

    version = get_version(api_client, version_pk)
    if not (version.get("project") or {}).get("id"):
        raise PreContainerFailure(
            BuildAppError.GENERIC_WITH_BUILD_ID,
            log_message=f"Version {version_pk} has no project pk.",
        )

    return build, version


def _prepare_build(*, build, version, build_os_hint=None):
    """
    Resolve the resources the container starts with.

    Returns ``(build_os, memory, time_limit_seconds)``. ``build_os`` is only a
    starting point: the hint from the web side, the latest LTS without one, or
    the fixed image for uploaded builds. The runner corrects it after the clone.
    """
    project = version["project"]

    memory = project.get("container_mem_limit") or constants.BUILD_MEMORY_LIMIT
    time_limit_seconds = project.get("container_time_limit") or constants.BUILD_TIME_LIMIT

    if build.get("is_uploaded"):
        return UPLOADED_BUILD_OS, memory, time_limit_seconds

    return resolve_build_os(build_os_hint), memory, time_limit_seconds


def _sync_versions_for_build(*, api_client, build, version):
    """
    Sync the project's versions, unless this build has nothing to sync.

    Skipped for uploaded builds (no repo) and external versions (a PR doesn't
    change the branch/tag list — and its output is untrusted).
    """
    if build.get("is_uploaded") or version.get("type") == EXTERNAL:
        return

    project = version["project"]
    repo_url = project.get("repo") or ""
    ssh_key = ""
    if repo_url.startswith("git@") or repo_url.startswith("ssh://"):
        ssh_key = get_project_ssh_key(api_client, project["id"])

    git_env = {
        **os.environ,
        "READTHEDOCS_GIT_CLONE_TOKEN": project.get("clone_token") or "",
    }
    _sync_versions(project=project, repo_url=repo_url, ssh_key=ssh_key, git_env=git_env)


SYNC_REPOSITORY_TIME_LIMIT = 120


@app.task(
    name="worker.tasks.sync_repository",
    bind=True,
    acks_late=True,
    soft_time_limit=SYNC_REPOSITORY_TIME_LIMIT,
    time_limit=int(SYNC_REPOSITORY_TIME_LIMIT * 1.2),
)
def sync_repository(self, *, project_pk, build_api_key, environment):
    """
    Reconcile a project's tags/branches into the database, without a build.

    Unlike ``run_build`` this must NOT self-terminate the instance -- one
    ``ls-remote`` is not worth an EC2 lifecycle.
    """
    structlog.contextvars.bind_contextvars(project_pk=project_pk)
    log.info("Received sync_repository task.")

    api_client = setup_api(
        api_url=environment["RTD_API_URL"],
        build_api_key=build_api_key,
        production_domain=environment["RTD_PRODUCTION_DOMAIN"],
    )

    project = get_project(api_client, project_pk)
    structlog.contextvars.bind_contextvars(project_slug=project.get("slug"))

    repo_url = project.get("repo") or ""
    ssh_key = ""
    if repo_url.startswith("git@") or repo_url.startswith("ssh://"):
        ssh_key = get_project_ssh_key(api_client, project_pk)

    git_env = {
        **os.environ,
        "READTHEDOCS_GIT_CLONE_TOKEN": project.get("clone_token") or "",
    }

    try:
        _sync_versions(project=project, repo_url=repo_url, ssh_key=ssh_key, git_env=git_env)
    except PreContainerFailure as exc:
        # There is no Build to attach a notification to, so the task failing is
        # the only signal we get. Duplicated reserved versions land here.
        log.warning("Version sync failed.", error=str(exc))
        raise


@task_received.connect
def _on_run_build_received(sender, request=None, **_):
    """
    Stop consuming the queue as soon as the one build this instance runs arrives.

    ``--max-tasks-per-child=1`` only recycles the pool child; the main process
    keeps consuming. Once the build finishes and is acked (``acks_late``), the
    freed prefetch slot lets it grab a second build while the instance is
    already terminating, and that build dies with it.

    ``task_received`` fires in the main process with the Consumer as
    ``sender``, and at that point the only prefetch slot is held by this
    message, so cancelling here guarantees nothing else is fetched.
    """
    if request is None or request.name != constants.RUN_BUILD_TASK_NAME:
        return

    # Dev: no instance to terminate, so keep consuming.
    if os.environ.get("RTD_DOCKER_COMPOSE"):
        return

    log.info("Cancelling queue consumer; this instance runs one build only.")
    sender.cancel_task_queue(constants.RUN_BUILD_TASK_QUEUE)


@task_postrun.connect
def _on_run_build_postrun(sender, kwargs=None, **_):
    """
    Self-terminate the EC2 instance after a build task finishes.

    Connected to Celery's ``task_postrun`` signal, which fires after
    a task's body returns — for success, failure, soft-time-limit
    expiry, and signal-revoked cancellation alike. The signal receives
    the task's *actual* kwargs, so we read ``no_self_terminate``
    directly off the call we're handling. No module-level state.

    Filtered to ``run_build`` since this is the only task this worker
    is meant to consume; if some other task somehow ended up routed
    here, we don't want to terminate the host as a side effect.
    """
    if sender is None or sender.name != constants.RUN_BUILD_TASK_NAME:
        return

    # Always released, even when we skip the terminate below: a protected
    # instance can't be terminated by the ASG *or* by self_terminate, so
    # leaving it set would strand it until someone clears it by hand.
    set_scale_in_protection(False)

    if (kwargs or {}).get("no_self_terminate"):
        log.warning(
            "Skipping self-terminate: KEEP_BUILD_ISOLATED_INSTANCE "
            "feature flag set on the project. Instance will remain in "
            "the ASG until manually terminated."
        )
        return

    self_terminate()
