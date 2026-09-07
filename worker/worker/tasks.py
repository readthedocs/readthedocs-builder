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

Everything else — memory, time limit, docker image tag, command — is
resolved here by fetching build/project data from the API and
sparse-cloning ``.readthedocs.yaml``.
"""

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import tempfile

import requests
import structlog
from builder.api_client import get_build
from builder.api_client import get_project_ssh_key
from builder.api_client import get_version
from builder.api_client import setup_api
from builder.entrypoint import run_build as run_builder
from builder.exceptions import BuildCancelled
from builder.lsremote import find_duplicate_reserved_versions
from builder.lsremote import parse_lsremote
from builder.refspec import get_remote_fetch_refspec
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import task_postrun

from worker import constants
from worker.celery import app
from worker.config import read_build_os
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
from worker.git import sparse_clone_yaml


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


def _install_cancellation_handlers():
    """
    Turn SIGINT/SIGTERM into :class:`BuildCancelled` for the whole task.

    ``cancel_build`` in readthedocs.org revokes with ``terminate=True``, so the
    signal can land at any point — including the bootstrap, before the runner
    installs its own (upload-aware) handlers. Without this it would surface as
    a bare ``KeyboardInterrupt`` and the build would be reported as a plain
    failure, with no cancellation notification.
    """

    def _on_cancel(signum, frame):
        log.warning("Cancellation signal received.", signal=signum)
        raise BuildCancelled(BuildCancelled.CANCELLED_BY_USER)

    signal.signal(signal.SIGINT, _on_cancel)
    signal.signal(signal.SIGTERM, _on_cancel)


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


@app.task(name="worker.tasks.run_build", bind=True, acks_late=True)
def run_build(self, *, build_pk, build_api_key, environment, no_self_terminate=False):
    """
    Run a single Read the Docs build.

    Steps:

    1. Set up an API client using ``build_api_key``.
    2. Fetch Build → Version → Project via the API.
    3. Sparse-clone ``.readthedocs.yaml``.
    4. Parse ``build.os`` and pick the ``readthedocs/build:<os>`` image.
    5. Resolve ``memory`` + ``time_limit_seconds`` from project
       fields, falling back to ``worker.constants``.
    6. Start the build container, run the build in this process, stop it.

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
    _install_cancellation_handlers()

    # Defined when we get the value from the API and revoked in ``finally``.
    clone_token = None

    try:
        try:
            build, version = _fetch_build(api_client, build_pk)
            clone_token = version["project"].get("clone_token")
            build_os, memory, time_limit_seconds = _prepare_build(
                api_client=api_client,
                build=build,
                version=version,
            )
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
        )

        # One client for the whole build: the worker starts and stops the
        # container with it, and the runner execs into it with the same one.
        docker_client = get_client()

        try:
            try:
                container = start_container(
                    docker_client, build_pk=build_pk, build_os=build_os, memory=memory
                )
            except BuildCancelled:
                _cancel_build(api_client, build_pk)
                return
            except Exception as exc:
                # The container never came up, so the runner can't report anything.
                _fail_build(api_client, build_pk, exc)
                return

            _start_healthcheck(docker_client, container, environment, build_pk)

            with _time_limit(time_limit_seconds):
                run_builder(
                    api_client=api_client,
                    docker_client=docker_client,
                    build=build,
                    version=version,
                    container_name=container,
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
    finally:
        # Always revoke the GitHub App token no matter what happened.
        _revoke_clone_token(clone_token)


def _sync_versions(*, project, repo_url, ssh_key, git_env):
    """
    Reconcile the project's tags/branches into the database.

    Host-side ``git ls-remote`` → validate reserved names (fail the build on a
    duplicate ``latest``/``stable``, like upstream) → dispatch
    ``sync_versions_task`` so readthedocs.org updates the ``Version`` rows.

    Runs before the container so a reserved-name conflict fails the build early
    (the post-build server-side tasks can't). Every error *except* that conflict
    is non-fatal — the webhook path also syncs versions — so a flaky
    ``ls-remote`` never blocks a build.
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


def _prepare_build(*, api_client, build, version):
    """
    Everything between fetching the build and starting its container.

    Returns ``(build_os, memory, time_limit_seconds)``, or raises
    ``PreContainerFailure``.
    """
    project = version["project"]
    project_pk = project["id"]

    memory = project.get("container_mem_limit") or constants.BUILD_MEMORY_LIMIT
    time_limit_seconds = project.get("container_time_limit") or constants.BUILD_TIME_LIMIT

    if build.get("is_uploaded"):
        # Nothing to clone: the artifacts came in through the upload API, so
        # there is no repository and no ``.readthedocs.yaml`` to read
        # ``build.os`` from.
        log.info("Build is uploaded; skipping sparse-clone.")
        return UPLOADED_BUILD_OS, memory, time_limit_seconds

    # Sparse-clone `.readthedocs.yaml`.
    repo_url = project.get("repo") or ""
    ssh_key = ""
    if repo_url.startswith("git@") or repo_url.startswith("ssh://"):
        ssh_key = get_project_ssh_key(api_client, project_pk)

    # Fetch the same ref the runner will build. For branches the identifier is
    # the branch name (``git clone -b`` would work), but for tags and external
    # (PR/MR) versions it's a commit hash / pull refspec, so we resolve it here.
    # Falls back to the remote's default branch (``HEAD``) when undecidable.
    refspec = (
        get_remote_fetch_refspec(
            version_type=version.get("type", "branch"),
            verbose_name=version.get("verbose_name", ""),
            identifier=version.get("identifier", ""),
            machine=version.get("machine", False),
            slug=version.get("slug", ""),
            is_github="github.com" in repo_url,
            is_gitlab="gitlab.com" in repo_url,
        )
        or "HEAD"
    )

    git_env = {
        **os.environ,
        "READTHEDOCS_GIT_CLONE_TOKEN": project.get("clone_token") or "",
    }

    tmp = tempfile.mkdtemp(prefix="rtd-bootstrap-")
    try:
        config_path = sparse_clone_yaml(
            repo_url=repo_url,
            refspec=refspec,
            ssh_key=ssh_key,
            dest=tmp,
            env=git_env,
            yaml_path=project.get("readthedocs_yaml_path"),
        )
        if config_path is None:
            raise PreContainerFailure(BuildUserError.NO_CONFIG_FILE_DEPRECATED)
        # Parse build.os and resolve alias.
        build_os = read_build_os(config_path)
    except subprocess.CalledProcessError as exc:
        # git clone / sparse-checkout failed — surface as an app error
        # (could be network, auth, etc).
        log.error(
            "git sparse-clone failed.",
            returncode=exc.returncode,
            stderr=(exc.stderr or b"").decode(errors="replace")[:2000],
        )
        raise PreContainerFailure(BuildAppError.GENERIC_WITH_BUILD_ID)
    except subprocess.TimeoutExpired as exc:
        log.error("git sparse-clone timed out.", timeout_seconds=exc.timeout)
        raise PreContainerFailure(BuildAppError.GENERIC_WITH_BUILD_ID)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Sync tags/branches into the DB (and fail early on a duplicate reserved
    # version). Runs here — before the container — so it can fail the build the
    # way upstream does.
    _sync_versions(project=project, repo_url=repo_url, ssh_key=ssh_key, git_env=git_env)

    return build_os, memory, time_limit_seconds


def _revoke_clone_token(clone_token):
    """
    Kill the GitHub App token this build cloned with.

    GitHub expires it after an hour and that isn't configurable.
    We call ``DELETE /installation/token`` to end it once the build is done.
    """
    prefix = "x-access-token:"
    # Empty for SSH projects and for public repos, which clone unauthenticated.
    if not clone_token or not clone_token.startswith(prefix):
        return

    try:
        response = requests.delete(
            "https://api.github.com/installation/token",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {clone_token.removeprefix(prefix)}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=constants.REVOKE_CLONE_TOKEN_TIMEOUT_SECONDS,
        )
    except Exception:
        log.info("Failed to revoke the clone token.", exc_info=True)
        return

    if response.status_code == 204:
        log.info("Clone token revoked.")
    else:
        log.info("Failed to revoke the clone token.", status_code=response.status_code)


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
    if sender is None or sender.name != "worker.tasks.run_build":
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
