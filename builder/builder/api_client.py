"""
HTTP client for the Read the Docs API v2.

Ported from ``readthedocs.api.v2.client``, dropping the Django and DRF
coupling: serialization is plain JSON, and upstream's TLS Host-header
adapter gymnastics are handled with a plain ``Host`` request header.
"""

import requests
import slumber
import structlog
from slumber import serialize
from urllib3.util.retry import Retry


log = structlog.get_logger(__name__)


DEFAULT_TIMEOUT_SECONDS = 30


class TimeoutHTTPAdapter(requests.adapters.HTTPAdapter):
    """HTTP adapter that applies a default timeout to every request."""

    def __init__(self, *args, timeout=DEFAULT_TIMEOUT_SECONDS, **kwargs):
        self._timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return super().send(request, **kwargs)


def setup_api(
    *,
    api_url: str,
    build_api_key: str,
    production_domain: str | None = None,
) -> slumber.API:
    """
    Build a slumber client pointed at the RTD API v2.

    All args explicit — the worker has no shared settings module to
    fall back to. ``production_domain`` forces the Host header for
    setups where the API is fronted by a Host-routing proxy; pass
    ``None`` when the URL's host already matches.
    """
    if not api_url:
        raise RuntimeError("api_url is required")
    if not build_api_key:
        raise RuntimeError("build_api_key is required")

    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        status=3,
        backoff_factor=0.5,
        allowed_methods=("GET", "PUT", "PATCH", "POST"),
        status_forcelist=(408, 413, 429, 500, 502, 503, 504),
    )
    session.mount(api_url, TimeoutHTTPAdapter(max_retries=retry))
    session.headers["Authorization"] = f"Token {build_api_key}"

    if production_domain:
        session.headers["Host"] = production_domain

    return slumber.API(
        base_url=f"{api_url.rstrip('/')}/api/v2/",
        serializer=serialize.Serializer(
            default="json",
            serializers=[serialize.JsonSerializer()],
        ),
        session=session,
    )


def get_build(api_client, build_pk: int) -> dict:
    """
    Fetch build JSON from the API and strip API-internal fields.

    Drops the nested ``project`` and the URI/href fields. ``version`` is
    kept as the version pk so callers can derive which version to fetch
    without requiring it separately.
    """
    if not build_pk:
        return {}
    return api_client.build(build_pk).get()


def get_version(api_client, version_pk: int) -> dict:
    """Fetch raw version JSON from the API."""
    return api_client.version(version_pk).get()


def get_project(api_client, project_pk: int) -> dict:
    """
    Fetch raw project JSON from the API.

    Includes ``clone_token`` — used by the sparse-clone helper for
    HTTPS auth. For SSH projects, ``clone_token`` will be empty and the
    worker falls back to the SSH-key endpoint.
    """
    return api_client.project(project_pk).get()


def get_project_ssh_key(api_client, project_pk: int) -> str:
    """
    Fetch the project's SSH deploy key (private key content).

    Returns the private key string, or empty string when the project has no SSH key.
    """
    data = api_client.project(project_pk).key.get()
    return data.get("private_key", "") or ""
