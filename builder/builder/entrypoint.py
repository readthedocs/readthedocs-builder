"""
Running one build.

``worker.tasks.run_build`` calls this in-process, having already started the
build container that every command will ``docker exec`` into.

The worker hands us everything it already knows — an authenticated API client,
the Build and Version it fetched, the container it started — so nothing here
is looked up a second time. ``builder.settings`` holds only host
configuration, read once at import.
"""

import os

import structlog

from builder.api_models import APIVersion
from builder.director import TaskData
from builder.filesystem import safe_rmtree
from builder.runner import Runner


log = structlog.get_logger(__name__)


def run_build(
    *,
    api_client,
    docker_client,
    build: dict,
    version: dict,
    container_name: str,
    production_domain: str,
    allow_private_repos: bool = False,
    s3_endpoint_url: str | None = None,
) -> bool:
    """
    Run ``build``. Returns its success boolean.

    :param api_client: authenticated client for the RTD API. The worker set it
        up and already used it to fetch ``build`` and ``version``; sharing it
        keeps the build to one round trip for each.
    :param docker_client: the client the worker started the container with.
        Shared for the same reason: one build, one connection to the daemon.
    :param build: the Build payload, as returned by the API.
    :param version: the Version payload, with its Project nested inside.
    :param container_name: the running build container to ``docker exec``
        into. The worker owns its lifecycle.
    :param production_domain: dashboard domain, used for the build URL and the
        ``READTHEDOCS_PRODUCTION_DOMAIN`` variable user commands see.
    :param allow_private_repos: whether this Read the Docs supports private
        repositories, which gates the SSH deploy-key path.
    :param s3_endpoint_url: S3-compatible endpoint to use instead of AWS. Only
        set in development.

    Every failure from here on is reported by the runner itself, through the
    API — the worker's job was to get us this far.
    """
    build_pk = build["id"]
    structlog.contextvars.bind_contextvars(build_pk=build_pk)

    version = APIVersion(**version)
    project = version.project

    build_url = f"https://{production_domain}/projects/{project.slug}/builds/{build_pk}/"
    log.info("Building.", build_url=build_url, project_slug=project.slug)

    # Wipe the project's full doc_path so re-runs start fresh: stale
    # virtualenvs and conda envs from a prior asdf-installed Python
    # otherwise point at no-longer-existing interpreters and break
    # ``virtualenv`` / ``conda env create``. Mirrors upstream's
    # ``clean_build(version)`` step.
    if os.path.exists(project.doc_path):
        safe_rmtree(project.doc_path, ignore_errors=True)

    data = TaskData(
        api_client=api_client,
        project=project,
        version=version,
        build=build,
        container_name=container_name,
        docker_client=docker_client,
        production_domain=production_domain,
        allow_private_repos=allow_private_repos,
        s3_endpoint_url=s3_endpoint_url,
    )

    success = Runner(data=data).run()
    log.info("Build complete.", success=success)
    return success
