"""
Integration tests against a real Docker daemon.

Everything else in this suite mocks the daemon, which means it verifies that
we *build* the right commands but not that they *work*. These tests cover the
assumptions mocks structurally can't:

- the docroot bind-mount resolves to the same files on both sides
- ``user`` really drops privileges
- the container's shell expands the variables we deliberately leave unescaped
- exit codes come back, including the OOM code

Excluded from the default run (see ``addopts`` in the workspace pyproject);
run with ``-m docker``.
"""

import os
import time
import uuid

import pytest

from builder.environments import DockerBuildCommand
from builder.environments import DockerBuildEnvironment
from worker import docker as worker_docker


pytestmark = pytest.mark.docker

BUILD_OS = os.environ.get("RTD_TEST_BUILD_OS", "ubuntu-24.04")
IMAGE = f"readthedocs/build:{BUILD_OS}"


def _image_present(client):
    return any(IMAGE in (image.get("RepoTags") or []) for image in client.images())


@pytest.fixture(scope="module")
def client():
    client = worker_docker.get_client()
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"No reachable Docker daemon: {exc}")
    if not _image_present(client):
        pytest.skip(f"{IMAGE} not pulled; `docker pull {IMAGE}` to run these.")
    return client


@pytest.fixture
def docroot(tmp_path, monkeypatch):
    """
    A docroot mounted at the same path inside and outside the container.

    ``tmp_path`` is world-traversable enough for the container's ``docs`` user
    to reach, and using the identical path on both sides is exactly the
    property under test.
    """
    path = tmp_path / "checkouts"
    path.mkdir()
    path.chmod(0o777)
    monkeypatch.setattr(worker_docker, "HOST_DOCROOT", str(path))
    monkeypatch.setattr(worker_docker, "CONTAINER_DOCROOT", str(path))
    return path


@pytest.fixture
def container(client, docroot):
    """A real build container, torn down afterwards."""
    build_pk = uuid.uuid4().int % 100000
    name = worker_docker.start_container(
        client, build_pk=build_pk, build_os=BUILD_OS, memory="512m"
    )
    try:
        yield name
    finally:
        worker_docker.stop_container(client, build_pk)


@pytest.fixture
def build_env(client, container):
    return DockerBuildEnvironment(
        record=False,
        container_name=container,
        docker_client=client,
    )


def run(build_env, *command, **kwargs):
    cmd = DockerBuildCommand(command, build_env=build_env, **kwargs)
    cmd.run()
    return cmd


# ---------------------------------------------------------------------------
# The docroot mount — the assumption the whole design rests on
# ---------------------------------------------------------------------------


def test_a_file_written_here_is_readable_in_the_container(build_env, docroot):
    """
    The runner reads and writes the build tree from the host; every command
    sees it from inside. If this breaks, nothing else matters.
    """
    (docroot / "written-by-the-runner").write_text("hello from the host")

    cmd = run(build_env, "cat", str(docroot / "written-by-the-runner"))

    assert cmd.exit_code == 0
    assert "hello from the host" in cmd.output


def test_a_file_written_in_the_container_is_readable_here(build_env, docroot):
    """The other direction: artifact validation and upload depend on it."""
    target = docroot / "written-by-the-build"

    cmd = run(build_env, "sh", "-c", f"echo hello from the container > {target}")

    assert cmd.exit_code == 0
    assert target.read_text().strip() == "hello from the container"


# ---------------------------------------------------------------------------
# Privilege drop
# ---------------------------------------------------------------------------


def test_commands_run_as_the_build_user_by_default(build_env):
    assert run(build_env, "whoami").output.strip() == "docs"


def test_privileged_commands_run_as_root(build_env):
    """``apt-get`` needs this; it's what replaced ``runuser``."""
    assert run(build_env, "whoami", user="root").output.strip() == "root"


def test_the_build_user_cannot_write_outside_the_docroot(build_env):
    cmd = run(build_env, "touch", "/etc/should-not-be-writable")

    assert cmd.exit_code != 0


# ---------------------------------------------------------------------------
# Shell semantics
# ---------------------------------------------------------------------------


