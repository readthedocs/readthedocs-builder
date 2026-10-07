import os
import signal
import time
import types

import pytest
from builder.exceptions import BuildCancelled
from conftest import API_URL
from conftest import JSON
from worker.exceptions import BuildAppError
from worker.exceptions import BuildUserError
from worker.exceptions import PreContainerFailure

from worker import constants
from worker import tasks


@pytest.fixture(autouse=True)
def _no_sync_versions(monkeypatch):
    # ``_prepare_build`` calls ``_sync_versions``, which does real git ls-remote
    # + broker I/O. Neutralize it for the prepare-build tests; its own behavior
    # is covered in ``test_sync_versions.py``.
    monkeypatch.setattr(tasks, "_sync_versions", lambda **kwargs: None)


@pytest.fixture
def fail_build_api(requests_mock):
    """Register the two endpoints ``_fail_build`` hits."""
    requests_mock.post(f"{API_URL}/api/v2/notifications/", status_code=201)
    requests_mock.patch(f"{API_URL}/api/v2/build/42/", status_code=201)
    return requests_mock


def requests_for(requests_mock, method, path):
    """Return the captured requests matching a given method and path."""
    return [
        request
        for request in requests_mock.request_history
        if request.method == method and request.path == path
    ]


@pytest.fixture
def mock_api(requests_mock):
    """
    Serve the build + version endpoints ``_prepare_build`` walks.

    Only the git clone is stubbed by the caller; everything else goes through
    the real API client.
    """

    def _mock(project=None, build=None, version=None):
        project_data = {
            "id": 1,
            "repo": "https://github.com/readthedocs/readthedocs.org.git",
            "clone_token": "",
            "container_mem_limit": None,
            "container_time_limit": None,
            "readthedocs_yaml_path": None,
        }
        project_data.update(project or {})

        build_data = {"id": 42, "version": 10, "commit": "a1b2c3"}
        build_data.update(build or {})

        version_data = {"id": 10, "identifier": "main", "project": project_data}
        version_data.update(version or {})

        # slumber picks its deserializer off Content-Type; without it the
        # response comes back as raw bytes.
        requests_mock.get(f"{API_URL}/api/v2/build/42/", json=build_data, headers=JSON)
        requests_mock.get(f"{API_URL}/api/v2/version/10/", json=version_data, headers=JSON)
        return requests_mock

    return _mock


@pytest.fixture
def prepare_build(api_client, mock_api):
    """Run ``_prepare_build`` against the mocked API."""

    def _prepare(project=None, version=None, build_os_hint=None):
        mock_api(project=project, version=version)
        build, version = tasks._fetch_build(api_client, 42)
        return tasks._prepare_build(build=build, version=version, build_os_hint=build_os_hint)

    return _prepare


def test_prepare_build_falls_back_to_the_default_resources(prepare_build):
    _, memory, time_limit = prepare_build()

    assert memory == constants.BUILD_MEMORY_LIMIT
    assert time_limit == constants.BUILD_TIME_LIMIT


def test_prepare_build_uses_the_project_resources(prepare_build):
    _, memory, time_limit = prepare_build(
        project={"container_mem_limit": "2g", "container_time_limit": 600}
    )

    assert memory == "2g"
    assert time_limit == 600


def test_prepare_build_does_not_cap_large_project_resources(prepare_build):
    """We trust container_*_limit; there is no worker-side ceiling."""
    _, memory, time_limit = prepare_build(
        project={"container_mem_limit": "64g", "container_time_limit": 108000}
    )

    assert memory == "64g"
    assert time_limit == 108000


def test_prepare_build_returns_the_projects_time_limit(prepare_build):
    """
    The limit is returned, not exported.

    It used to be an env var read by a bash watchdog inside the container; the
    worker now arms a SIGALRM around the build instead.
    """
    _, _, time_limit = prepare_build(project={"container_time_limit": 600})

    assert time_limit == 600


def test_prepare_build_does_not_claim_the_build(prepare_build, requests_mock):
    """Claiming ``Build.builder`` is the container's job — the worker must not."""
    prepare_build()

    assert requests_for(requests_mock, "PATCH", "/api/v2/build/42/") == []


def test_prepare_build_starts_from_the_hinted_os(prepare_build):
    build_os, _, _ = prepare_build(build_os_hint="ubuntu-22.04")

    assert build_os == "ubuntu-22.04"


