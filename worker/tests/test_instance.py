import subprocess

from conftest import API_URL
from conftest import JSON
from worker import instance


HOSTNAME = "build-isolated-i-0123456789abcdef0"
PROTECT_URL = f"{API_URL}/api/v2/build/42/instance/protect/"
RELEASE_URL = f"{API_URL}/api/v2/build/42/instance/release/"


def test_protect_claims_the_build_for_this_host(api_client, requests_mock):
    requests_mock.post(PROTECT_URL, status_code=204)

    assert instance.protect(api_client, 42, HOSTNAME) is True

    assert requests_mock.last_request.json() == {"builder": HOSTNAME}
    assert requests_mock.last_request.headers["Authorization"] == "Token TOKEN"


def test_protect_never_raises(api_client, requests_mock):
    requests_mock.post(PROTECT_URL, status_code=500)

    assert instance.protect(api_client, 42, HOSTNAME) is False


def test_protect_calls_the_api_under_docker_compose_too(api_client, requests_mock, monkeypatch):
    """The API decides what development means; the worker behaves like production."""
    monkeypatch.setenv("RTD_DOCKER_COMPOSE", "1")
    requests_mock.post(PROTECT_URL, status_code=204)

    assert instance.protect(api_client, 42, "3f9a1c2b7d4e") is True

    assert requests_mock.last_request.json() == {"builder": "3f9a1c2b7d4e"}


def test_release_returns_the_state_the_api_decided(api_client, requests_mock):
    requests_mock.post(RELEASE_URL, json={"state": "kept", "decremented": False}, headers=JSON)

    assert instance.release(api_client, 42) is instance.InstanceState.KEPT


def test_release_fails_when_the_api_errors(api_client, requests_mock):
    requests_mock.post(RELEASE_URL, status_code=500)

    assert instance.release(api_client, 42) is instance.InstanceState.FAILED


def test_release_fails_on_an_unexpected_payload(api_client, requests_mock):
    requests_mock.post(RELEASE_URL, json={}, headers=JSON)

    assert instance.release(api_client, 42) is instance.InstanceState.FAILED


def test_release_fails_on_an_unknown_state(api_client, requests_mock):
    requests_mock.post(RELEASE_URL, json={"state": "exploded"}, headers=JSON)

    assert instance.release(api_client, 42) is instance.InstanceState.FAILED


def test_release_calls_the_api_under_docker_compose_too(api_client, requests_mock, monkeypatch):
    monkeypatch.setenv("RTD_DOCKER_COMPOSE", "1")
    requests_mock.post(RELEASE_URL, json={"state": "kept", "decremented": False}, headers=JSON)

    assert instance.release(api_client, 42) is instance.InstanceState.KEPT

    assert requests_mock.call_count == 1


def test_poweroff_shuts_the_host_down(monkeypatch):
    calls = []
    monkeypatch.setattr(instance.subprocess, "run", lambda *args, **kwargs: calls.append(args[0]))

    instance.poweroff()

    assert calls == [instance.POWEROFF_COMMAND]


def test_poweroff_refuses_under_docker_compose(monkeypatch):
    monkeypatch.setenv("RTD_DOCKER_COMPOSE", "1")
    calls = []
    monkeypatch.setattr(instance.subprocess, "run", lambda *args, **kwargs: calls.append(args[0]))

    instance.poweroff()

    assert calls == []


def test_poweroff_never_raises(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(instance.subprocess, "run", fail)

    instance.poweroff()
