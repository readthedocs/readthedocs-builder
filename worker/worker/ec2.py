"""EC2 instance metadata + self-terminate."""

import os
from urllib.parse import urlparse

import boto3
import redis
import requests
import structlog
from botocore.exceptions import ClientError

from worker import constants


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


def _describe_group(client, asg_name: str) -> dict | None:
    """The ASG as returned by ``DescribeAutoScalingGroups``, or ``None`` on failure."""
    try:
        groups = client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[asg_name],
        ).get("AutoScalingGroups", [])
    except Exception:
        log.exception("Failed to describe the autoscaling group.", asg_name=asg_name)
        return None

    if not groups:
        log.warning("Autoscaling group not found.", asg_name=asg_name)
        return None
    return groups[0]


def _queued_builds() -> int | None:
    """
    Builds waiting in the broker queue that no instance has claimed yet.

    Read straight from Redis with the same URL Celery uses. Returns ``None`` if
    the lookup fails.
    """
    broker_url = os.environ.get("RTD_BROKER_URL")
    if not broker_url:
        return None

    kwargs = {"socket_connect_timeout": 2, "socket_timeout": 2}
    if urlparse(broker_url).scheme == "rediss":
        # Same as ``broker_use_ssl`` in worker.celery.
        kwargs.update(ssl_cert_reqs=None, ssl_check_hostname=False)

    try:
        return redis.Redis.from_url(broker_url, **kwargs).llen(constants.RUN_BUILD_TASK_QUEUE)
    except Exception:
        log.exception("Failed to read the broker queue length.")
        return None


def ensure_warm_buffer():
    """
    Raise desired capacity so ``BUFFER`` idle instances stay ahead of demand.

    Called when a build starts.
    Desired must cover every building instance (the protected ones, plus us),
    every build still queued, and the buffer. This only ever raises desired:
    a finished build removes its instance and its demand together.
    """
    instance_id = _ec2_metadata("instance-id")
    if not instance_id:
        log.info("Skipping warm buffer: not running on EC2.")
        return

    client = _autoscaling_client()
    asg_name = _asg_name(client, instance_id)
    if not asg_name:
        return

    group = _describe_group(client, asg_name)
    if group is None:
        return

    busy = {instance_id} | {
        instance["InstanceId"]
        for instance in group.get("Instances", [])
        if instance.get("ProtectedFromScaleIn")
    }
    queued = _queued_builds() or 0
    desired = group["DesiredCapacity"]
    target = min(len(busy) + queued + constants.BUFFER, group["MaxSize"])
    log.info(
        "Warm buffer checked.",
        asg_name=asg_name,
        busy=len(busy),
        queued=queued,
        desired=desired,
        target=target,
    )

    # Never reduce the desired capacity; only increase it if needed.
    if target <= desired:
        return

    try:
        client.set_desired_capacity(
            AutoScalingGroupName=asg_name,
            DesiredCapacity=target,
            HonorCooldown=False,
        )
        log.info("Warm buffer lifted.", asg_name=asg_name, desired=desired, target=target)
    except Exception:
        log.exception("Failed to lift the warm buffer.", asg_name=asg_name, target=target)


def _is_at_min_size(exc: ClientError) -> bool:
    """AWS refuses to decrement desired capacity below the group's MinSize."""
    error = exc.response.get("Error", {})
    message = error.get("Message", "").lower()
    return error.get("Code") == "ValidationError" and (
        "minsize" in message or "min size" in message
    )


def _terminate(client, instance_id: str, decrement: bool):
    client.terminate_instance_in_auto_scaling_group(
        InstanceId=instance_id,
        ShouldDecrementDesiredCapacity=decrement,
    )
    log.info("Self-terminate requested.", instance_id=instance_id, decrement=decrement)


def self_terminate():
    """
    Tell the ASG to terminate the EC2 instance we're running on.

    Always decrements desired capacity.``ensure_warm_buffer`` refills the buffer
    on the next build.

    Off EC2 (dev) there's no instance id, so this is a no-op — no dedicated
    skip flag needed.
    """
    instance_id = _ec2_metadata("instance-id")
    if not instance_id:
        log.warning("Skipping self-terminate: no instance id (running outside EC2?).")
        return

    client = _autoscaling_client()
    try:
        _terminate(client, instance_id, True)
        return
    except ClientError as exc:
        if not _is_at_min_size(exc):
            log.exception("Self-terminate failed.", instance_id=instance_id)
            return
    except Exception:
        log.exception("Self-terminate failed.", instance_id=instance_id)
        return

    # Desired already equals MinSize. This instance stopped consuming the queue
    # when its build arrived, so leaving it alive would strand it; terminate
    # without the decrement and let the ASG replace it to keep MinSize.
    log.info("Fleet at MinSize; terminating without decrement.", instance_id=instance_id)
    try:
        _terminate(client, instance_id, False)
    except Exception:
        log.exception("Self-terminate failed.", instance_id=instance_id)