def test_prepare_build_resolves_the_lts_alias_in_the_hint(prepare_build):
    build_os, _, _ = prepare_build(build_os_hint="ubuntu-lts-latest")

    assert build_os == "ubuntu-26.04"


def test_prepare_build_defaults_to_the_latest_lts_without_a_hint(prepare_build):
    """A version with no successful build yet: start somewhere, switch later if needed."""
    build_os, _, _ = prepare_build()

    assert build_os == "ubuntu-26.04"


def test_prepare_build_does_not_touch_the_repo(prepare_build, requests_mock):
    """No sparse clone anymore: the runner reads the config from the real checkout."""
    prepare_build()

    assert [r for r in requests_mock.request_history if "/key/" in r.path] == []


@pytest.fixture
def sync_versions_calls(monkeypatch):
    """Replace ``_sync_versions`` with a spy and return its captured calls."""
    calls = []
    monkeypatch.setattr(tasks, "_sync_versions", lambda **kwargs: calls.append(kwargs))
    return calls


def _start_sync(api_client, mock_api, **mock_kwargs):
    mock_api(**mock_kwargs)
    build, version = tasks._fetch_build(api_client, 42)
    return tasks._start_sync_versions(api_client=api_client, build=build, version=version)


def test_sync_versions_runs_in_the_background_for_branch_versions(
    api_client, mock_api, sync_versions_calls
):
    sync = _start_sync(api_client, mock_api, version={"type": "branch", "verbose_name": "main"})

    sync.join()

    assert len(sync_versions_calls) == 1
    assert sync_versions_calls[0]["repo_url"]
    assert "READTHEDOCS_GIT_CLONE_TOKEN" in sync_versions_calls[0]["git_env"]


def test_sync_versions_is_skipped_for_external_versions(api_client, mock_api, sync_versions_calls):
    # A PR build can't add or remove tags/branches, so there is nothing to sync.
    sync = _start_sync(
        api_client,
        mock_api,
        version={"type": "external", "verbose_name": "2109", "identifier": "9f4d838"},
    )

    assert sync is None
    assert sync_versions_calls == []


def test_sync_versions_is_skipped_for_uploaded_builds(api_client, mock_api, sync_versions_calls):
    # There is no remote to ``git ls-remote``.
    sync = _start_sync(api_client, mock_api, build={"is_uploaded": True})

    assert sync is None


def test_sync_versions_failure_surfaces_on_join(api_client, mock_api, monkeypatch):
    """A duplicated reserved version must still fail the build before it runs."""

    def boom(**kwargs):
        raise PreContainerFailure(BuildUserError.GENERIC, log_message="dup")

    monkeypatch.setattr(tasks, "_sync_versions", boom)
    sync = _start_sync(api_client, mock_api, version={"type": "branch", "verbose_name": "main"})

    with pytest.raises(PreContainerFailure):
        sync.join()


def test_switch_container_replaces_the_container_and_its_healthcheck(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tasks, "stop_container", lambda client, build_pk: calls.append(("stop", build_pk))
    )
    monkeypatch.setattr(
        tasks,
        "start_container",
        lambda client, *, build_pk, build_os, memory: (
            calls.append(("start", build_os, memory)) or "build-42"
        ),
    )
    monkeypatch.setattr(
        tasks,
        "_start_healthcheck",
        lambda client, container, environment, build_pk: calls.append(("healthcheck", container)),
    )

    name = tasks._switch_container(
        object(), build_pk=42, build_os="ubuntu-22.04", memory="7g", environment={}
    )

    assert name == "build-42"
    assert calls == [("stop", 42), ("start", "ubuntu-22.04", "7g"), ("healthcheck", "build-42")]


def test_fetch_build_fails_when_the_build_has_no_version(api_client, requests_mock):
    requests_mock.get(f"{API_URL}/api/v2/build/42/", json={"id": 42}, headers=JSON)

    with pytest.raises(PreContainerFailure) as excinfo:
        tasks._fetch_build(api_client, 42)

    assert excinfo.value.message_id == BuildAppError.GENERIC_WITH_BUILD_ID


def test_fetch_build_fails_when_the_build_does_not_exist(api_client, requests_mock):
    requests_mock.get(f"{API_URL}/api/v2/build/42/", json={}, headers=JSON)

    with pytest.raises(PreContainerFailure) as excinfo:
        tasks._fetch_build(api_client, 42)

    assert excinfo.value.message_id == BuildAppError.GENERIC_WITH_BUILD_ID