def test_allowlisted_variables_are_expanded_by_the_containers_shell(build_env, docroot):
    """
    ``$READTHEDOCS_OUTPUT`` and friends are deliberately left unescaped so the
    container expands them. Mocked tests only assert the command *string*.
    """
    cmd = DockerBuildCommand(
        ("echo", "$READTHEDOCS_OUTPUT"),
        build_env=build_env,
        environment={"READTHEDOCS_OUTPUT": str(docroot / "_readthedocs")},
    )
    cmd.run()

    assert cmd.output.strip() == str(docroot / "_readthedocs")


def test_shell_metacharacters_survive_the_round_trip(build_env):
    """``pip install requests<0.8`` must not be read as a redirect."""
    cmd = run(build_env, "echo", "requests<0.8")

    assert cmd.exit_code == 0
    assert "requests<0.8" in cmd.output


def test_user_commands_are_run_as_shell_expressions(build_env):
    """``build.jobs`` / ``build.commands`` are meant to be shell, unescaped."""
    cmd = run(build_env, "echo one && echo two", escape_command=False)

    assert cmd.output.split() == ["one", "two"]


def test_bin_path_is_prepended_to_the_containers_path(build_env):
    cmd = run(build_env, "sh", "-c", "echo $PATH", bin_path="/custom/bin")

    assert cmd.output.startswith("/custom/bin:")
    assert "/usr/bin" in cmd.output


def test_the_working_directory_is_honoured(build_env, docroot):
    subdir = docroot / "checkout"
    subdir.mkdir()

    assert run(build_env, "pwd", cwd=str(subdir)).output.strip() == str(subdir)


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def test_a_failing_command_reports_its_exit_code(build_env):
    assert run(build_env, "sh", "-c", "exit 42").exit_code == 42


def test_a_successful_command_reports_zero(build_env):
    assert run(build_env, "true").exit_code == 0


def test_stderr_is_captured_with_stdout_by_default(build_env):
    cmd = run(build_env, "sh", "-c", "echo to-stderr >&2")

    assert "to-stderr" in cmd.output


def test_stderr_is_separated_when_demuxed(build_env):
    cmd = run(build_env, "sh", "-c", "echo out; echo err >&2", demux=True)

    assert "out" in cmd.output
    assert "err" in cmd.error


def test_a_memory_kill_is_reported_as_such(build_env):
    """
    The container is capped at 512m; allocating past it gets the process
    SIGKILLed, which ``docker exec`` reports as 137.
    """
    cmd = run(build_env, "tail /dev/zero", escape_command=False)

    assert cmd.exit_code == 137
    assert "excessive memory" in cmd.output


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


def test_healthcheck_loop_actually_runs_in_the_container(client, container, build_env):
    """
    The mocked tests assert we build the right exec; this asserts it survives
    ``shlex.split`` and bash, and keeps looping.
    """
    worker_docker.start_healthcheck(
        client,
        container,
        # Nowhere real: curl failing is fine, the loop must keep going anyway.
        url="http://127.0.0.1:9/healthcheck/",
        host_header="readthedocs.org",
        delay=1,
    )

    # Give bash a moment to be up, then confirm the loop is still there.
    time.sleep(2)
    processes = run(build_env, "ps", "-eo", "args").output

    assert "while true" in processes


def test_healthcheck_does_not_block_other_commands(client, container, build_env):
    """Detached: a foreground exec of an infinite loop would hang the build."""
    worker_docker.start_healthcheck(
        client, container, url="http://127.0.0.1:9/", host_header="rtd.org", delay=1
    )

    assert run(build_env, "echo", "still-responsive").output.strip() == "still-responsive"


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------


def test_stop_container_removes_it(client, docroot):
    build_pk = uuid.uuid4().int % 100000
    worker_docker.start_container(client, build_pk=build_pk, build_os=BUILD_OS, memory="512m")

    worker_docker.stop_container(client, build_pk)

    names = [name for c in client.containers(all=True) for name in c.get("Names", [])]
    assert f"/{worker_docker.container_name(build_pk)}" not in names


def test_start_container_replaces_a_stale_one_of_the_same_name(client, docroot):
    build_pk = uuid.uuid4().int % 100000
    worker_docker.start_container(client, build_pk=build_pk, build_os=BUILD_OS, memory="512m")

    try:
        worker_docker.start_container(
            client, build_pk=build_pk, build_os=BUILD_OS, memory="512m"
        )
    finally:
        worker_docker.stop_container(client, build_pk)
