"""Tests for the runner's entry point — what the worker actually calls."""

from unittest import mock

import pytest

from builder import entrypoint


VERSION_PAYLOAD = {
    "slug": "latest",
    "type": "branch",
    "verbose_name": "latest",
    "identifier": "abc123",
    "git_identifier": "abc123",
    "canonical_url": "https://pip.readthedocs.io/en/latest/",
    "machine": False,
    "project": {
        "slug": "pip",
        "name": "Pip",
        "language": "en",
        "repo": "https://github.com/readthedocs/pip",
        "clone_token": None,
        "features": [],
    },
}


@pytest.fixture
def runner(monkeypatch):
    """Stand in for the Runner, capturing the TaskData it was handed."""
    fake = mock.MagicMock()
    fake.return_value.run.return_value = True
    monkeypatch.setattr(entrypoint, "Runner", fake)
    return fake


def run(**overrides):
    kwargs = {
        "api_client": mock.MagicMock(),
        "docker_client": mock.MagicMock(),
        "build": {"id": 42, "version": 10},
        "version": dict(VERSION_PAYLOAD),
        "container_name": "build-42",
        "production_domain": "readthedocs.org",
    }
    kwargs.update(overrides)
    return entrypoint.run_build(**kwargs)


def task_data(runner):
    return runner.call_args.kwargs["data"]


def test_run_build_returns_the_runners_result(docroot, runner):
    runner.return_value.run.return_value = False

    assert run() is False


def test_run_build_reuses_the_workers_api_client(docroot, runner):
    """The worker already authenticated and fetched with it; don't build another."""
    api_client = mock.MagicMock()

    run(api_client=api_client)

    assert task_data(runner).api_client is api_client


def test_run_build_does_not_refetch_the_build_or_version(docroot, runner):
    """Both arrive as arguments — the runner must not hit the API for them."""
    api_client = mock.MagicMock()

    run(api_client=api_client)

    assert task_data(runner).build == {"id": 42, "version": 10}
    assert task_data(runner).version.slug == "latest"
    api_client.build.assert_not_called()
    api_client.version.assert_not_called()


def test_run_build_passes_the_container_the_worker_started(docroot, runner):
    docker_client = mock.MagicMock()

    run(container_name="build-99", docker_client=docker_client)

    assert task_data(runner).container_name == "build-99"
    assert task_data(runner).docker_client is docker_client


def test_run_build_passes_the_platform_settings_through(docroot, runner):
    run(
        production_domain="readthedocs.com",
        allow_private_repos=True,
        s3_endpoint_url="http://storage:9000",
    )

    data = task_data(runner)
    assert data.production_domain == "readthedocs.com"
    assert data.allow_private_repos is True
    assert data.s3_endpoint_url == "http://storage:9000"


def test_run_build_defaults_to_a_public_platform(docroot, runner):
    run()

    assert task_data(runner).allow_private_repos is False
    assert task_data(runner).s3_endpoint_url is None