def test_fetch_build_raises_cancelled_when_the_build_was_cancelled_while_queued(
    api_client, requests_mock
):
    # A worker started after the revoke was broadcast doesn't know about it
    # (no mingle), so the build's own state is the source of truth.
    requests_mock.get(
        f"{API_URL}/api/v2/build/42/",
        json={"id": 42, "version": 10, "state": "cancelled"},
        headers=JSON,
    )

    with pytest.raises(BuildCancelled):
        tasks._fetch_build(api_client, 42)

    # Stops before the version is fetched.
    assert not requests_for(requests_mock, "GET", "/api/v2/version/10/")


def test_fail_build_reports_the_message_id_from_the_exception(api_client, fail_build_api):
    tasks._fail_build(api_client, 42, PreContainerFailure(BuildUserError.BUILD_OS_REQUIRED))

    posted = requests_for(fail_build_api, "POST", "/api/v2/notifications/")[0].json()
    assert posted["message_id"] == BuildUserError.BUILD_OS_REQUIRED
    assert posted["attached_to"] == "build/42"


def test_fail_build_reports_a_generic_app_error_for_unexpected_exceptions(
    api_client, fail_build_api
):
    tasks._fail_build(api_client, 42, ValueError("boom"))

    posted = requests_for(fail_build_api, "POST", "/api/v2/notifications/")[0].json()
    assert posted["message_id"] == BuildAppError.GENERIC_WITH_BUILD_ID


def test_fail_build_finalizes_the_build_so_it_does_not_stay_triggered(api_client, fail_build_api):
    tasks._fail_build(api_client, 42, PreContainerFailure(BuildUserError.GENERIC))

    patched = requests_for(fail_build_api, "PATCH", "/api/v2/build/42/")[0].json()
    assert patched == {"state": "finished", "success": False, "length": 0}


def test_fail_build_still_patches_when_the_notification_post_fails(api_client, requests_mock):
    """The build must leave ``triggered`` even if the notification POST fails."""
    requests_mock.post(f"{API_URL}/api/v2/notifications/", status_code=500)
    requests_mock.patch(f"{API_URL}/api/v2/build/42/", status_code=201)

    tasks._fail_build(api_client, 42, PreContainerFailure(BuildUserError.GENERIC))

    patched = requests_for(requests_mock, "PATCH", "/api/v2/build/42/")[0].json()
    assert patched == {"state": "finished", "success": False, "length": 0}


def test_fail_build_authenticates_with_the_build_api_key(api_client, fail_build_api):
    tasks._fail_build(api_client, 42, PreContainerFailure(BuildUserError.GENERIC))

    request = requests_for(fail_build_api, "POST", "/api/v2/notifications/")[0]
    assert request.headers["Authorization"] == "Token TOKEN"


def test_cancel_build_finalizes_the_build_as_cancelled(api_client, fail_build_api):
    tasks._cancel_build(api_client, 42)

    posted = requests_for(fail_build_api, "POST", "/api/v2/notifications/")[0].json()
    assert posted["message_id"] == BuildCancelled.CANCELLED_BY_USER

    patched = requests_for(fail_build_api, "PATCH", "/api/v2/build/42/")[0].json()
    assert patched == {"state": "cancelled", "success": False, "length": 0}


def test_cancellation_handlers_raise_build_cancelled():
    """A revoke lands as SIGINT; it must not surface as a KeyboardInterrupt."""
    with pytest.raises(BuildCancelled) as excinfo, tasks._cancellation_handlers():
        os.kill(os.getpid(), signal.SIGINT)

    assert excinfo.value.message_id == BuildCancelled.CANCELLED_BY_USER


def test_cancellation_handlers_are_restored_after_the_task():
    """Otherwise billiard's recycle SIGTERM logs a false cancellation."""
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    with tasks._cancellation_handlers():
        assert signal.getsignal(signal.SIGTERM) is not previous[signal.SIGTERM]

    assert {sig: signal.getsignal(sig) for sig in previous} == previous


@pytest.fixture
def consumer():
    """A stand-in for the Celery Consumer that records cancelled queues."""
    cancelled = []
    return types.SimpleNamespace(cancel_task_queue=cancelled.append, cancelled=cancelled)


