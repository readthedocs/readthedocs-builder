from pathlib import Path

import pytest
import yaml

from builder.api_client import setup_api


API_URL = "https://readthedocs.org"

# slumber picks its deserializer off the response Content-Type — mocked
# responses carrying JSON must say so or the client hands back raw bytes.
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def api_client():
    """
    A real slumber client, to be driven against the ``requests_mock`` fixture.

    Mocking at the HTTP layer (rather than stubbing the client) keeps the
    client's own URL building, auth header and JSON serialization under test.
    """
    return setup_api(api_url=API_URL, build_api_key="TOKEN")


@pytest.fixture
def project():
    """Minimal project payload, shaped like the API v2 response."""
    return {
        "container_mem_limit": None,
        "container_time_limit": None,
        "readthedocs_yaml_path": None,
    }


@pytest.fixture
def write_config():
    """Write a config as YAML to ``path``, creating parents. Returns the path."""

    def _write(path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(data))
        return str(path)

    return _write
