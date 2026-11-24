"""Tests for the worker's pre-build version sync (``tasks._sync_versions``)."""

import subprocess
from unittest import mock

import pytest
import structlog.testing

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


TOKEN = "x-access-token:ghs_SECRETSECRETSECRET"
AUTH_URL = f"https://{TOKEN}@github.com/readthedocs/private.git"


def _run_with_token(monkeypatch, exc):
    """``_run`` for an HTTPS private repo, returning the captured log events."""

    def fake_lsremote(**kwargs):
        raise exc

    monkeypatch.setattr(tasks, "lsremote", fake_lsremote)
    monkeypatch.setattr(tasks, "app", mock.MagicMock())
    with structlog.testing.capture_logs() as events:
        tasks._sync_versions(
            project={"id": 7, "features": []},
            repo_url="https://github.com/readthedocs/private.git",
            ssh_key="",
            git_env={"READTHEDOCS_GIT_CLONE_TOKEN": TOKEN},
        )
    return events


@pytest.mark.parametrize(
    "exc",
    [
        # Both exceptions render the full argv -- token-bearing URL included --
        # in ``str()``, and git's stderr quotes the URL too.
        subprocess.CalledProcessError(
            128,
            ["git", "ls-remote", "--heads", "--", AUTH_URL],
            stderr=f"fatal: unable to access '{AUTH_URL}': Could not resolve host\n",
        ),
        subprocess.TimeoutExpired(
            ["git", "ls-remote", "--heads", "--", AUTH_URL],
            timeout=300,
            stderr=f"fatal: unable to access '{AUTH_URL}'".encode(),
        ),
    ],
    ids=["CalledProcessError", "TimeoutExpired"],
)
def test_lsremote_failure_never_logs_the_clone_token(monkeypatch, exc):
    events = _run_with_token(monkeypatch, exc)

    assert len(events) == 1
    rendered = repr(events[0])
    assert "ghs_SECRETSECRETSECRET" not in rendered
    assert TOKEN not in rendered
    # The useful part of stderr survives, masked.
    assert "unable to access 'https://****@github.com" in events[0]["stderr"]


def test_lsremote_failure_logs_returncode_and_timeout(monkeypatch):
    events = _run_with_token(monkeypatch, subprocess.CalledProcessError(128, ["git"]))
    assert events[0]["returncode"] == 128
    assert events[0]["timeout_seconds"] is None

    events = _run_with_token(monkeypatch, subprocess.TimeoutExpired(["git"], timeout=300))
    assert events[0]["returncode"] is None
    assert events[0]["timeout_seconds"] == 300


def test_redact_clone_token_handles_empty_and_bytes_stderr():
    env = {"READTHEDOCS_GIT_CLONE_TOKEN": TOKEN}
    assert tasks._redact_clone_token(None, env) == ""
    assert tasks._redact_clone_token(b"", env) == ""
    assert tasks._redact_clone_token(b"bad " + TOKEN.encode(), env) == "bad ****"
    # No token configured (public repo): stderr passes through untouched.
    assert tasks._redact_clone_token("fatal: nope", {}) == "fatal: nope"
    # Long output is capped so a chatty remote can't flood the log line.
    assert len(tasks._redact_clone_token("x" * 5000, env)) == 2000


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