def test_task_received_cancels_the_queue_consumer(consumer):
    """
    The main process must stop consuming on the first build, or it grabs a
    second one while the instance is already terminating.
    """
    request = types.SimpleNamespace(name=constants.RUN_BUILD_TASK_NAME)

    tasks._on_run_build_received(consumer, request=request)

    assert consumer.cancelled == ["build:isolated"]


def test_task_received_keeps_consuming_under_docker_compose(consumer, monkeypatch):
    monkeypatch.setenv("RTD_DOCKER_COMPOSE", "1")
    request = types.SimpleNamespace(name=constants.RUN_BUILD_TASK_NAME)

    tasks._on_run_build_received(consumer, request=request)

    assert consumer.cancelled == []


def test_task_received_ignores_other_tasks(consumer):
    request = types.SimpleNamespace(name="some.other.task")

    tasks._on_run_build_received(consumer, request=request)

    assert consumer.cancelled == []


@pytest.fixture
def postrun(monkeypatch):
    """Record the ordered instance-lifecycle calls the postrun handler makes."""
    calls = []
    monkeypatch.setattr(tasks, "self_terminate", lambda: calls.append("terminated"))
    monkeypatch.setattr(
        tasks, "set_scale_in_protection", lambda protected: calls.append(f"protected={protected}")
    )
    return calls


def test_postrun_self_terminates_after_a_build(postrun):
    sender = types.SimpleNamespace(name=constants.RUN_BUILD_TASK_NAME)

    tasks._on_run_build_postrun(sender, kwargs={"no_self_terminate": False})

    assert "terminated" in postrun


def test_postrun_releases_scale_in_protection_before_terminating(postrun):
    """
    Order matters: TerminateInstanceInAutoScalingGroup refuses to terminate a
    protected instance, which would strand it in the ASG.
    """
    sender = types.SimpleNamespace(name=constants.RUN_BUILD_TASK_NAME)

    tasks._on_run_build_postrun(sender, kwargs={"no_self_terminate": False})

    assert postrun == ["protected=False", "terminated"]


def test_postrun_skips_self_terminate_when_asked(postrun):
    sender = types.SimpleNamespace(name=constants.RUN_BUILD_TASK_NAME)

    tasks._on_run_build_postrun(sender, kwargs={"no_self_terminate": True})

    assert "terminated" not in postrun


def test_postrun_releases_scale_in_protection_even_when_not_terminating(postrun):
    """A protected instance can't be scaled in either — never leave it set."""
    sender = types.SimpleNamespace(name=constants.RUN_BUILD_TASK_NAME)

    tasks._on_run_build_postrun(sender, kwargs={"no_self_terminate": True})

    assert postrun == ["protected=False"]


def test_postrun_ignores_other_tasks(postrun):
    """Only run_build owns the instance; nothing else may touch it."""
    sender = types.SimpleNamespace(name="some.other.task")

    tasks._on_run_build_postrun(sender, kwargs={})

    assert postrun == []


# ---------------------------------------------------------------------------
# Uploaded builds (direct artifact upload)
#
# The user uploaded a ZIP of already-built docs, so there is no repository to
# reach: the worker must not sparse-clone, must not sync versions, and must not
# require a config file. It still resolves resources and injects the container
# environment the same way.
# ---------------------------------------------------------------------------


@pytest.fixture
def prepare_uploaded_build(api_client, mock_api):
    """Run ``_prepare_build`` for an uploaded build."""

    def _prepare(project=None):
        mock_api(project=project, build={"is_uploaded": True})
        build, version = tasks._fetch_build(api_client, 42)
        result = tasks._prepare_build(build=build, version=version)
        return result, {}

    return _prepare


def test_prepare_build_uses_the_latest_lts_image_for_uploaded_builds(
    prepare_uploaded_build,
):
    # ``build.os`` is unknowable without the config file; the container only
    # needs ``unzip``, so any current image works.
    (build_os, _, _), _ = prepare_uploaded_build()

    assert build_os == constants.UPLOADED_BUILD_OS


def test_prepare_build_resolves_the_project_resources_for_uploaded_builds(
    prepare_uploaded_build,
):
    (_, memory, time_limit), _ = prepare_uploaded_build(
        project={"container_mem_limit": "2g", "container_time_limit": 600}
    )

    assert memory == "2g"
    assert time_limit == 600


def test_prepare_build_falls_back_to_default_resources_for_uploaded_builds(
    prepare_uploaded_build,
):
    (_, memory, time_limit), _ = prepare_uploaded_build()

    assert memory == constants.BUILD_MEMORY_LIMIT
    assert time_limit == constants.BUILD_TIME_LIMIT


