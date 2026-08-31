import pytest
from docker.errors import NotFound

from worker import docker


class FakeClient:
    """Stands in for ``docker.APIClient``, recording what it was asked to do."""

    def __init__(self, *, missing=False):
        self.created = None
        self.host_config = None
        self.started = None
        self.killed = []
        self.removed = []
        self._missing = missing

    def create_host_config(self, **kwargs):
        self.host_config = kwargs
        return kwargs

    def create_container(self, **kwargs):
        self.created = kwargs
        return {"Id": "container-id"}

    def start(self, container):
        self.started = container

    def kill(self, name):
        if self._missing:
            raise NotFound("no such container")
        self.killed.append(name)

    def remove_container(self, name, force=False):
        if self._missing:
            raise NotFound("no such container")
        self.removed.append((name, force))


@pytest.fixture
def client():
    return FakeClient()


def start_container(client, **kwargs):
    defaults = dict(build_pk=42, build_os="ubuntu-24.04", memory="7g")
    return docker.start_container(client, **{**defaults, **kwargs})


def test_container_name_is_derived_from_the_build():
    assert docker.container_name(42) == "build-42"


def test_start_container_removes_a_stale_container_first(client):
    start_container(client)

    assert client.removed == [("build-42", True)]


def test_start_container_names_the_container_after_the_build(client):
    start_container(client)

    assert client.created["name"] == "build-42"


def test_start_container_returns_the_container_name(client):
    assert start_container(client) == "build-42"


def test_start_container_uses_the_image_for_the_build_os(client):
    start_container(client, build_os="ubuntu-22.04")

    assert client.created["image"] == "readthedocs/build:ubuntu-22.04"


def test_start_container_passes_memory_straight_through(client):
    start_container(client, memory="3g")

    assert client.host_config["mem_limit"] == "3g"


def test_start_container_runs_detached_and_starts_it(client):
    start_container(client)

    assert client.created["detach"] is True
    assert client.started == {"Id": "container-id"}


def test_start_container_does_not_override_the_image_user(client):
    """
    The container runs as the image's default (``docs``) user.

    Root arrives per-command via ``docker exec``'s ``user``, which is what let
    us stop running the whole container privileged.
    """
    start_container(client)

    assert "user" not in client.created


def test_start_container_mounts_the_docroot_at_the_same_path(client):
    """
    Same path on both sides, so the runner's paths mean the same thing whether
    it's reading a file here or exec'ing a command in there.
    """
    start_container(client)

    (host, target), = client.host_config["binds"].items()
    assert host == target["bind"]
    assert target["mode"] == "rw"


def test_start_container_mounts_nothing_else(client):
    """The venv, interpreter, source and rclone mounts went with the runner."""
    start_container(client)

    assert len(client.host_config["binds"]) == 1


def test_start_container_carries_no_environment(client):
    """
    The build API key stays on the host with the runner.

    Per-command environment is passed through ``docker exec`` instead.
    """
    start_container(client)

    assert not client.created.get("environment")


def test_start_container_idles_as_pid_1(client):
    """
    ``exec`` so the sleep is PID 1 and receives ``docker kill``'s signal
    directly, rather than a shell swallowing it.
    """
    start_container(client)

    assert client.created["entrypoint"] == ["/bin/sh", "-c", "exec sleep infinity"]


def test_stop_container_force_removes_it(client):
    docker.stop_container(client, 42)

    assert client.removed == [("build-42", True)]


def test_stop_container_ignores_an_already_gone_container():
    docker.stop_container(FakeClient(missing=True), 42)


def test_kill_container_kills_by_name(client):
    docker.kill_container(client, 42)

    assert client.killed == ["build-42"]


def test_kill_container_ignores_an_already_gone_container():
    docker.kill_container(FakeClient(missing=True), 42)
