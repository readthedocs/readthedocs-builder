"""EC2 instance metadata + self-terminate."""

import os

import boto3
import requests
import structlog


log = structlog.get_logger(__name__)


IMDS_URL = "http://169.254.169.254"
IMDS_TIMEOUT_SECONDS = 2


def _ec2_metadata(path: str) -> str:
    """
    Fetch a value from the EC2 IMDSv2 metadata service.

    Returns an empty string if the metadata service is unreachable
    (e.g. running outside EC2 during tests). Callers must handle the
    empty value rather than treating it as a hard error so unit tests
    can exercise ``run_build`` without IMDS.

    Under docker-compose there is no IMDS, and 169.254.169.254 is not routable.
    """
    if os.environ.get("RTD_DOCKER_COMPOSE"):
        return ""

    try:
        token = requests.put(
            f"{IMDS_URL}/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
            timeout=IMDS_TIMEOUT_SECONDS,
        )
        token.raise_for_status()

        resp = requests.get(
            f"{IMDS_URL}/latest/meta-data/{path}",
            headers={"X-aws-ec2-metadata-token": token.text},
            timeout=IMDS_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        log.warning("EC2 metadata fetch failed.", path=path, error=str(exc))
        return ""


def _autoscaling_client():
    region = _ec2_metadata("placement/region")
    return boto3.client("autoscaling", region_name=region or None)


def _asg_name(client, instance_id: str) -> str:
    """
    Name of the ASG this instance belongs to, or empty string.

    ``set_instance_protection`` needs the group name, so we ask the API.
    """
    try:
        instances = client.describe_auto_scaling_instances(
            InstanceIds=[instance_id],
        ).get("AutoScalingInstances", [])
    except Exception:
        log.exception("Failed to describe the autoscaling instance.", instance_id=instance_id)
        return ""

    if not instances:
        log.warning("Instance is not part of an ASG.", instance_id=instance_id)
        return ""
    return instances[0].get("AutoScalingGroupName", "")


def set_scale_in_protection(protected: bool):
    """
    Protect this instance from ASG scale-in while it's building, or release it.

    Must be released before ``self_terminate`` —
    ``TerminateInstanceInAutoScalingGroup`` refuses to terminate a protected
    instance, which would strand it in the ASG forever.
    """
    instance_id = _ec2_metadata("instance-id")
    if not instance_id:
        log.info("Skipping scale-in protection: not running on EC2.")
        return

    client = _autoscaling_client()
    asg_name = _asg_name(client, instance_id)
    if not asg_name:
        log.warning("Skipping scale-in protection: no ASG name.", instance_id=instance_id)
        return

    try:
        client.set_instance_protection(
            InstanceIds=[instance_id],
            AutoScalingGroupName=asg_name,
            ProtectedFromScaleIn=protected,
        )
        log.info(
            "Scale-in protection set.",
            instance_id=instance_id,
            asg_name=asg_name,
            protected=protected,
        )
    except Exception:
        # Never fail a build over this. Left protected, the instance is caught
        # by the release in task_postrun; if that fails too it needs manual
        # cleanup, which is why we log loudly.
        log.exception(
            "Failed to set scale-in protection.",
            instance_id=instance_id,
            asg_name=asg_name,
            protected=protected,
        )


def self_terminate():
    """
    Tell the ASG to terminate the EC2 instance we're running on.

    Off EC2 (dev) there's no instance id, so this is a no-op — no dedicated
    skip flag needed.
    """
    instance_id = _ec2_metadata("instance-id")
    if not instance_id:
        log.warning("Skipping self-terminate: no instance id (running outside EC2?).")
        return

    client = _autoscaling_client()
    try:
        client.terminate_instance_in_auto_scaling_group(
            InstanceId=instance_id,
            ShouldDecrementDesiredCapacity=False,
        )
        log.info("Self-terminate requested.", instance_id=instance_id)
    except Exception:
        log.exception("Self-terminate failed.", instance_id=instance_id)
