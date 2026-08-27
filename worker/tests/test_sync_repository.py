"""Tests for the standalone version sync task (``tasks.sync_repository``)."""

from unittest import mock

import pytest

from conftest import API_URL
from conftest import JSON
from worker import tasks
from worker.exceptions import PreContainerFailure
from worker.exceptions import RepositoryError


ENVIRONMENT = {"RTD_API_URL": API_URL, "RTD_PRODUCTION_DOMAIN": "readthedocs.org"}

PROJECT = {
    "id": 7,
    "slug": "pip",
    "repo": "https://github.com/readthedocs/pip",
    "clone_token": "TOKEN",
    "features": [],
}


@pytest.fixture
def project_api(requests_mock):
    """Register the project endpoint the task fetches."""

    def _register(project=None):
        requests_mock.get(
            f"{API_URL}/api/v2/project/7/",
            json=project or PROJECT,
            headers=JSON,
        )
        return requests_mock

    return _register


def _call():
    return tasks.sync_repository(
        project_pk=7,
        build_api_key="KEY",
        environment=ENVIRONMENT,
    )


def test_syncs_the_versions_it_finds(monkeypatch, project_api):
    project_api()
    monkeypatch.setattr(tasks, "lsremote", lambda **kwargs: "aaa\trefs/heads/main\n")
    fake_app = mock.MagicMock()
    monkeypatch.setattr(tasks, "app", fake_app)

    _call()

    payload = fake_app.send_task.call_args.kwargs["kwargs"]
    assert payload["project_pk"] == 7
    assert {"identifier": "main", "verbose_name": "main"} in payload["branches_data"]


def test_passes_the_clone_token_to_git(monkeypatch, project_api):
    project_api()
    captured = {}

    def fake_lsremote(**kwargs):
        captured.update(kwargs)
        return ""

    monkeypatch.setattr(tasks, "lsremote", fake_lsremote)
    monkeypatch.setattr(tasks, "app", mock.MagicMock())

    _call()

    assert captured["env"]["READTHEDOCS_GIT_CLONE_TOKEN"] == "TOKEN"
    assert captured["ssh_key"] == ""


def test_fetches_the_ssh_key_for_ssh_repositories(monkeypatch, project_api, requests_mock):
    project_api({**PROJECT, "repo": "git@github.com:readthedocs/pip.git"})
    requests_mock.get(
        f"{API_URL}/api/v2/project/7/key/",
        json={"private_key": "PRIVATE-KEY"},
        headers=JSON,
    )
    captured = {}

    def fake_lsremote(**kwargs):
        captured.update(kwargs)
        return ""

    monkeypatch.setattr(tasks, "lsremote", fake_lsremote)
    monkeypatch.setattr(tasks, "app", mock.MagicMock())

    _call()

    assert captured["ssh_key"] == "PRIVATE-KEY"


def test_duplicated_reserved_versions_fail_the_task(monkeypatch, project_api):
    project_api()
    out = "aaa\trefs/heads/latest\nbbb\trefs/tags/latest\n"
    monkeypatch.setattr(tasks, "lsremote", lambda **kwargs: out)
    fake_app = mock.MagicMock()
    monkeypatch.setattr(tasks, "app", fake_app)

    with pytest.raises(PreContainerFailure) as excinfo:
        _call()

    assert excinfo.value.message_id == RepositoryError.DUPLICATED_RESERVED_VERSIONS
    fake_app.send_task.assert_not_called()


def test_does_not_touch_the_instance_lifecycle(monkeypatch, project_api):
    # One ls-remote must not protect the instance from scale-in, nor terminate
    # it. ``_on_run_build_postrun`` filters on the task name for the latter.
    project_api()
    monkeypatch.setattr(tasks, "lsremote", lambda **kwargs: "")
    monkeypatch.setattr(tasks, "app", mock.MagicMock())
    protection = mock.MagicMock()
    terminate = mock.MagicMock()
    monkeypatch.setattr(tasks, "set_scale_in_protection", protection)
    monkeypatch.setattr(tasks, "self_terminate", terminate)

    _call()
    tasks._on_run_build_postrun(sender=tasks.sync_repository, kwargs={})

    protection.assert_not_called()
    terminate.assert_not_called()