def test_fetch_build_still_fails_uploaded_builds_without_a_version(api_client, requests_mock):
    requests_mock.get(
        f"{API_URL}/api/v2/build/42/", json={"id": 42, "is_uploaded": True}, headers=JSON
    )

    with pytest.raises(PreContainerFailure) as excinfo:
        tasks._fetch_build(api_client, 42)

    assert excinfo.value.message_id == BuildAppError.GENERIC_WITH_BUILD_ID


# ---------------------------------------------------------------------------
# Wall-clock limit
# ---------------------------------------------------------------------------


def test_time_limit_arms_and_disarms_the_alarm(monkeypatch):
    alarms = []
    monkeypatch.setattr(tasks.signal, "alarm", lambda seconds: alarms.append(seconds))

    with tasks._time_limit(900):
        pass

    assert alarms == [900, 0]


def test_time_limit_disarms_even_when_the_build_raises(monkeypatch):
    """A failed build must not leave an alarm armed for whatever runs next."""
    alarms = []
    monkeypatch.setattr(tasks.signal, "alarm", lambda seconds: alarms.append(seconds))

    with pytest.raises(RuntimeError):
        with tasks._time_limit(900):
            raise RuntimeError("boom")

    assert alarms[-1] == 0


def test_no_time_limit_leaves_the_alarm_alone(monkeypatch):
    alarms = []
    monkeypatch.setattr(tasks.signal, "alarm", lambda seconds: alarms.append(seconds))

    with tasks._time_limit(0):
        pass

    assert alarms == []


def test_time_limit_actually_fires():
    """
    Against the real signal machinery, not a stub.

    The runner installs a SIGALRM handler that raises ``BUILD_TIME_OUT``; this
    asserts the alarm we arm actually reaches a handler, rather than only that
    we called ``signal.alarm``.
    """
    fired = []

    def handler(signum, frame):
        fired.append(signum)
        raise TimeoutError

    previous = signal.signal(signal.SIGALRM, handler)
    try:
        with pytest.raises(TimeoutError):
            with tasks._time_limit(1):
                time.sleep(3)
    finally:
        signal.signal(signal.SIGALRM, previous)
        signal.alarm(0)

    assert fired == [signal.SIGALRM]


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


class FakeDockerClient:
    def __init__(self):
        self.execs = []

    def exec_create(self, **kwargs):
        self.execs.append(kwargs)
        return {"Id": "exec-id"}

    def exec_start(self, **kwargs):
        self.started = kwargs


def test_healthcheck_pings_the_build_from_the_container(monkeypatch):
    """
    Without these, ``Build.healthcheck`` stays NULL and
    ``finish_unhealthy_builds`` can never reap a build whose instance vanished.
    """
    monkeypatch.setattr(tasks.socket, "gethostname", lambda: "builder-i-abc123")
    client = FakeDockerClient()

    tasks._start_healthcheck(
        client,
        "build-42",
        {"RTD_HEALTHCHECK_API_HOST": "https://lb.example", "RTD_PRODUCTION_DOMAIN": "rtd.org"},
        42,
    )

    command = client.execs[0]["cmd"]
    assert client.execs[0]["container"] == "build-42"
    assert "https://lb.example/api/v2/build/42/healthcheck/?builder=builder-i-abc123" in command
    assert "Host: rtd.org" in command
    assert f"sleep {constants.BUILD_HEALTHCHECK_DELAY}" in command


def test_healthcheck_runs_detached(monkeypatch):
    """It never returns, so a foreground exec would block the whole build."""
    client = FakeDockerClient()

    tasks._start_healthcheck(client, "build-42", {"RTD_HEALTHCHECK_API_HOST": "https://lb"}, 42)

    assert client.started["detach"] is True


def test_healthcheck_survives_the_load_balancers_certificate(monkeypatch):
    """
    The URL is the internal LB's raw AWS DNS name; its cert is for the
    production domain and it routes on ``Host``.
    """
    client = FakeDockerClient()

    tasks._start_healthcheck(client, "build-42", {"RTD_HEALTHCHECK_API_HOST": "https://lb"}, 42)

    assert "--insecure" in client.execs[0]["cmd"]


def test_no_healthcheck_host_is_a_no_op():
    client = FakeDockerClient()

    tasks._start_healthcheck(client, "build-42", {}, 42)

    assert client.execs == []
