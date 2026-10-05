import datetime

import boto3
import pytest
from botocore.stub import Stubber

from worker import constants
from worker import ec2


INSTANCE_ID = "i-0abc"
ASG_NAME = "build-isolated"


@pytest.fixture
def imds(requests_mock):
    """
    Serve IMDSv2: the token PUT plus the metadata GETs.

    Mocked at the HTTP layer, so the IMDSv2 token dance itself is under test.
    """
    requests_mock.put(f"{ec2.IMDS_URL}/latest/api/token", text="TOKEN")
    requests_mock.get(f"{ec2.IMDS_URL}/latest/meta-data/instance-id", text=INSTANCE_ID)
    requests_mock.get(f"{ec2.IMDS_URL}/latest/meta-data/placement/region", text="us-east-2")
    return requests_mock


@pytest.fixture
def asg(monkeypatch):
    """
    Stub the autoscaling API with botocore's Stubber.

    ``requests_mock`` cannot be used here: botocore doesn't go through
    ``requests``, so it would sail past the mock and hit real AWS. Stubber also
    validates params and responses against the AWS API model.
    """
    client = boto3.client(
        "autoscaling",
        region_name="us-east-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    monkeypatch.setattr(ec2, "_autoscaling_client", lambda: client)
    with Stubber(client) as stubber:
        yield stubber


class FakeRedis:
    def __init__(self, length):
        self.length = length
        self.llen_calls = []

    def llen(self, key):
        self.llen_calls.append(key)
        return self.length


@pytest.fixture(autouse=True)
def queue(monkeypatch):
    """
    Stand in for the broker queue ``self_terminate`` consults.

    Empty by default so the warm-buffer tests exercise the idle count; tests
    set ``queue.length`` to simulate waiting builds. ``from_url`` kwargs are
    recorded so the TLS handling for ``rediss://`` can be asserted.
    """
    monkeypatch.setenv("RTD_BROKER_URL", "redis://broker:6379/0")
    fake = FakeRedis(length=0)
    fake.from_url_kwargs = None

    def from_url(url, **kwargs):
        fake.from_url_kwargs = kwargs
        return fake

    monkeypatch.setattr(ec2.redis.Redis, "from_url", staticmethod(from_url))
    return fake


def stub_describe(stubber, asg_name=ASG_NAME):
    stubber.add_response(
        "describe_auto_scaling_instances",
        {
            "AutoScalingInstances": [
                {
                    "InstanceId": INSTANCE_ID,
                    "AutoScalingGroupName": asg_name,
                    "AvailabilityZone": "us-east-2a",
                    "LifecycleState": "InService",
                    "HealthStatus": "HEALTHY",
                    "ProtectedFromScaleIn": False,
                }
            ]
        },
        {"InstanceIds": [INSTANCE_ID]},
    )


def test_ec2_metadata_sends_the_imdsv2_token(imds):
    assert ec2._ec2_metadata("instance-id") == INSTANCE_ID

    token_request, metadata_request = imds.request_history
    assert token_request.method == "PUT"
    assert token_request.headers["X-aws-ec2-metadata-token-ttl-seconds"] == "60"
    assert metadata_request.headers["X-aws-ec2-metadata-token"] == "TOKEN"


def test_ec2_metadata_returns_empty_string_when_imds_is_unreachable(requests_mock):
    """Callers must handle the empty value rather than crash off EC2."""
    requests_mock.put(f"{ec2.IMDS_URL}/latest/api/token", exc=OSError("no route to host"))

    assert ec2._ec2_metadata("instance-id") == ""


def test_ec2_metadata_does_not_contact_imds_under_docker_compose(imds, monkeypatch):
    """
    Dev has no IMDS and 169.254.169.254 isn't routable, so the call must not be
    attempted at all — otherwise every lookup burns its connect timeout.
    """
    monkeypatch.setenv("RTD_DOCKER_COMPOSE", "1")

    assert ec2._ec2_metadata("instance-id") == ""
    assert imds.request_history == []


def test_set_scale_in_protection_skips_under_docker_compose(imds, monkeypatch):
    monkeypatch.setenv("RTD_DOCKER_COMPOSE", "1")
    monkeypatch.setattr(ec2, "_autoscaling_client", lambda: pytest.fail("must not be called"))

    assert ec2.set_scale_in_protection(True) is None
    assert ec2.set_scale_in_protection(False) is None
    assert imds.request_history == []


def test_self_terminate_skips_under_docker_compose(imds, monkeypatch):
    monkeypatch.setenv("RTD_DOCKER_COMPOSE", "1")
    monkeypatch.setattr(ec2, "_autoscaling_client", lambda: pytest.fail("must not be called"))

    assert ec2.self_terminate() is None
    assert imds.request_history == []


def test_ec2_metadata_returns_empty_string_when_imds_errors(requests_mock):
    requests_mock.put(f"{ec2.IMDS_URL}/latest/api/token", text="TOKEN")
    requests_mock.get(f"{ec2.IMDS_URL}/latest/meta-data/instance-id", status_code=404)

    assert ec2._ec2_metadata("instance-id") == ""


def test_set_scale_in_protection_protects_this_instance_in_its_asg(imds, asg):
    stub_describe(asg)
    asg.add_response(
        "set_instance_protection",
        {},
        {
            "InstanceIds": [INSTANCE_ID],
            "AutoScalingGroupName": ASG_NAME,
            "ProtectedFromScaleIn": True,
        },
    )

    ec2.set_scale_in_protection(True)

    asg.assert_no_pending_responses()


def test_set_scale_in_protection_releases_protection(imds, asg):
    stub_describe(asg)
    asg.add_response(
        "set_instance_protection",
        {},
        {
            "InstanceIds": [INSTANCE_ID],
            "AutoScalingGroupName": ASG_NAME,
            "ProtectedFromScaleIn": False,
        },
    )

    ec2.set_scale_in_protection(False)

    asg.assert_no_pending_responses()


def test_set_scale_in_protection_skips_when_not_on_ec2(requests_mock, monkeypatch):
    requests_mock.put(f"{ec2.IMDS_URL}/latest/api/token", exc=OSError("no route to host"))
    monkeypatch.setattr(ec2, "_autoscaling_client", lambda: pytest.fail("must not be called"))

    assert ec2.set_scale_in_protection(True) is None


def test_set_scale_in_protection_skips_when_the_instance_has_no_asg(imds, asg):
    """Nothing to protect against if the instance isn't in an ASG."""
    asg.add_response(
        "describe_auto_scaling_instances",
        {"AutoScalingInstances": []},
        {"InstanceIds": [INSTANCE_ID]},
    )

    ec2.set_scale_in_protection(True)

    # No set_instance_protection was stubbed, so a call would have raised.
    asg.assert_no_pending_responses()


def test_set_scale_in_protection_never_raises(imds, asg):
    """A protection failure must not fail the build."""
    stub_describe(asg)
    asg.add_client_error("set_instance_protection", service_error_code="AccessDenied")

    assert ec2.set_scale_in_protection(True) is None


def stub_group(stubber, *, idle=0, protected=0, include_self=True):
    """
    Stub ``describe_auto_scaling_groups`` with ``idle`` unprotected in-service
    instances, ``protected`` building ones, and (optionally) this instance.
    """

    def instance(instance_id, protected_from_scale_in):
        return {
            "InstanceId": instance_id,
            "AvailabilityZone": "us-east-2a",
            "LifecycleState": "InService",
            "HealthStatus": "Healthy",
            "ProtectedFromScaleIn": protected_from_scale_in,
        }

    instances = [instance(f"i-idle{n}", False) for n in range(idle)]
    instances += [instance(f"i-busy{n}", True) for n in range(protected)]
    if include_self:
        instances.append(instance(INSTANCE_ID, False))

    stubber.add_response(
        "describe_auto_scaling_groups",
        {
            "AutoScalingGroups": [
                {
                    "AutoScalingGroupName": ASG_NAME,
                    "MinSize": 5,
                    "MaxSize": 100,
                    "DesiredCapacity": len(instances),
                    "DefaultCooldown": 300,
                    "AvailabilityZones": ["us-east-2a"],
                    "HealthCheckType": "EC2",
                    "CreatedTime": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
                    "Instances": instances,
                }
            ]
        },
        {"AutoScalingGroupNames": [ASG_NAME]},
    )


def stub_terminate(stubber, decrement):
    stubber.add_response(
        "terminate_instance_in_auto_scaling_group",
        {},
        {"InstanceId": INSTANCE_ID, "ShouldDecrementDesiredCapacity": decrement},
    )


def test_self_terminate_decrements_when_the_warm_buffer_is_full(imds, asg):
    """Enough idle instances already: shrink the fleet instead of being replaced."""
    stub_describe(asg)
    stub_group(asg, idle=constants.WARM_BUFFER, protected=3)
    stub_terminate(asg, decrement=True)

    ec2.self_terminate()

    asg.assert_no_pending_responses()


def test_self_terminate_is_replaced_when_the_warm_buffer_is_short(imds, asg):
    """Too few idle instances: keep desired so the ASG launches a replacement now."""
    stub_describe(asg)
    stub_group(asg, idle=constants.WARM_BUFFER - 1, protected=3)
    stub_terminate(asg, decrement=False)

    ec2.self_terminate()

    asg.assert_no_pending_responses()


def test_self_terminate_does_not_count_itself_as_idle(imds, asg):
    """Our own protection is already released by task_postrun; we're not spare capacity."""
    stub_describe(asg)
    stub_group(asg, idle=constants.WARM_BUFFER - 1, include_self=True)
    stub_terminate(asg, decrement=False)

    ec2.self_terminate()

    asg.assert_no_pending_responses()


def test_self_terminate_is_replaced_while_builds_are_queued(imds, asg, queue):
    """Waiting builds mean the idle count lies (fresh instances still booting): never shrink."""
    queue.length = 3
    stub_terminate(asg, decrement=False)

    ec2.self_terminate()

    # No describe calls were stubbed: the queue check short-circuits them.
    asg.assert_no_pending_responses()
    assert queue.llen_calls == [constants.RUN_BUILD_TASK_QUEUE]


def test_self_terminate_is_replaced_when_the_queue_lookup_fails(imds, asg, monkeypatch):
    def from_url(url, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(ec2.redis.Redis, "from_url", staticmethod(from_url))
    stub_terminate(asg, decrement=False)

    ec2.self_terminate()

    asg.assert_no_pending_responses()


def test_queued_builds_disables_tls_verification_for_rediss(monkeypatch, queue):
    """Mirror ``broker_use_ssl`` in worker.celery: the broker cert is self-signed."""
    monkeypatch.setenv("RTD_BROKER_URL", "rediss://broker:6379/0")

    assert ec2._queued_builds() == 0
    assert queue.from_url_kwargs["ssl_cert_reqs"] is None
    assert queue.from_url_kwargs["ssl_check_hostname"] is False


def test_queued_builds_uses_plain_connection_for_redis(queue):
    assert ec2._queued_builds() == 0
    assert "ssl_cert_reqs" not in queue.from_url_kwargs


def test_self_terminate_is_replaced_when_the_group_lookup_fails(imds, asg):
    """Unknown fleet state: a replacement is the safe default."""
    stub_describe(asg)
    asg.add_client_error("describe_auto_scaling_groups", service_error_code="AccessDenied")
    stub_terminate(asg, decrement=False)

    ec2.self_terminate()

    asg.assert_no_pending_responses()


def test_self_terminate_is_replaced_when_the_instance_has_no_asg(imds, asg):
    asg.add_response(
        "describe_auto_scaling_instances",
        {"AutoScalingInstances": []},
        {"InstanceIds": [INSTANCE_ID]},
    )
    stub_terminate(asg, decrement=False)

    ec2.self_terminate()

    asg.assert_no_pending_responses()


def test_self_terminate_without_decrement_at_min_size(imds, asg):
    """At MinSize AWS refuses the decrement; terminate anyway so the instance isn't stranded."""
    stub_describe(asg)
    stub_group(asg, idle=constants.WARM_BUFFER)
    asg.add_client_error(
        "terminate_instance_in_auto_scaling_group",
        service_error_code="ValidationError",
        service_message=(
            "Currently, desiredSize equals minSize (5). Terminating instance without "
            "replacement will violate group's min size constraint."
        ),
        expected_params={"InstanceId": INSTANCE_ID, "ShouldDecrementDesiredCapacity": True},
    )
    stub_terminate(asg, decrement=False)

    ec2.self_terminate()

    asg.assert_no_pending_responses()


def test_self_terminate_does_not_retry_other_validation_errors(imds, asg, monkeypatch):
    stub_describe(asg)
    stub_group(asg, idle=constants.WARM_BUFFER)
    asg.add_client_error(
        "terminate_instance_in_auto_scaling_group",
        service_error_code="ValidationError",
        service_message="Instance Id not found - No managed instance found for instance ID: i-0abc",
    )
    client = ec2._autoscaling_client()
    calls = []
    original = client.terminate_instance_in_auto_scaling_group
    monkeypatch.setattr(
        client,
        "terminate_instance_in_auto_scaling_group",
        lambda **kwargs: calls.append(kwargs) or original(**kwargs),
    )

    assert ec2.self_terminate() is None

    assert len(calls) == 1
    assert calls[0]["ShouldDecrementDesiredCapacity"] is True


def test_self_terminate_skips_when_not_running_on_ec2(requests_mock, monkeypatch):
    """No instance id means no IMDS, e.g. local development."""
    requests_mock.put(f"{ec2.IMDS_URL}/latest/api/token", exc=OSError("no route to host"))
    monkeypatch.setattr(ec2, "_autoscaling_client", lambda: pytest.fail("must not be called"))

    assert ec2.self_terminate() is None


def test_self_terminate_never_raises(imds, asg):
    stub_describe(asg)
    stub_group(asg, idle=constants.WARM_BUFFER)
    asg.add_client_error(
        "terminate_instance_in_auto_scaling_group", service_error_code="AccessDenied"
    )

    assert ec2.self_terminate() is None
