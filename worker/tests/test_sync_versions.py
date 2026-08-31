"""Tests for the worker's pre-build version sync (``tasks._sync_versions``)."""

import subprocess
from unittest import mock

import pytest

from worker import constants
from worker import tasks
from worker.exceptions import PreContainerFailure
from worker.exceptions import RepositoryError


def _run(monkeypatch, *, project, lsremote_out="", lsremote_exc=None):
    """Run ``_sync_versions`` with ``lsremote`` and the Celery app stubbed."""

    def fake_lsremote(**kwargs):
        if lsremote_exc:
            raise lsremote_exc
        return lsremote_out

    fake_app = mock.MagicMock()
    monkeypatch.setattr(tasks, "lsremote", fake_lsremote)
    monkeypatch.setattr(tasks, "app", fake_app)
    tasks._sync_versions(
        project=project,
        repo_url="https://github.com/readthedocs/test-builds.git",
        ssh_key="",
        git_env={},
    )
    return fake_app


def test_dispatches_sync_versions_task_for_valid_versions(monkeypatch):
    out = "aaa\trefs/heads/main\nbbb\trefs/tags/v1.0\n"
    app = _run(monkeypatch, project={"id": 7, "features": []}, lsremote_out=out)

    app.send_task.assert_called_once()
    args, kwargs = app.send_task.call_args
    assert args[0] == constants.SYNC_VERSIONS_TASK_NAME
    assert kwargs["queue"] == constants.SYNC_VERSIONS_TASK_QUEUE
    payload = kwargs["kwargs"]
    assert payload["project_pk"] == 7
    assert {"identifier": "main", "verbose_name": "main"} in payload["branches_data"]
    assert {"identifier": "bbb", "verbose_name": "v1.0"} in payload["tags_data"]


def test_fails_the_build_on_a_duplicate_reserved_version(monkeypatch):
    # ``stable`` as both a branch and a tag.
    out = "aaa\trefs/heads/stable\nbbb\trefs/tags/stable\n"
    with pytest.raises(PreContainerFailure) as excinfo:
        _run(monkeypatch, project={"id": 7, "features": []}, lsremote_out=out)
    assert excinfo.value.message_id == RepositoryError.DUPLICATED_RESERVED_VERSIONS


def test_lsremote_failure_is_non_fatal_and_skips_dispatch(monkeypatch):
    exc = subprocess.CalledProcessError(128, "git ls-remote")
    app = _run(monkeypatch, project={"id": 7, "features": []}, lsremote_exc=exc)
    app.send_task.assert_not_called()


def test_skips_entirely_when_both_sync_flags_are_set(monkeypatch):
    calls = {"lsremote": 0}

    def fake_lsremote(**kwargs):
        calls["lsremote"] += 1
        return ""

    fake_app = mock.MagicMock()
    monkeypatch.setattr(tasks, "lsremote", fake_lsremote)
    monkeypatch.setattr(tasks, "app", fake_app)

    tasks._sync_versions(
        project={"id": 7, "features": ["skip_sync_tags", "skip_sync_branches"]},
        repo_url="https://github.com/x/y.git",
        ssh_key="",
        git_env={},
    )

    assert calls["lsremote"] == 0
    fake_app.send_task.assert_not_called()
