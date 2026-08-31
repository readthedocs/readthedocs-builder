"""
Minimal Celery worker that runs Read the Docs builds.

This package is installed on a Packer-baked AMI and run as a systemd
service (``readthedocs-celery-worker.service``). One worker process per
EC2 instance consumes the ``build:isolated`` queue with
``--max-tasks-per-child=1``: after a single build task completes (or fails),
the worker exits and the task itself calls
``autoscaling:TerminateInstanceInAutoScalingGroup`` on the host so the
instance is removed from the ``build-isolated`` ASG (the ASG then
launches a fresh instance to take its place).
"""
