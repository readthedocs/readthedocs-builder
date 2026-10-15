"""
EC2 instance lifecycle, delegated to the Read the Docs API.

The instance holds no AWS credentials. ``protect`` and ``release`` hit the
``instance/*`` build endpoints and the web tier makes the Auto Scaling calls,
or skips them in development: the worker always calls the API so dev matches
production. ``poweroff`` is the fallback when the API can't be reached: the
launch template terminates the instance on shutdown.
"""

import os
import subprocess

import structlog


log = structlog.get_logger(__name__)


STATE_TERMINATED = "terminated"
STATE_KEPT = "kept"
STATE_FAILED = "failed"

POWEROFF_COMMAND = ["sudo", "--non-interactive", "/usr/bin/systemctl", "poweroff"]
POWEROFF_TIMEOUT_SECONDS = 30


def _is_docker_compose() -> bool:
    return bool(os.environ.get("RTD_DOCKER_COMPOSE"))


def protect(api_client, build_pk: int, hostname: str) -> bool:
    """Claim ``build_pk`` for this instance and protect it from ASG scale-in."""
    try:
        api_client.build(build_pk).instance.protect.post({"builder": hostname})
    except Exception:
        # Never fail a build over this; the ASG may still scale the instance in.
        log.exception("Failed to protect the instance.", builder=hostname)
        return False

    log.info("Instance protected from scale-in.", builder=hostname)
    return True


def release(api_client, build_pk: int) -> str:
    """
    Ask the API to release and terminate this instance.

    Returns ``terminated``, ``kept`` or ``failed``. Only ``failed`` (or an
    unreachable API) should make the caller power off the host.
    """
    try:
        response = api_client.build(build_pk).instance.release.post() or {}
    except Exception:
        log.exception("Failed to release the instance.")
        return STATE_FAILED

    state = response.get("state", STATE_FAILED)
    log.info("Instance released.", state=state, decremented=response.get("decremented"))
    return state


def poweroff():
    """Shut the host down. The launch template terminates it on shutdown."""
    if _is_docker_compose():
        log.warning("Refusing to power off under docker-compose.")
        return

    log.warning("Powering off the instance.")
    try:
        subprocess.run(POWEROFF_COMMAND, check=True, timeout=POWEROFF_TIMEOUT_SECONDS)
    except Exception:
        log.exception("Power off failed; the instance needs manual cleanup.")
