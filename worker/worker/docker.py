"""
Build container lifecycle: start it, stop it, kill it.

The container is a long-lived idle process (``sleep``) that the runner
``docker exec``s into for every build command. It is deliberately dumb: it
holds no credentials, runs no code of ours, and its only job is to be a
namespace with the right image and the docroot mounted.
"""

import os

import docker
import structlog
from docker.errors import NotFound


log = structlog.get_logger(__name__)


# The build tree, shared between this host (where the runner reads the config,
# validates artifacts and uploads them) and the container (where every build
# command actually runs). Mounted at the same path on both sides so the
# runner's paths mean the same thing in both places.
#
# The AMI creates the host ``docs`` user with the same uid/gid as the build
# image's, so no ownership translation is needed.
HOST_DOCROOT = os.environ.get("RTD_HOST_DOCROOT", "/home/docs/checkouts")
CONTAINER_DOCROOT = os.environ.get("RTD_DOCROOT", "/home/docs/checkouts")

# The upstream image format. The exact tags pulled by the AMI are
# enumerated in the ``build_images`` map in ``builder.pkr.hcl``, which
# now lives in readthedocs-ops
# (salt/base/utils/ops/build-isolated/builder.pkr.hcl) — keep this in
# sync with it; it must include every ``build.os`` we want to support.
BUILD_IMAGE_FORMAT = os.environ.get(
    "RTD_BUILD_IMAGE_FORMAT",
    "readthedocs/build:{build_os}",
)

# Docker network the build container joins. ``bridge`` matches the
# legacy production builder's docker config (no special isolation
# between the build container and the host's other docker workload).
# In dev, the compose service overrides this to the compose network
# so the build container can resolve service names (nginx, storage).
BUILD_NETWORK = os.environ.get(
    "RTD_BUILD_NETWORK",
    "bridge",
)

# Daemon this worker talks to. On the AMI it runs as ``docs``, which is in the
# ``docker`` group; in dev the compose service bind-mounts the host socket.
DOCKER_SOCKET = os.environ.get("RTD_DOCKER_SOCKET", "unix:///var/run/docker.sock")
DOCKER_VERSION = os.environ.get("RTD_DOCKER_VERSION", "auto")


def get_client():
    """
    Build the Docker client for one build.
    """
    return docker.APIClient(base_url=DOCKER_SOCKET, version=DOCKER_VERSION)


def container_name(build_pk: int) -> str:
    """Container name shared by this worker, the runner, and ``cancel_build``."""
    return f"build-{build_pk}"


def kill_container(client, build_pk: int):
    """Force-kill the build container. No-op if it isn't running."""
    try:
        client.kill(container_name(build_pk))
    except NotFound:
        pass


def stop_container(client, build_pk: int):
    """Remove the build container. No-op if it's already gone."""
    try:
        client.remove_container(container_name(build_pk), force=True)
    except NotFound:
        pass


def start_healthcheck(client, container, *, url, host_header, delay):
    """
    Ping ``Build.healthcheck`` from inside the container.

    ``--insecure`` plus an explicit ``Host`` header because in production the
    URL is the internal load balancer's raw AWS DNS name — its certificate is
    for the production domain, and the LB routes on ``Host``.
    """
    command = (
        "/bin/bash -c 'while true; do "
        f'curl --insecure --silent --max-time 2 -H "Host: {host_header}" -X POST {url}'
        f"; sleep {delay}; done;'"
    )
    exec_id = client.exec_create(container=container, cmd=command, stdout=False, stderr=False)
    client.exec_start(exec_id=exec_id["Id"], detach=True)
    log.info("Healthcheck started.", container=container, url=url, delay=delay)


def start_container(client, *, build_pk, build_os, memory):
    """
    Start the build container detached and return its name.

    The container runs ``sleep infinity`` as its only process — all real work
    arrives later as ``docker exec`` from the runner. It runs as the image's
    default user (``docs``); privileged steps get ``user="root"`` per exec,
    which is what removed the need to run the whole container as root.

    Raises ``docker.errors.APIError`` if the container won't start; the caller
    turns that into a pre-container build failure.
    """
    image = BUILD_IMAGE_FORMAT.format(build_os=build_os)
    name = container_name(build_pk)

    stop_container(client, build_pk)

    container = client.create_container(
        image=image,
        name=name,
        # ``exec`` so the sleep is PID 1 and receives ``docker kill``'s signal
        # directly rather than through a shell that would ignore it.
        entrypoint=["/bin/sh", "-c", "exec sleep infinity"],
        detach=True,
        host_config=client.create_host_config(
            mem_limit=memory,
            network_mode=BUILD_NETWORK,
            binds={HOST_DOCROOT: {"bind": CONTAINER_DOCROOT, "mode": "rw"}},
        ),
    )
    client.start(container)

    log.info(
        "Build container started.",
        container=name,
        image=image,
        memory=memory,
        network=BUILD_NETWORK,
        docroot=f"{HOST_DOCROOT}:{CONTAINER_DOCROOT}",
    )
    return name
