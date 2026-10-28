import boto3
import pytest
from botocore.stub import Stubber

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


def test_self_terminate_asks_the_asg_to_terminate_this_instance(imds, asg):
    asg.add_response(
        "terminate_instance_in_auto_scaling_group",
        {},
        {"InstanceId": INSTANCE_ID, "ShouldDecrementDesiredCapacity": False},
    )

    ec2.self_terminate()

    asg.assert_no_pending_responses()


def test_self_terminate_skips_when_not_running_on_ec2(requests_mock, monkeypatch):
    """No instance id means no IMDS, e.g. local development."""
    requests_mock.put(f"{ec2.IMDS_URL}/latest/api/token", exc=OSError("no route to host"))
    monkeypatch.setattr(ec2, "_autoscaling_client", lambda: pytest.fail("must not be called"))

    assert ec2.self_terminate() is None


def test_self_terminate_never_raises(imds, asg):
    asg.add_client_error(
        "terminate_instance_in_auto_scaling_group", service_error_code="AccessDenied"
    )

    assert ec2.self_terminate() is None
